"""Per-page provenance detection for PDF originals (pdf-inspector).

Answers layer 1 of the OCR strategy: *does a page have trustworthy native
text, or is it an OCR candidate?* The OCR mode (layer 2, ``ingest_args``)
then decides how an existing text layer is treated for the pages that are
routed to OCR.

Measurement notes (pdf-inspector 1.20.0, verified live on this instance):

* ``extract_pages_markdown`` is the authoritative signal, **not** the fast
  document-level ``detect_pdf``/``classify_pdf``: the latter reports
  ``text_based`` for a scanned page carrying an invisible OCR overlay and
  ``image_based`` for a document mixing a text page with a scanned page.
  The per-page ``needs_ocr`` flag is correct in both cases.
* ``PageMarkdown.page`` is 0-indexed; this module normalises everything to
  1-indexed page numbers (as ``PagesExtractionResult.pages_needing_ocr`` and
  ``detect_pdf.pages_needing_ocr`` already are).
* Every pdf-inspector entry point takes a ``str`` path; a ``Path`` raises
  ``TypeError``.

When pdf-inspector is unavailable or fails, the historical heuristics
(``pdf_born_digital_text`` + ``has_visible_text_content``) are used so the
sidecar degrades to its pre-detection behaviour instead of crashing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from paperless_rearchive.ocr.ingest_args import (
    has_visible_text_content,
    pdf_born_digital_text,
)

log = logging.getLogger(__name__)

TEXT_BASED = "text_based"
SCANNED = "scanned"
MIXED = "mixed"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class PdfProvenance:
    """Aggregated provenance verdict for one PDF original.

    Attributes:
        kind: ``text_based`` (no page needs OCR), ``scanned`` (every page
            needs OCR), ``mixed`` (some do), or ``unknown`` (detection could
            not decide - callers fall back to mode-driven behaviour).
        page_count: Total pages in the document.
        pages_needing_ocr: 1-indexed pages routed to OCR.
        native_markdown: 1-indexed pages with native text, mapped to the
            markdown pdf-inspector extracted (may be empty when the source
            is a heuristic fallback).
        confidence: Classifier confidence, when reported (0.0 otherwise).
        source: ``pdf_inspector``, ``fallback``, or ``error``.
    """

    kind: str
    page_count: int
    pages_needing_ocr: frozenset[int] = frozenset()
    native_markdown: dict[int, str] = field(default_factory=dict)
    confidence: float = 0.0
    source: str = "pdf_inspector"

    @property
    def native_pages(self) -> frozenset[int]:
        """1-indexed pages that keep their native text (not OCR'd)."""
        return frozenset(range(1, self.page_count + 1)) - self.pages_needing_ocr

    def summary(self) -> str:
        """One-line human-readable form for logs and audit notes."""
        return (
            f"{self.kind} ({len(self.pages_needing_ocr)} OCR / "
            f"{len(self.native_markdown)} native of {self.page_count} page(s); "
            f"source={self.source})"
        )


def classify_pdf(path: Path, *, max_pages: int = 0) -> PdfProvenance:
    """Classify a PDF original as born-digital, scanned, mixed, or unknown.

    Never raises: a pdf-inspector failure falls back to the historical
    ``pdftotext``/PyMuPDF heuristics, and a heuristic failure yields
    ``unknown`` so the caller can keep its mode-driven behaviour.

    Args:
        path: Path to the PDF original (the *immutable* file, never the
            archive - an already-OCR'd archive fools the document-level
            classifier).
        max_pages: Inspect at most this many pages (0 = all). Mirrors
            ``REARCHIVE_MAX_PAGES``; pages beyond the cap are only assumed to
            need OCR when every inspected page did.
    """
    try:
        provenance = _classify_with_inspector(path, max_pages=max_pages)
        if provenance is not None:
            return provenance
    except Exception:  # noqa: BLE001 - detection must never fail a document
        log.warning(
            "pdf-inspector classification failed for %s; using heuristic provenance",
            path,
            exc_info=True,
        )
    return _classify_with_heuristics(path, max_pages=max_pages)


def _classify_with_inspector(path: Path, *, max_pages: int) -> PdfProvenance | None:
    """Run pdf-inspector's per-page extraction; ``None`` when unusable."""
    try:
        import pdf_inspector
    except ImportError:
        log.warning("pdf-inspector not installed; using heuristic provenance detection")
        return None

    total_pages = _pdf_page_count(path)
    pages_arg: list[int] | None = None
    if max_pages > 0:
        limit = min(max_pages, total_pages) if total_pages else max_pages
        pages_arg = list(range(limit))

    # ``Path`` is rejected by the extension module; always pass ``str``.
    result = pdf_inspector.extract_pages_markdown(str(path), pages=pages_arg)
    pages = list(getattr(result, "pages", None) or [])
    if not pages:
        log.warning("pdf-inspector returned no pages for %s; using heuristics", path)
        return None

    if not total_pages:
        total_pages = int(getattr(result, "page_count", 0) or len(pages)) or len(pages)

    native_markdown: dict[int, str] = {}
    needing_ocr: set[int] = set()
    for page in pages:
        # PageMarkdown.page is 0-indexed -> normalise to 1-indexed.
        page_num = int(getattr(page, "page", 0)) + 1
        markdown = getattr(page, "markdown", "") or ""
        needs_ocr = bool(getattr(page, "needs_ocr", False))
        if not needs_ocr and not markdown.strip():
            # Classifier says native, but there is nothing to keep: route it
            # to OCR rather than silently dropping a real page's content.
            log.debug("Page %d: native but empty markdown; routing to OCR", page_num)
            needs_ocr = True
        if needs_ocr:
            needing_ocr.add(page_num)
        else:
            native_markdown[page_num] = markdown

    capped = pages_arg is not None and total_pages > len(pages)
    if capped and needing_ocr == set(range(1, len(pages) + 1)):
        # Every inspected page is scanned: treat the un-inspected tail as
        # scanned too (a full-scan document stays ``scanned``, not ``mixed``).
        needing_ocr |= set(range(len(pages) + 1, total_pages + 1))

    if needing_ocr and len(needing_ocr) >= total_pages:
        kind = SCANNED
    elif needing_ocr:
        kind = MIXED
    else:
        kind = TEXT_BASED

    return PdfProvenance(
        kind=kind,
        page_count=total_pages,
        pages_needing_ocr=frozenset(needing_ocr),
        native_markdown=native_markdown,
        confidence=float(getattr(result, "confidence", 0.0) or 0.0),
        source="pdf_inspector",
    )


def _classify_with_heuristics(path: Path, *, max_pages: int) -> PdfProvenance:
    """Pre-pdf-inspector heuristics: document-level, never raises.

    ``text_based`` requires *both* a text layer (``pdf_born_digital_text``)
    and visible native text (``has_visible_text_content``); otherwise the
    document is treated as scanned. Native per-page markdown is unavailable,
    so callers fall back to the PDF's own text extraction.
    """
    try:
        total = _pdf_page_count(path)
        if pdf_born_digital_text(path) and has_visible_text_content(path):
            return PdfProvenance(kind=TEXT_BASED, page_count=total, source="fallback")
        if total <= 0:
            return PdfProvenance(kind=UNKNOWN, page_count=0, source="error")
        return PdfProvenance(
            kind=SCANNED,
            page_count=total,
            pages_needing_ocr=frozenset(range(1, total + 1)),
            source="fallback",
        )
    except Exception:  # noqa: BLE001 - never fail a document on detection
        log.warning("heuristic provenance detection failed for %s", path, exc_info=True)
        return PdfProvenance(kind=UNKNOWN, page_count=0, source="error")


def _pdf_page_count(path: Path) -> int:
    """Total page count via PyMuPDF (0 when the file cannot be opened)."""
    try:
        import fitz  # PyMuPDF

        with fitz.open(path) as doc:
            return len(doc)
    except Exception:  # noqa: BLE001
        log.debug("could not read page count from %s", path, exc_info=True)
        return 0
