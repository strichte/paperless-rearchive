"""Tests for per-page PDF provenance detection (ocr/provenance.py).

The synthetic documents reproduce the cases that motivated the module:
an original born-digital PDF, a raw scan, a scan carrying an *invisible*
OCR text layer (the case document-level classifiers get wrong), and a
document mixing a native text page with a scanned page.
"""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from paperless_rearchive.ocr.provenance import (
    MIXED,
    SCANNED,
    TEXT_BASED,
    UNKNOWN,
    classify_pdf,
)


def _write_lines(page: fitz.Page, prefix: str) -> None:
    """Write enough text to cover >5% of the page (has_visible_text_content)."""
    for line in range(20):
        page.insert_text((72, 72 + line * 20), f"{prefix} line {line + 1}. " * 4)


def _make_text_pdf(path: Path, pages: int = 1) -> Path:
    """Born-digital PDF: one visible native-text page per requested page."""
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        _write_lines(page, f"Native digital text page {i + 1}")
    doc.save(path)
    doc.close()
    return path


def _make_image_pdf(path: Path, pages: int = 1, *, overlay_text: str = "") -> Path:
    """Scan-like PDF: text rasterised into a full-page image.

    ``overlay_text`` adds an invisible (render mode 3) text layer on top -
    i.e. what a previously OCR'd scan looks like.
    """
    src = fitz.open()
    out = fitz.open()
    for i in range(pages):
        source_page = src.new_page(width=595, height=842)
        _write_lines(source_page, f"Baked-in scan text page {i + 1}")
        pix = source_page.get_pixmap(dpi=150)
        page = out.new_page(width=595, height=842)
        page.insert_image(fitz.Rect(0, 0, 595, 842), pixmap=pix)
        if overlay_text:
            page.insert_text((72, 72), overlay_text, render_mode=3)
    out.save(path)
    out.close()
    src.close()
    return path


def _make_mixed_pdf(path: Path) -> Path:
    """Page 1 native text, page 2 a scan image."""
    out = fitz.open()
    page = out.new_page(width=595, height=842)
    _write_lines(page, "Native digital text page")
    src = fitz.open()
    source_page = src.new_page(width=595, height=842)
    _write_lines(source_page, "Baked-in scan text page")
    pix = source_page.get_pixmap(dpi=150)
    scan_page = out.new_page(width=595, height=842)
    scan_page.insert_image(fitz.Rect(0, 0, 595, 842), pixmap=pix)
    src.close()
    out.save(path)
    out.close()
    return path


def test_born_digital_is_text_based(tmp_path: Path) -> None:
    provenance = classify_pdf(_make_text_pdf(tmp_path / "born.pdf"))
    assert provenance.kind == TEXT_BASED
    assert provenance.pages_needing_ocr == frozenset()
    assert provenance.page_count == 1
    assert provenance.native_markdown.get(1, "").strip()
    assert provenance.source == "pdf_inspector"


def test_scanned_is_scanned(tmp_path: Path) -> None:
    provenance = classify_pdf(_make_image_pdf(tmp_path / "scan.pdf", pages=2))
    assert provenance.kind == SCANNED
    assert provenance.pages_needing_ocr == frozenset({1, 2})
    assert provenance.native_markdown == {}
    assert provenance.native_pages == frozenset()


def test_invisible_ocr_overlay_is_still_scanned(tmp_path: Path) -> None:
    """A scan with an invisible OCR text layer must still need OCR.

    This is the case the document-level ``detect_pdf`` gets wrong (it reports
    ``text_based``); the per-page signal keeps it a re-OCR candidate.
    """
    path = _make_image_pdf(
        tmp_path / "ocr_scan.pdf", overlay_text="INVISIBLE OCR TEXT LAYER. " * 40
    )
    provenance = classify_pdf(path)
    assert provenance.kind == SCANNED
    assert provenance.pages_needing_ocr == frozenset({1})


def test_mixed_provenance(tmp_path: Path) -> None:
    provenance = classify_pdf(_make_mixed_pdf(tmp_path / "mixed.pdf"))
    assert provenance.kind == MIXED
    assert provenance.pages_needing_ocr == frozenset({2})
    assert provenance.native_pages == frozenset({1})
    assert provenance.native_markdown.get(1, "").strip()


def test_max_pages_caps_inspection_but_keeps_total(tmp_path: Path) -> None:
    """A capped full scan stays SCANNED; page_count still reports every page."""
    path = _make_image_pdf(tmp_path / "scan3.pdf", pages=3)
    provenance = classify_pdf(path, max_pages=1)
    assert provenance.page_count == 3
    assert provenance.kind == SCANNED
    assert provenance.pages_needing_ocr == frozenset({1, 2, 3})


def test_fallback_when_inspector_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No pdf-inspector -> historical heuristics, tagged with source='fallback'."""
    path = _make_text_pdf(tmp_path / "born.pdf")
    monkeypatch.setattr(
        "paperless_rearchive.ocr.provenance._classify_with_inspector",
        lambda *a, **kw: None,
    )
    provenance = classify_pdf(path)
    assert provenance.source == "fallback"
    assert provenance.kind == TEXT_BASED


def test_classify_never_raises_on_garbage(tmp_path: Path) -> None:
    path = tmp_path / "garbage.pdf"
    path.write_bytes(b"this is not a pdf")
    provenance = classify_pdf(path)
    assert provenance.kind == UNKNOWN


def test_summary_reports_counts() -> None:
    from paperless_rearchive.ocr.provenance import PdfProvenance

    provenance = PdfProvenance(
        kind=MIXED,
        page_count=4,
        pages_needing_ocr=frozenset({2, 3}),
        native_markdown={1: "a", 4: "b"},
        source="pdf_inspector",
    )
    summary = provenance.summary()
    assert "mixed" in summary
    assert "2 OCR" in summary
    assert "2 native" in summary
    assert "4 page" in summary
