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
import tempfile
import time
from dataclasses import dataclass
from datetime import date
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
from paperless_rearchive.ocr.chandra_engine import ChandraOcrEngine, OcrResult
from paperless_rearchive.ocr.provenance import (
    MIXED,
    SCANNED,
    TEXT_BASED,
    UNKNOWN,
    PdfProvenance,
    classify_pdf,
)


def _format_size(size_bytes: int) -> str:
    """Format file size in human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"

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
    #: ``re-ocr-force`` modifier present: bypass the provenance gate and OCR
    #: every page (explicit operator override).
    force: bool = False
    #: Id of the force modifier tag, removed together with the trigger.
    force_tag_id: int | None = None

    def removal_tag_ids(self) -> list[int]:
        """Tags to strip when the trigger is swapped for an outcome tag."""
        ids = [self.trigger_tag_id]
        if self.force_tag_id is not None and self.force_tag_id not in ids:
            ids.append(self.force_tag_id)
        return ids


#: Suffixes the OCR engine can actually render (PyMuPDF). Everything else
#: was text-extracted at ingest and has no OCR text layer to re-run.
_OCRABLE_SUFFIXES = {
    ".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".gif",
}


def _skip_not_ocrable(
    settings: Settings,
    api: PaperlessAPI,
    ctx: DocumentContext,
    original: Path,
) -> None:
    """No-op outcome for a non-OCR-able original (Office document etc.).

    Paperless ingests these via Tika/Gotenberg: the content is extracted
    text, never OCR, so there is no OCR text layer to re-run. Resolve the
    trigger cleanly (success + ``re-ocr-skipped`` marker) instead of
    failing 3 cycles and escalating to ``<trigger>-failure``.
    """
    note = (
        f"Re-OCR skipped ({ctx.trigger_tag_name}): original is "
        f"{original.name!r} ({original.suffix.lower()}) - digital-born, "
        f"not PDF. Its text was extracted at ingest (Tika/Gotenberg), "
        f"not OCR - nothing to re-run."
    )
    log.info("Document %d: %s", ctx.doc_id, note.replace("\n", " | "))
    if settings.dry_run:
        log.info(
            "DRY-RUN document %d: would skip non-OCR-able original (trigger tag kept).",
            ctx.doc_id,
        )
        return
    skipped_tag_id = api.ensure_tag("re-ocr-skipped")
    _finish(api, ctx, success=True, note=note, settings=settings, extra_tags=[skipped_tag_id])


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
    log.debug("Document %d: retrieved from API, title=%r", ctx.doc_id, doc.get("title"))

    with tempfile.TemporaryDirectory(prefix=f"rearch-{ctx.doc_id}-") as tmp:
        tmp_dir = Path(tmp)
        original = api.download_original(ctx.doc_id, tmp_dir)
        log.info(
            "Document %d: downloaded original to %s (%s)",
            ctx.doc_id,
            original,
            original.stat().st_size,
        )
        log.debug("Document %d: original file size=%d bytes, mime=%s",
                  ctx.doc_id, original.stat().st_size,
                  original.suffix.lower())

        # Only PDFs and raster images carry an OCR text layer to re-run.
        # Anything else (Office documents, email, ...) was text-extracted at
        # ingest by paperless (Tika/Gotenberg) - re-OCR does not apply.
        # Resolve the trigger cleanly instead of crashing (observed on doc
        # 5080, an .xls original: 3 failed cycles + failure escalation).
        if original.suffix.lower() not in _OCRABLE_SUFFIXES:
            _skip_not_ocrable(settings, api, ctx, original)
            return

        # Layer 1 of the OCR strategy: per-page provenance (born-digital vs
        # scanned vs mixed). The verdict routes pages to OCR and, for a
        # born-digital document, stops the run before anything is written.
        provenance: PdfProvenance | None = None
        if settings.pdf_provenance == "on" and original.suffix.lower() == ".pdf":
            provenance = classify_pdf(original, max_pages=settings.provenance_max_pages)
            log.info(
                "Document %d: provenance=%s%s",
                ctx.doc_id,
                provenance.summary(),
                " (forced)" if ctx.force else "",
            )
            # Per-page matrix: which page is native vs needs OCR.
            if provenance.kind in (TEXT_BASED, SCANNED, MIXED):
                page_labels: list[str] = [str(p) for p in range(1, provenance.page_count + 1)]
                keep_marks = [
                    "x" if p not in provenance.pages_needing_ocr else "-"
                    for p in range(1, provenance.page_count + 1)
                ]
                ocr_marks = [
                    "x" if p in provenance.pages_needing_ocr else "-"
                    for p in range(1, provenance.page_count + 1)
                ]
                log.info(
                    "Document %d: page matrix:\n"
                    "  Page: [%s]\n"
                    "  Keep: [%s]\n"
                    "  OCR:  [%s]",
                    ctx.doc_id,
                    " ".join(f"{p:>3}" for p in page_labels),
                    " ".join(f"{m:>3}" for m in keep_marks),
                    " ".join(f"{m:>3}" for m in ocr_marks),
                )

            if ctx.force:
                log.warning(
                    "Document %d: modifier tag %r present - provenance gate "
                    "bypassed, every page will be OCR'd",
                    ctx.doc_id,
                    settings.force_tag,
                )
            elif provenance.kind == TEXT_BASED and settings.skip_born_digital:
                _preserve_born_digital(settings, api, ctx, provenance)
                return
            elif provenance.kind == UNKNOWN:
                log.warning(
                    "Document %d: provenance unknown - falling back to "
                    "mode-driven behaviour (REARCHIVE_OCR_MODE=%s)",
                    ctx.doc_id,
                    settings.ocr_mode,
                )

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
                # A DB failure here is transient: let it propagate so the
                # poller keeps the trigger tag and retries next cycle - the
                # same treatment fetch_archive_filename above gets.
                archive_checksum = fetch_archive_checksum(settings.db, ctx.doc_id)
                log.debug(
                    "Document %d: archive checksum from DB=%s",
                    ctx.doc_id,
                    archive_checksum,
                )
                try:
                    verify_current_checksum(archive_path, archive_checksum)
                    log.debug(
                        "Document %d: archive checksum verified (DB %s == disk)",
                        ctx.doc_id,
                        archive_checksum,
                    )
                except ArchiveReplaceError as e:
                    log.warning(
                        "Document %d: archive checksum mismatch, attempting repair",
                        ctx.doc_id,
                    )
                    try:
                        _repair_checksum_drift(settings, ctx, archive_path, archive_checksum, e)
                    except ArchiveReplaceError as repair_error:
                        # Unrepairable drift is a decision: fail the document
                        # (never retried). A RuntimeError from the repair's
                        # own DB update propagates instead (transient ->
                        # trigger kept), keeping DB handling consistent.
                        if settings.dry_run:
                            log.error(
                                "DRY-RUN document %d: archive check failed: %s",
                                ctx.doc_id,
                                repair_error,
                            )
                            return
                        _finish(
                            api, ctx, success=False, note=str(repair_error), settings=settings
                        )
                        return

        # Use unified Chandra OCR engine
        log.info(
            "Document %d: initializing ChandraOcrEngine (concurrency=%d, max_pages=%d)",
            ctx.doc_id,
            settings.concurrency,
            settings.max_pages,
        )
        ocr_started = time.monotonic()
        engine = ChandraOcrEngine(
            server_url=provider.server_url,
            model_name=provider.model_name,
            api_key=provider.api_key,
            content_format=provider.content_format,
            max_output_tokens=provider.max_output_tokens,
            language=settings.ocr_language,
            dpi=settings.ocr_dpi,
            concurrency=settings.concurrency,
            max_pages=settings.max_pages,
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

        log.info(
            "Document %d: starting unified OCR (produce_pdf=%s, archive_mode=%s)",
            ctx.doc_id,
            produce_pdf,
            ctx.archive_mode,
        )
        result = engine.ocr_document(
            original,
            produce_pdf=produce_pdf,
            output_pdf_path=output_pdf_path,
            settings=settings,
            provenance=provenance,
            force=ctx.force,
        )

        log.info(
            "Document %d: OCR complete - %d pages, %d succeeded, %d failed",
            ctx.doc_id,
            result.page_count,
            result.success_count,
            len(result.error_pages),
        )
        if result.pdf_path:
            log.debug("Document %d: PDF/A produced at %s", ctx.doc_id, result.pdf_path)

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
            _finish(api, ctx, success=False, note=message, settings=settings)
            return

        if settings.dry_run:
            would = f"content ({len(content)} chars)"
            if result.pdf_path is not None:
                would += f" and archive {result.pdf_path}"
            log.info("DRY-RUN document %d: would write %s. Trigger tag kept.", ctx.doc_id, would)
            log.debug("Document %d: DRY-RUN - no actual writes performed", ctx.doc_id)
            return

        api.patch_content(ctx.doc_id, content)
        log.info(
            "Document %d: Updated content field with %d chars of OCR markdown",
            ctx.doc_id,
            len(content),
        )

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
        if provenance is not None and provenance.kind == UNKNOWN:
            try:
                extra_tags.append(api.ensure_tag("re-ocr-detection-unknown"))
            except Exception:
                log.exception(
                    "Could not add re-ocr-detection-unknown tag for document %d", ctx.doc_id
                )

        size_ratio: float | None = None
        if result.pdf_path is not None and archive_path is not None:
            log.info(
                "Document %d: replacing archive %s with new PDF/A %s",
                ctx.doc_id,
                archive_path,
                result.pdf_path,
            )
            old_checksum = fetch_archive_checksum(settings.db, ctx.doc_id)
            log.debug(
                "Document %d: current DB archive_checksum=%s",
                ctx.doc_id,
                old_checksum,
            )
            # Get original archive size before replacement
            old_size = archive_path.stat().st_size if archive_path.exists() else 0

            checksum = replace_archive(
                archive_path,
                result.pdf_path,
                backup_dir=settings.backup_dir,
                archive_dir=settings.archive_dir,
            )
            log.info(
                "Document %d: archive replaced, new SHA-256=%s",
                ctx.doc_id,
                checksum,
            )

            # Get new archive size after replacement
            new_size = result.pdf_path.stat().st_size

            # Ratio + pct for concise logging, e.g. 624.5 KB to 625.1 KB (1.001x, +0.1%)
            size_diff = new_size - old_size
            if old_size > 0:
                size_ratio = new_size / old_size
                size_change_pct = (size_diff / old_size) * 100
            else:
                size_ratio = None
                size_change_pct = 0.0

            log.debug(
                "Document %d: archive size changed from %s to %s (%.3fx, %+.1f%%)",
                ctx.doc_id,
                _format_size(old_size),
                _format_size(new_size),
                size_ratio if size_ratio is not None else float("inf"),
                size_change_pct,
            )

            # Check for drastic size changes
            if old_size > 0 and abs(size_change_pct) > 50:
                direction = "increased" if size_diff > 0 else "decreased"
                log.warning(
                    "Document %d: archive size %s by %.1f%% (%.3fx, %s → %s)",
                    ctx.doc_id,
                    direction,
                    abs(size_change_pct),
                    size_ratio if size_ratio is not None else float("inf"),
                    _format_size(old_size),
                    _format_size(new_size),
                )
                if size_diff > 0:
                    log.warning(
                        "Document %d: significant size increase may indicate fallback to force_ocr or other issues",
                        ctx.doc_id,
                    )

            update_archive_checksum(settings.db, ctx.doc_id, checksum)
            log.info(
                "Document %d: updated documents_document.archive_checksum to %s",
                ctx.doc_id,
                checksum,
            )
            log.debug("Document %d: archive replacement complete, new checksum=%s",
                      ctx.doc_id, checksum)

        ocr_seconds = time.monotonic() - ocr_started
        _write_provenance(
            settings,
            api,
            ctx,
            provider,
            result,
            size_ratio=size_ratio,
            ocr_seconds=ocr_seconds,
            provenance=provenance,
        )

        # Finish: swap trigger tag for outcome tag
        outcome_tag_name = f"{ctx.trigger_tag_name}{'-success' if result.page_count > 0 else '-failure'}"
        try:
            outcome_tag_id = api.ensure_tag(outcome_tag_name)
            all_tags_to_add = [outcome_tag_id] + extra_tags
            api.set_tags(
                ctx.doc_id,
                remove=ctx.removal_tag_ids(),
                add=all_tags_to_add,
                current_tags=ctx.current_tags,
            )
        except Exception:
            log.exception("Could not update tags on document %d; keeping trigger.", ctx.doc_id)
            return

    log.info(
        "Document %d re-OCR complete: %d pages, %d succeeded, %d failed%s",
        ctx.doc_id,
        result.page_count,
        result.success_count,
        len(result.error_pages),
        _page_action_summary(result),
    )


def _page_action_summary(result: "object") -> str:
    """Human-readable per-page breakdown for the final run log.

    Groups pages by the action the engine took, e.g.
    ``; pages: 1 native (text kept), 2-3 ocr (Chandra re-OCR)`` - so the log
    states explicitly what was done to which page.
    """
    actions: dict[int, str] = getattr(result, "page_actions", {}) or {}
    if not actions:
        return ""

    label = {
        "ocr": "ocr (fresh Chandra layer)",
        "native": "native (text kept)",
        "passthrough": "passthrough (untouched)",
        "skipped": "skipped (page cap)",
        "error": "error",
    }

    def _ranges(pages: list[int]) -> str:
        """1,2,3,7 -> '1-3,7'."""
        if not pages:
            return ""
        runs: list[tuple[int, int]] = []
        start = prev = pages[0]
        for p in pages[1:]:
            if p == prev + 1:
                prev = p
            else:
                runs.append((start, prev))
                start = prev = p
        runs.append((start, prev))
        return ",".join(f"{a}" if a == b else f"{a}-{b}" for a, b in runs)

    order = ["ocr", "native", "passthrough", "error", "skipped"]
    groups: dict[str, list[int]] = {}
    for page, action in actions.items():
        groups.setdefault(action, []).append(page)

    parts = []
    for action in order:
        pages = sorted(groups.get(action, []))
        if pages:
            parts.append(f"{_ranges(pages)} {label.get(action, action)}")
    leftover = sorted(set(groups) - set(order))
    for action in leftover:
        parts.append(f"{_ranges(sorted(groups[action]))} {action}")
    return "; pages: " + ", ".join(parts)


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
        "Document %d: archive checksum drift detected (DB %s != disk %s)",
        ctx.doc_id,
        expected,
        disk_checksum,
    )
    log.info(
        "Document %d: adopting on-disk archive bytes (previous run likely interrupted)",
        ctx.doc_id,
    )
    update_archive_checksum(settings.db, ctx.doc_id, disk_checksum)
    log.info(
        "Document %d: updated archive_checksum from %s to %s",
        ctx.doc_id,
        expected,
        disk_checksum,
    )


def _write_provenance(
    settings: Settings,
    api: PaperlessAPI,
    ctx: DocumentContext,
    provider: OcrProviderPlugin,
    result: OcrResult,
    *,
    size_ratio: float | None,
    ocr_seconds: float,
    provenance: PdfProvenance | None = None,
) -> None:
    """Record OCR run provenance: custom fields (latest state wins) + audit note.

    Writes ``OCR engine`` / ``OCR date`` / ``OCR pages`` on every success,
    plus ``OCR archive ratio`` for ``re-ocr-all``. Definitions are ensured
    once per document (idempotent); values ride a single PATCH. Custom
    fields only keep the latest run, so a human-readable audit note is also
    appended to the document's paperless note history (append-only) for the
    full per-run trail. Skipped in dry-run mode and when
    ``REARCHIVE_WRITE_PROVENANCE=false``. Failures here must never fail the
    document - the tags already carry the verdict.
    """
    if settings.dry_run or not settings.write_provenance:
        return
    ok_pages = result.page_count - len(result.error_pages)
    pages_value = f"{ok_pages}/{result.page_count} ok"

    # "ok" counts pages without errors, but not all of them were OCR'd: for
    # mixed documents some pages pass through untouched (born-digital or
    # outside --pages), and a skip/off run OCRs nothing at all. A bare
    # "3/3 ok" would then overstate what happened - spell the split out.
    # Errored pages are not counted here; they are listed by number in the
    # ``errors:`` suffix below.
    actions = getattr(result, "page_actions", {}) or {}
    counts: dict[str, int] = {}
    for action in actions.values():
        if action == "ocr":
            key = "ocr"
        elif action in ("passthrough", "native"):
            key = "passthrough"
        elif action == "skipped":
            key = "skipped"
        else:  # "error" and anything unknown: covered by the errors suffix
            continue
        counts[key] = counts.get(key, 0) + 1
    detail: list[str] = []
    if counts.get("ocr"):
        detail.append(f"{counts['ocr']} ocr")
    if counts.get("passthrough"):
        detail.append(f"{counts['passthrough']} passthrough")
    if counts.get("skipped"):
        detail.append(f"{counts['skipped']} skipped")
    if len(result.error_pages) > 1 or (
        len(result.error_pages) == 1 and result.error_pages != [0]
    ):
        shown = ",".join(str(n) for n in result.error_pages[:8])
        if len(result.error_pages) > 8:
            shown += "\u2026"
        detail.append(f"errors: {shown}")
    if detail:
        pages_value += f" ({', '.join(detail)})"
    if result.page_count and ok_pages == 0:
        pages_value = "0 ok - failed scan?"
    note_lines = [
        f"Re-OCR {'complete' if ok_pages > 0 else 'finished without usable pages'} ({ctx.trigger_tag_name})",
        f"Engine: {provider.model_name}",
        f"Pages: {pages_value}",
    ]
    if provenance is not None:
        note_lines.append(f"Detected: {provenance.summary()}")
    if ctx.archive_mode and size_ratio is not None:
        note_lines.append(f"Archive size ratio: {size_ratio:.3f}")
    note_lines.append(f"OCR duration: {ocr_seconds:.1f}s")
    values_spec: list[tuple[str, str, object]] = [
        ("OCR engine", "string", provider.model_name[:128]),
        ("OCR date", "date", date.today().isoformat()),
        ("OCR pages", "string", pages_value[:128]),
    ]
    if ctx.archive_mode and size_ratio is not None:
        values_spec.append(("OCR archive ratio", "float", round(size_ratio, 3)))
    try:
        field_ids = api.ensure_provenance_fields()
        values = [
            {"field": field_ids[name], "value": value} for name, _, value in values_spec
        ]
        api.set_custom_fields(ctx.doc_id, values)
        _write_audit_note(api, ctx, "\n".join(note_lines))
        log.info(
            "Document %d: provenance written (%s, %s, %s) in %.1fs of OCR",
            ctx.doc_id,
            provider.model_name,
            pages_value,
            (
                f"ratio {size_ratio:.3f}"
                if size_ratio is not None
                else "content-only"
            ),
            ocr_seconds,
        )
    except Exception:  # noqa: BLE001 - provenance is informational only
        log.exception(
            "Document %d: could not write provenance custom fields; tags already updated.",
            ctx.doc_id,
        )


def _write_audit_note(api: PaperlessAPI, ctx: DocumentContext, note: str) -> None:
    """Best-effort append to the document's paperless note history.

    The caller gates on ``REARCHIVE_WRITE_PROVENANCE``; a failure here must
    never fail the document - the tags already carry the verdict.
    """
    try:
        api.add_note(ctx.doc_id, note)
        log.info(
            "Document %d: audit note appended (%d line(s))",
            ctx.doc_id,
            note.count("\n") + 1,
        )
    except Exception:  # noqa: BLE001 - informational only
        log.exception("Document %d: could not append audit note.", ctx.doc_id)


def _preserve_born_digital(
    settings: Settings,
    api: PaperlessAPI,
    ctx: DocumentContext,
    provenance: PdfProvenance,
) -> None:
    """No-op outcome for a born-digital document: keep its native text.

    Layer 1 of the OCR strategy: re-OCR'ing a PDF whose text is *visible,
    native* content (wkhtmltopdf, Word, LaTeX, ...) destroys it - the LLM
    describes the page layout instead of reading it. Nothing is written:
    neither ``content`` nor the archive, and no database connection is
    opened. The trigger is swapped for ``<trigger>-success`` plus the
    ``re-ocr-preserved`` tag, and an audit note records the verdict.
    """
    note = (
        f"Re-OCR skipped ({ctx.trigger_tag_name}): born-digital PDF - native "
        f"text preserved.\nDetected: {provenance.summary()}"
    )
    log.info("Document %d: %s", ctx.doc_id, note.replace("\n", " | "))
    if settings.dry_run:
        log.info(
            "DRY-RUN document %d: would preserve native text (trigger tag kept).",
            ctx.doc_id,
        )
        return

    extra_tags: list[int] = []
    if settings.preserved_tag:
        try:
            extra_tags.append(api.ensure_tag(settings.preserved_tag))
        except Exception:  # noqa: BLE001 - tag is informational
            log.exception(
                "Could not ensure preserved tag %r for document %d",
                settings.preserved_tag,
                ctx.doc_id,
            )

    _finish(api, ctx, success=True, note=note, settings=settings, extra_tags=extra_tags)


def _finish(
    api: PaperlessAPI,
    ctx: DocumentContext,
    *,
    success: bool,
    note: str = "",
    settings: Settings | None = None,
    extra_tags: list[int] | None = None,
) -> None:
    """Swap the trigger tag for the outcome tag; keep trigger on API errors.

    On failure an audit note is appended (same gate as custom-field
    provenance) so the run outcome survives in paperless history.
    """
    outcome_tag_name = f"{ctx.trigger_tag_name}{'-success' if success else '-failure'}"
    log.info(
        "Document %d: swapping trigger tag %r for outcome tag %r (success=%s)",
        ctx.doc_id,
        ctx.trigger_tag_name,
        outcome_tag_name,
        success,
    )
    try:
        outcome_tag_id = api.ensure_tag(outcome_tag_name)
        log.debug(
            "Document %d: outcome tag %r has id %d",
            ctx.doc_id,
            outcome_tag_name,
            outcome_tag_id,
        )
        extra = extra_tags or []
        api.set_tags(
            ctx.doc_id,
            remove=ctx.removal_tag_ids(),
            add=[outcome_tag_id, *extra],
            current_tags=ctx.current_tags,
        )
        log.info(
            "Document %d: tag swap complete - removed trigger %d (%s), added outcome %d (%s)",
            ctx.doc_id,
            ctx.trigger_tag_id,
            ctx.trigger_tag_name,
            outcome_tag_id,
            outcome_tag_name,
        )
        if extra:
            log.info(
                "Document %d: also added %d extra tag(s): %s",
                ctx.doc_id,
                len(extra),
                extra,
            )
    except Exception:  # noqa: BLE001 - tag API hiccup must not lose the trigger
        log.exception("Could not update tags on document %d; keeping trigger.", ctx.doc_id)
        return
    if not success and note:
        log.error("Document %d failed: %s", ctx.doc_id, note)
    if note and settings is not None and not settings.dry_run and settings.write_provenance:
        _write_audit_note(
            api,
            ctx,
            note
            if success
            else (
                f"Re-OCR failed ({ctx.trigger_tag_name}) for document "
                f"{ctx.doc_id}\n{note}"
            ),
        )
