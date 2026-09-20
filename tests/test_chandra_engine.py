"""Tests for ChandraOcrEngine error semantics.

Transport/server errors (``ChandraClientError`` — retries exhausted at the
client) abort the whole document so the poller keeps the trigger tag and
retries; genuine per-page failures (empty result, garbage parse) become page
errors and fail the document with a partial result.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from paperless_chandra.engine.client import ChandraClientError

from paperless_rearchive.ocr import chandra_engine
from paperless_rearchive.ocr.chandra_engine import ChandraOcrEngine
from paperless_rearchive.ocr.model_check import ModelNotServedError


def _engine(concurrency: int = 1) -> ChandraOcrEngine:
    return ChandraOcrEngine(
        server_url="http://ai:8110/v1",
        model_name="chandra-ocr-2-q8",
        api_key="secret",
        concurrency=concurrency,
    )


@pytest.fixture(autouse=True)
def _no_model_probe(monkeypatch):
    """Keep the /models preflight offline for the local-logic tests.

    The probe itself is covered by tests/test_model_check.py, and the
    abort-before-any-page behaviour by
    test_model_not_served_aborts_before_any_page below.
    """
    monkeypatch.setattr(chandra_engine, "ensure_model_served", lambda *a, **k: None)


def test_transport_error_aborts_document_sequential(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(concurrency=1)
    monkeypatch.setattr(ChandraOcrEngine, "_render_pdf_pages", lambda self, p: [object(), object()])
    calls: list[int] = []

    def fake_ocr_image(image, options):
        calls.append(1)
        raise ChandraClientError("Chandra inference failed after retries")

    monkeypatch.setattr(
        "paperless_rearchive.ocr.chandra_engine.chandra_client.ocr_image", fake_ocr_image
    )

    with pytest.raises(ChandraClientError, match="after retries"):
        engine.ocr_document(tmp_path / "in.pdf")
    assert len(calls) == 1  # first page aborts; not swallowed per page


def test_transport_error_aborts_document_concurrent(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(concurrency=2)
    monkeypatch.setattr(ChandraOcrEngine, "_render_pdf_pages", lambda self, p: [object()] * 3)
    monkeypatch.setattr(
        "paperless_rearchive.ocr.chandra_engine.chandra_client.ocr_image",
        lambda image, options: (_ for _ in ()).throw(ChandraClientError("server down")),
    )

    with pytest.raises(ChandraClientError, match="server down"):
        engine.ocr_document(tmp_path / "in.pdf")


def test_empty_page_result_stays_a_page_error(tmp_path: Path, monkeypatch) -> None:
    """An empty Chandra result (model gave up on a hard page after retries)
    is a page-level failure, not an abort: the document finishes with the
    other pages and is tagged re-ocr-page-errors."""
    engine = _engine(concurrency=1)
    monkeypatch.setattr(ChandraOcrEngine, "_render_pdf_pages", lambda self, p: [object(), object()])
    monkeypatch.setattr(
        "paperless_rearchive.ocr.chandra_engine.chandra_client.ocr_image",
        lambda image, options: "",  # empty raw -> empty page
    )

    result = engine.ocr_document(tmp_path / "in.pdf")

    assert result.has_errors
    assert result.error_pages == [1, 2]
    assert result.markdown.strip() == ""
    assert result.pdf_path is None


# ── model provenance stamp ───────────────────────────────────────────────────


def test_stamp_model_provenance(tmp_path: Path) -> None:
    """The served model name is appended to docinfo /Creator and mirrored
    into the XMP CreatorTool, on top of what ocrmypdf recorded."""
    import pikepdf

    pdf_path = tmp_path / "archive.pdf"
    with pikepdf.new() as pdf:
        pdf.docinfo["/Creator"] = "OCRmyPDF 17.12.1 / OCRmyPDF fpdf2 + Chandra 0.2.0"
        pdf.save(pdf_path)

    engine = _engine()
    engine._stamp_model_provenance(pdf_path)

    with pikepdf.open(pdf_path) as pdf:
        creator = str(pdf.docinfo["/Creator"])
        with pdf.open_metadata() as meta:
            creator_tool = meta.get("xmp:CreatorTool", "")
    assert creator == (
        "OCRmyPDF 17.12.1 / OCRmyPDF fpdf2 + Chandra 0.2.0 [model: chandra-ocr-2-q8]"
    )
    assert creator_tool == creator


def test_stamp_model_provenance_without_model_name(tmp_path: Path) -> None:
    """An empty model name is a no-op (no stray '[model: ]')."""
    import pikepdf

    pdf_path = tmp_path / "archive.pdf"
    with pikepdf.new() as pdf:
        pdf.docinfo["/Creator"] = "OCRmyPDF 17.12.1 / Chandra 0.2.0"
        pdf.save(pdf_path)

    engine = _engine()
    engine.model_name = ""
    engine._stamp_model_provenance(pdf_path)

    with pikepdf.open(pdf_path) as pdf:
        assert str(pdf.docinfo["/Creator"]) == "OCRmyPDF 17.12.1 / Chandra 0.2.0"


# ── provenance-driven content path (layer 1) ─────────────────────────────────


def _two_page_pdf(tmp_path: Path) -> Path:
    import fitz

    doc = fitz.open()
    for _ in range(2):
        doc.new_page(width=595, height=842)
    path = tmp_path / "two.pdf"
    doc.save(path)
    doc.close()
    return path


def test_content_path_ocrs_only_pages_needing_ocr(tmp_path: Path, monkeypatch) -> None:
    """Native pages keep pdf-inspector's markdown; only OCR candidates hit Chandra."""
    from paperless_rearchive.ocr.provenance import MIXED, PdfProvenance

    engine = _engine()
    monkeypatch.setattr(ChandraOcrEngine, "_render_page_image", lambda self, page: object())
    seen: list[int] = []

    def fake_ocr_page(self, image, page_num, page_count=0):
        seen.append(page_num)
        return f"ocr-{page_num}"

    monkeypatch.setattr(ChandraOcrEngine, "_ocr_page", fake_ocr_page)
    provenance = PdfProvenance(
        kind=MIXED,
        page_count=2,
        pages_needing_ocr=frozenset({2}),
        native_markdown={1: "native-1"},
        source="pdf_inspector",
    )

    result = engine.ocr_document(_two_page_pdf(tmp_path), provenance=provenance)

    assert seen == [2]
    assert "native-1" in result.markdown
    assert "ocr-2" in result.markdown
    assert result.error_pages == []
    assert result.page_count == 2


def test_content_path_force_ocrs_every_page(tmp_path: Path, monkeypatch) -> None:
    from paperless_rearchive.ocr.provenance import TEXT_BASED, PdfProvenance

    engine = _engine()
    monkeypatch.setattr(ChandraOcrEngine, "_render_page_image", lambda self, page: object())
    seen: list[int] = []
    monkeypatch.setattr(
        ChandraOcrEngine,
        "_ocr_page",
        lambda self, image, page_num, page_count=0: seen.append(page_num) or f"ocr-{page_num}",
    )
    provenance = PdfProvenance(kind=TEXT_BASED, page_count=2, source="pdf_inspector")

    result = engine.ocr_document(
        _two_page_pdf(tmp_path), provenance=provenance, force=True
    )

    assert seen == [1, 2]
    assert "native" not in result.markdown
    assert "ocr-1" in result.markdown and "ocr-2" in result.markdown


def test_resolve_ingest_mode_uses_provenance(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from paperless_rearchive.ocr.provenance import (
        MIXED,
        SCANNED,
        TEXT_BASED,
        PdfProvenance,
    )

    engine = _engine()
    settings = SimpleNamespace(ocr_mode="redo", ocr_mixed_mode="skip")
    missing = tmp_path / "does-not-exist.pdf"

    assert engine._resolve_ingest_mode(
        settings, missing, PdfProvenance(kind=MIXED, page_count=1), False
    ) == "skip"
    assert engine._resolve_ingest_mode(
        settings, missing, PdfProvenance(kind=SCANNED, page_count=1), False
    ) == "redo"
    assert engine._resolve_ingest_mode(
        settings, missing, PdfProvenance(kind=TEXT_BASED, page_count=1), False
    ) == "off"
    assert engine._resolve_ingest_mode(
        settings, missing, PdfProvenance(kind=TEXT_BASED, page_count=1), True
    ) == "force"
    assert engine._resolve_ingest_mode(settings, missing, None, True) == "force"


# ── model-name preflight ─────────────────────────────────────────────────────


def test_model_not_served_aborts_before_any_page(tmp_path: Path, monkeypatch) -> None:
    """A model the server does not advertise fails the document up front:
    no page is rendered and no page request (hence no retry ladder) is made."""
    engine = _engine()

    def raise_missing(server_url, model_name, api_key=""):
        raise ModelNotServedError(f"Chandra model {model_name!r} is not served by {server_url}")

    monkeypatch.setattr(chandra_engine, "ensure_model_served", raise_missing)
    rendered: list[int] = []
    monkeypatch.setattr(
        ChandraOcrEngine,
        "_render_pdf_pages",
        lambda self, p: rendered.append(1) or [object()],
    )
    generated: list[int] = []
    monkeypatch.setattr(
        "paperless_rearchive.ocr.chandra_engine.chandra_client.ocr_image",
        lambda image, options: generated.append(1),
    )

    with pytest.raises(ModelNotServedError, match="not served"):
        engine.ocr_document(tmp_path / "in.pdf")

    assert rendered == []
    assert generated == []


def test_model_preflight_receives_engine_configuration(tmp_path: Path, monkeypatch) -> None:
    engine = _engine()
    seen: dict[str, str] = {}

    def record(server_url, model_name, api_key=""):
        seen.update(server_url=server_url, model_name=model_name, api_key=api_key)

    monkeypatch.setattr(chandra_engine, "ensure_model_served", record)
    monkeypatch.setattr(ChandraOcrEngine, "_render_pdf_pages", lambda self, p: [])

    engine.ocr_document(tmp_path / "in.pdf")

    assert seen == {
        "server_url": "http://ai:8110/v1",
        "model_name": "chandra-ocr-2-q8",
        "api_key": "secret",
    }


def test_upstream_generation_error_is_logged_with_model(monkeypatch, caplog) -> None:
    """Upstream prints a bare 'Error during VLLM generation: ...'; the
    interceptor re-logs it with the model name (which the message omits)."""
    chandra_engine._install_retry_log_interceptor()
    monkeypatch.setattr(chandra_engine, "_current_model_name", lambda: "typo-model")

    with caplog.at_level(logging.ERROR):
        print("Error during VLLM generation: Error code: 404 - no router for requested model")

    assert any(
        "typo-model" in record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR
    )
