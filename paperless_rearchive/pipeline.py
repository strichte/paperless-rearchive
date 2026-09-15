"""Per-document re-OCR pipeline."""

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
    replace_archive,
    verify_current_checksum,
)
from paperless_rearchive.ocr.runner import guess_mime_type, is_image, run_ocr

if TYPE_CHECKING:
    from paperless_rearchive.config import Settings
    from paperless_rearchive.ocr.base import OcrProviderPlugin
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
    settings: "Settings",
    api: "PaperlessAPI",
    provider: "OcrProviderPlugin",
    ctx: DocumentContext,
) -> None:
    """Re-OCR one document; never raises for expected failure modes.

    Tag outcomes:
    * success  -> trigger removed, ``<trigger>-success`` added
    * failure  -> trigger removed, ``<trigger>-failure`` added
    * retry    -> trigger kept (transient OCR error); nothing else touched
    """
    doc = api.document(ctx.doc_id)
    mime = guess_mime_type(Path(doc.get("original_file_name") or "x.bin"))

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
            elif not mime.startswith("application/pdf") and is_image(mime):
                log.info("Document %d is an image; content-only mode.", ctx.doc_id)
                do_archive = False
            else:
                archive_path = settings.archive_dir / archive_filename
                try:
                    archive_checksum = fetch_archive_checksum(settings.db, ctx.doc_id)
                    verify_current_checksum(archive_path, archive_checksum)
                except (ArchiveReplaceError, RuntimeError) as e:
                    if settings.dry_run:
                        log.error(
                            "DRY-RUN document %d: archive check failed: %s", ctx.doc_id, e
                        )
                        return
                    _finish(api, ctx, success=False, note=str(e))
                    return

        new_pdf = tmp_dir / "archive.pdf"
        sidecar = tmp_dir / "sidecar.txt"
        try:
            run_ocr(original, new_pdf, sidecar, provider, settings)
        except RuntimeError as e:
            log.warning("Document %d: OCR failed transiently: %s", ctx.doc_id, e)
            return  # keep trigger tag for retry

        content = _read_content(sidecar, new_pdf)

        if settings.dry_run:
            would = f"content ({len(content)} chars)"
            if archive_path is not None:
                would += f" and archive {archive_path}"
            log.info("DRY-RUN document %d: would write %s. Trigger tag kept.", ctx.doc_id, would)
            return

        api.patch_content(ctx.doc_id, content)

        if archive_path is not None:
            checksum = replace_archive(archive_path, new_pdf)
            update_archive_checksum(settings.db, ctx.doc_id, checksum)

    _finish(api, ctx, success=True)
    log.info("Document %d re-OCR complete.", ctx.doc_id)


def _read_content(sidecar: Path, archive_pdf: Path) -> str:
    """Markdown sidecar text; fall back to pdftotext on the new PDF."""
    if sidecar.is_file():
        content = sidecar.read_text(encoding="utf-8").strip()
        if content:
            return content
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no user input
            ["pdftotext", str(archive_pdf), "-"],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


def _finish(
    api: "PaperlessAPI",
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
