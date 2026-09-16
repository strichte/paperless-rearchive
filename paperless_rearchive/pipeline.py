"""Per-document re-OCR pipeline using unified Chandra OCR engine.

This pipeline uses the unified ChandraOcrEngine which:
1. Renders PDF pages to images using PyMuPDF
2. Calls Chandra for OCR on each page
3. Optionally assembles a searchable PDF/A using ocrmypdf's sandwich pipeline
4. Tracks page-level errors for partial failure reporting

The OCR work is unified (same Chandra calls regardless of mode),
but PDF/A assembly only happens for re-ocr-all to avoid wasting CPU.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from paperless_rearchive.archive.db import (
    fetch_archive_checksum,
    fetch_archive_filename,
    update_archive_checksum,
)
from paperless_rearchive.archive.replacer import (
    ArchiveReplaceError,
    checksum_of_file,
    replace_archive,
    verify_current_checksum,
)
from paperless_rearchive.ocr.base import OcrProviderPlugin
from paperless_rearchive.ocr.chandra_engine import ChandraOcrEngine

if TYPE_CHECKING:
    from paperless_rearchive.config import Settings
    from paperless_rearchive.paperless_api import PaperlessAPI

log = logging.getLogger(__name__)


@dataclass
class DocumentContext:
    """Tag bookkeeping for one document being processed."""

    doc_id: int
    trigger_tag_id: int
    trigger_tag_name: str
    archive_mode: bool  # True: re-ocr-all, False: re-ocr-content only
    current_tags: list[int]


def process_document(
    settings: Settings,
    api: PaperlessAPI,
    provider: OcrProviderPlugin,
    ctx: DocumentContext,
) -> None:
    """Re-OCR one document using unified Chandra OCR engine.

    Tag outcomes:
    * success  -> trigger removed, ``<trigger>-success`` added
    * failure  -> trigger removed, ``<trigger>-failure`` added
    * retry    -> trigger kept (transient OCR error); nothing else touched
    """
    doc = api.document(ctx.doc_id)

    with tempfile.TemporaryDirectory(prefix=f"rearch-{ctx.doc_id}-") as tmp:
        tmp_dir = Path(tmp)
        original = api.download_original(ctx.doc_id, tmp_dir)

        do_archive = ctx.archive_mode
        archive_path: Path | None = None
        if do_archive:
            # The real on-disk archive path lives only in the database: the
            # REST API's archived_file_name is a flattened download name
            # (no template subdirectories).
            try:
                archive_filename = fetch_archive_filename(settings.db, ctx.doc_id)
            except RuntimeError as e:
                if settings.dry_run:
                    log.error("DRY-RUN document %d: DB lookup failed: %s", ctx.doc_id, e)
                    return
                raise
            if not archive_filename:
                log.info(
                    "Document %d has no archive version (born-digital or "
                    "PAPERLESS_ARCHIVE_FILE_GENERATION=never); content-only mode.",
                    ctx.doc_id,
                )
                do_archive = False
            elif original.suffix.lower() != ".pdf":
                log.info("Document %d is not a PDF; content-only mode.", ctx.doc_id)
                do_archive = False
            else:
                archive_path = settings.archive_dir / archive_filename
                try:
                    archive_checksum = fetch_archive_checksum(settings.db, ctx.doc_id)
                    try:
                        verify_current_checksum(archive_path, archive_checksum)
                    except ArchiveReplaceError as e:
                        _repair_checksum_drift(settings, ctx, archive_path, archive_checksum, e)
                except (ArchiveReplaceError, RuntimeError) as e:
                    if settings.dry_run:
                        log.error(
                            "DRY-RUN document %d: archive check failed: %s", ctx.doc_id, e
                        )
                        return
                    _finish(api, ctx, success=False, note=str(e))
                    return

        # Use unified Chandra OCR engine
        engine = ChandraOcrEngine(
            server_url=provider.server_url,
            model_name=provider.model_name,
            api_key=provider.api_key,
            content_format=provider.content_format,
            max_output_tokens=provider.max_output_tokens,
            language=settings.ocr_language,
            dpi=getattr(settings, 'ocr_dpi', 300),
        )

        # Branch: produce PDF/A only for re-ocr-all
        produce_pdf = do_archive and archive_path is not None
        output_pdf_path = tmp_dir / "archive.pdf" if produce_pdf else None

        log.info(
            "Unified OCR engine: document %d, produce_pdf=%s, archive_mode=%s",
            ctx.doc_id,
            produce_pdf,
            ctx.archive_mode,
        )

        result = engine.ocr_document(
            original,
            produce_pdf=produce_pdf,
            output_pdf_path=output_pdf_path,
        )

        # Handle page-level errors
        if result.has_errors:
            log.warning(
                "Document %d: %d of %d pages failed OCR: %s",
                ctx.doc_id,
                len(result.error_pages),
                result.page_count,
                result.errors,
            )

        content = result.markdown

        if not content.strip():
            # No OCR content produced
            message = "OCR produced no content (all pages failed or empty)"
            log.error("Document %d: %s. Nothing written.", ctx.doc_id, message)
            if settings.dry_run:
                return
            _finish(api, ctx, success=False, note=message)
            return

        if settings.dry_run:
            would = f"content ({len(content)} chars)"
            if result.pdf_path is not None:
                would += f" and archive {result.pdf_path}"
            log.info("DRY-RUN document %d: would write %s. Trigger tag kept.", ctx.doc_id, would)
            return

        api.patch_content(ctx.doc_id, content)

        # Add page-errors tag if there were partial failures
        extra_tags = []
        if result.has_errors:
            try:
                page_errors_tag_id = api.ensure_tag("re-ocr-page-errors")
                extra_tags.append(page_errors_tag_id)
                log.info(
                    "Document %d: added re-ocr-page-errors tag (%d pages failed)",
                    ctx.doc_id,
                    len(result.error_pages),
                )
            except Exception:
                log.exception("Could not add re-ocr-page-errors tag for document %d", ctx.doc_id)

        if result.pdf_path is not None and archive_path is not None:
            checksum = replace_archive(archive_path, result.pdf_path)
            update_archive_checksum(settings.db, ctx.doc_id, checksum)

        # Finish: swap trigger tag for outcome tag
        outcome_tag_name = f"{ctx.trigger_tag_name}{'-success' if result.page_count > 0 else '-failure'}"
        try:
            outcome_tag_id = api.ensure_tag(outcome_tag_name)
            all_tags_to_add = [outcome_tag_id] + extra_tags
            api.set_tags(
                ctx.doc_id,
                remove=[ctx.trigger_tag_id],
                add=all_tags_to_add,
                current_tags=ctx.current_tags,
            )
        except Exception:
            log.exception("Could not update tags on document %d; keeping trigger.", ctx.doc_id)
            return

    log.info("Document %d re-OCR complete: %d pages, %d succeeded, %d failed",
             ctx.doc_id, result.page_count, result.success_count, len(result.error_pages))


def _repair_checksum_drift(
    settings: Settings,
    ctx: DocumentContext,
    archive_path: Path,
    expected: str | None,
    error: ArchiveReplaceError,
) -> None:
    """Adopt an on-disk archive whose checksum no longer matches the DB.

    This is the residue of a run that was interrupted between the atomic file
    replace and the ``UPDATE documents_document`` (the process was killed, the
    container stopped, the host rebooted). The archive bytes themselves are
    always complete because the swap uses ``os.replace``.

    Failing the document instead would strand it: the trigger tag is swapped for
    ``<trigger>-failure`` and it never retries. Re-OCR is idempotent and the
    archive is a derived artifact of the immutable original - paperless can
    always regenerate it - so adopt the on-disk bytes and carry on.

    Raises the original error when no repair is possible (missing file, unknown
    checksum) or when running dry (dry runs never write to the database).
    """
    if settings.dry_run or not expected or not archive_path.is_file():
        raise error
    disk_checksum = checksum_of_file(archive_path)
    log.warning(
        "Document %d: archive checksum drift (DB %s != disk %s); adopting the on-disk "
        "archive - the previous run was probably interrupted after replacing it.",
        ctx.doc_id,
        expected,
        disk_checksum,
    )
    update_archive_checksum(settings.db, ctx.doc_id, disk_checksum)


def _finish(
    api: PaperlessAPI,
    ctx: DocumentContext,
    *,
    success: bool,
    note: str = "",
) -> None:
    """Swap the trigger tag for the outcome tag; keep trigger on API errors."""
    outcome_tag_name = f"{ctx.trigger_tag_name}{'-success' if success else '-failure'}"
    try:
        outcome_tag_id = api.ensure_tag(outcome_tag_name)
        api.set_tags(
            ctx.doc_id,
            remove=[ctx.trigger_tag_id],
            add=[outcome_tag_id],
            current_tags=ctx.current_tags,
        )
    except Exception:  # noqa: BLE001 - tag API hiccup must not lose the trigger
        log.exception("Could not update tags on document %d; keeping trigger.", ctx.doc_id)
        return
    if not success and note:
        log.error("Document %d failed: %s", ctx.doc_id, note)
