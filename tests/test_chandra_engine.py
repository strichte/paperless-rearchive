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
from paperless_rearchive.ocr.server_check import ServerUnreachableError


def _engine(concurrency: int = 1) -> ChandraOcrEngine:
    return ChandraOcrEngine(
        server_url="http://ai:8110/v1",
        model_name="chandra-ocr-2-q8",
        api_key="secret",
        concurrency=concurrency,
    )


@pytest.fixture(autouse=True)
def _no_preflight_probes(monkeypatch):
    """Keep the /models preflights offline for the local-logic tests.

    The probes themselves are covered by tests/test_model_check.py and
    tests/test_server_check.py, and the abort-before-any-page behaviour by
    test_model_not_served_aborts_before_any_page below.
    """
    monkeypatch.setattr(chandra_engine, "ensure_model_served", lambda *a, **k: None)
    monkeypatch.setattr(chandra_engine, "ensure_server_reachable", lambda *a, **k: None)


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


# ── reachability preflight (2026-09-22 outage, doc 3697) ─────────────────────


def test_unreachable_server_aborts_before_any_page(tmp_path: Path, monkeypatch) -> None:
    """An unreachable inference server fails the document up front as a
    ChandraClientError subclass - before any page is rendered, so the poller
    aborts the cycle instead of recording a per-document failure."""

    def raise_unreachable(server_url, api_key="", timeout=None):
        raise ServerUnreachableError(
            f"The Chandra server at {server_url} is not reachable "
            "([Errno 111] Connection refused)."
        )

    monkeypatch.setattr(chandra_engine, "ensure_server_reachable", raise_unreachable)
    engine = _engine()
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

    with pytest.raises(ChandraClientError, match="not reachable"):
        engine.ocr_document(tmp_path / "in.pdf")

    assert rendered == []
    assert generated == []


def _ingest_pass_engine(
    tmp_path: Path, monkeypatch, ocr_side_effects, expected_error: type[Exception] | None = None
) -> list[dict]:
    """Drive _ocr_document_ingest_pass with the given ocrmypdf.ocr behaviour;
    return the captured ocrmypdf kwargs per call."""
    import ocrmypdf

    engine = _engine()
    output_pdf = tmp_path / "out.pdf"
    calls: list[dict] = []

    def fake_ocr(**kwargs):
        calls.append(kwargs)
        side_effect = ocr_side_effects[len(calls) - 1]
        if isinstance(side_effect, Exception):
            raise side_effect
        output_pdf.write_bytes(b"%PDF-fake")
        return 0

    monkeypatch.setattr(ocrmypdf, "ocr", fake_ocr)
    monkeypatch.setattr(ChandraOcrEngine, "_stamp_model_provenance", lambda self, pdf: None)
    monkeypatch.setattr(
        ChandraOcrEngine,
        "_pdftotext_page_map",
        lambda self, pdf: {1: "native-1", 2: "ocr-2", 3: "ocr-3"},
    )
    monkeypatch.setattr(
        "paperless_rearchive.ocr.ingest_args.sidecar_content",
        lambda *a, **k: "ocr-2 ocr-3",
    )
    if expected_error is not None:
        with pytest.raises(expected_error):
            engine._ocr_document_ingest_pass(
                _three_page_pdf(tmp_path),
                output_pdf,
                _ingest_settings(),
                provenance=_mixed_provenance(),
            )
    else:
        engine._ocr_document_ingest_pass(
            _three_page_pdf(tmp_path),
            output_pdf,
            _ingest_settings(),
            provenance=_mixed_provenance(),
        )
    return calls


def test_unreachable_server_not_retried_with_safe_fallback(tmp_path: Path, monkeypatch) -> None:
    """The plugin's check_options probe raises MissingDependencyError when the
    server is unreachable. The safe fallback only changes ocrmypdf args - it
    cannot bring a dead server back - so the error must propagate as-is
    (previously: a second identical pass + a wrapped RuntimeError)."""
    from ocrmypdf.exceptions import MissingDependencyError

    boom = MissingDependencyError(
        "The Chandra server at http://ai:8110/v1 is not reachable "
        "([Errno 111] Connection refused). Check PAPERLESS_CHANDRA_SERVER_URL "
        "and that the inference server container is running."
    )
    calls = _ingest_pass_engine(
        tmp_path, monkeypatch, [boom], expected_error=MissingDependencyError
    )
    assert len(calls) == 1  # no safe-fallback second pass


def test_other_ocr_failures_still_get_safe_fallback(tmp_path: Path, monkeypatch) -> None:
    """Non-outage failures keep the paperless-style safe-fallback retry:
    first pass fails with a redo-specific error, force_ocr pass succeeds."""
    from ocrmypdf.exceptions import PriorOcrFoundError

    calls = _ingest_pass_engine(
        tmp_path,
        monkeypatch,
        [PriorOcrFoundError("prior ocr found"), None],
    )
    assert len(calls) == 2
    assert calls[0].get("redo_ocr") is True
    assert calls[1].get("force_ocr") is True


def test_missing_dependency_discriminator_matches_plugin_messages() -> None:
    """The message regex separates the plugin's two MissingDependencyError
    causes: unreachable server (skip the fallback) vs rejected API key
    (configuration error, fallback behaviour unchanged)."""
    unreachable = (
        "The Chandra server at http://ai:8000/v1 is not reachable "
        "([Errno 111] Connection refused). Check PAPERLESS_CHANDRA_SERVER_URL."
    )
    bad_key = (
        "The Chandra server at http://ai:8000/v1 rejected the API key "
        "(HTTP 401). Check PAPERLESS_CHANDRA_API_KEY."
    )
    other = "OCRmyPDF failed: UnknownError: something else"
    assert chandra_engine._is_missing_dependency(Exception(unreachable)) is True
    assert chandra_engine._is_missing_dependency(Exception(bad_key)) is False
    assert chandra_engine._is_missing_dependency(Exception(other)) is False


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


# ── mixed-provenance ingest pass (document 5830 regression) ──────────────────


def _mixed_provenance(page_count: int = 3) -> "object":
    from paperless_rearchive.ocr.provenance import MIXED, PdfProvenance

    return PdfProvenance(
        kind=MIXED,
        page_count=page_count,
        pages_needing_ocr=frozenset({2, 3}),
        native_markdown={1: "native-1"},
        source="pdf_inspector",
    )


def _three_page_pdf(tmp_path: Path) -> Path:
    import fitz

    doc = fitz.open()
    for _ in range(3):
        doc.new_page(width=595, height=842)
    path = tmp_path / "three.pdf"
    doc.save(path)
    doc.close()
    return path


def _ingest_settings() -> "object":
    from types import SimpleNamespace

    return SimpleNamespace(
        ocr_mode="redo",
        ocr_mixed_mode="skip",
        ocr_clean="clean",
        ocr_deskew=True,
        ocr_rotate=True,
        ocr_rotate_threshold=12.0,
        ocr_output_type="pdfa",
        ocr_user_args=None,
    )


def test_mixed_ingest_pass_ocrs_only_pages_needing_ocr(tmp_path: Path, monkeypatch) -> None:
    """Regression (doc 5830): a mixed document must not be handed to ocrmypdf
    with skip_text (which skipped every text-bearing page, OCR no-op). OCR is
    restricted to the pages the provenance verdict marked, native pages pass
    through via --pages."""
    import ocrmypdf

    engine = _engine()
    captured: dict = {}
    output_pdf = tmp_path / "out.pdf"

    def fake_ocr(**kwargs):
        captured.update(kwargs)
        output_pdf.write_bytes(b"%PDF-fake")
        return 0

    monkeypatch.setattr(ocrmypdf, "ocr", fake_ocr)
    monkeypatch.setattr(
        ChandraOcrEngine, "_stamp_model_provenance", lambda self, pdf: None
    )
    monkeypatch.setattr(
        ChandraOcrEngine,
        "_pdftotext_page_map",
        lambda self, pdf: {1: "native-1", 2: "ocr-2", 3: "ocr-3"},
    )

    result = engine._ocr_document_ingest_pass(
        _three_page_pdf(tmp_path),
        output_pdf,
        _ingest_settings(),
        provenance=_mixed_provenance(),
    )

    assert "skip_text" not in captured
    assert captured["pages"] == "2,3"
    assert captured.get("redo_ocr") is True
    assert "sidecar" not in captured
    assert "native-1" in result.markdown
    assert "ocr-2" in result.markdown and "ocr-3" in result.markdown


def test_mixed_ingest_pass_respects_max_pages_cap(tmp_path: Path, monkeypatch) -> None:
    """The mixed --pages list is intersected with the REARCHIVE_MAX_PAGES cap."""
    import ocrmypdf

    engine = ChandraOcrEngine(
        server_url="http://ai:8110/v1",
        model_name="m",
        api_key="k",
        max_pages=2,
    )
    captured: dict = {}
    output_pdf = tmp_path / "out.pdf"

    def fake_ocr(**kwargs):
        captured.update(kwargs)
        output_pdf.write_bytes(b"%PDF-fake")
        return 0

    monkeypatch.setattr(ocrmypdf, "ocr", fake_ocr)
    monkeypatch.setattr(
        ChandraOcrEngine, "_stamp_model_provenance", lambda self, pdf: None
    )
    monkeypatch.setattr(
        ChandraOcrEngine, "_pdftotext_page_map", lambda self, pdf: {1: "n", 2: "o"}
    )

    engine._ocr_document_ingest_pass(
        _three_page_pdf(tmp_path),
        output_pdf,
        _ingest_settings(),
        provenance=_mixed_provenance(),
    )

    assert captured["pages"] == "2"  # page 3 beyond the cap


def test_mixed_ingest_pass_warns_when_all_pages_skipped(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """Guard: a sidecar with a skip placeholder for every page (the silent
    no-op seen on doc 5830) must log a loud warning."""
    import ocrmypdf

    engine = _engine()
    output_pdf = tmp_path / "out.pdf"
    sidecar = tmp_path / "archive-sidecar.txt"
    sidecar.write_text(
        "[OCR skipped on page 1]\n[OCR skipped on page 2]\n[OCR skipped on page 3]\n"
    )

    def fake_ocr(**kwargs):
        output_pdf.write_bytes(b"%PDF-fake")
        sidecar.touch()
        return 0

    monkeypatch.setattr(ocrmypdf, "ocr", fake_ocr)
    monkeypatch.setattr(
        ChandraOcrEngine, "_stamp_model_provenance", lambda self, pdf: None
    )
    monkeypatch.setattr(
        "paperless_rearchive.ocr.ingest_args.sidecar_content",
        lambda *a, **k: "untouched text",
    )

    with caplog.at_level(logging.WARNING):
        engine._ocr_document_ingest_pass(
            _three_page_pdf(tmp_path),
            output_pdf,
            _ingest_settings(),
            provenance=None,  # not mixed: skip mode + skip-all sidecar
        )

    assert any("made no changes" in r.getMessage() for r in caplog.records)


# ── per-page action reporting (final pipeline log) ───────────────────────────


def test_ingest_pass_reports_page_actions(tmp_path: Path, monkeypatch) -> None:
    """The ingest pass records which pages were OCR'd vs passed through, so
    the pipeline can log 'what was done to which page'."""
    import ocrmypdf

    engine = _engine()
    output_pdf = tmp_path / "out.pdf"

    def fake_ocr(**kwargs):
        output_pdf.write_bytes(b"%PDF-fake")
        return 0

    monkeypatch.setattr(ocrmypdf, "ocr", fake_ocr)
    monkeypatch.setattr(
        ChandraOcrEngine, "_stamp_model_provenance", lambda self, pdf: None
    )
    monkeypatch.setattr(
        ChandraOcrEngine,
        "_pdftotext_page_map",
        lambda self, pdf: {1: "native-1", 2: "ocr-2", 3: "ocr-3"},
    )

    result = engine._ocr_document_ingest_pass(
        _three_page_pdf(tmp_path),
        output_pdf,
        _ingest_settings(),
        provenance=_mixed_provenance(),
    )

    assert result.page_actions == {1: "passthrough", 2: "ocr", 3: "ocr"}


def test_page_action_summary_formats_groups() -> None:
    from paperless_rearchive.pipeline import _page_action_summary

    class _R:
        page_actions = {
            1: "passthrough",
            2: "ocr",
            3: "ocr",
            4: "error",
            5: "skipped",
        }

    summary = _page_action_summary(_R())
    assert summary == (
        "; pages: 2-3 ocr (fresh Chandra layer), "
        "1 passthrough (untouched), 4 error, 5 skipped (page cap)"
    )


def test_page_action_summary_empty_without_actions() -> None:
    from paperless_rearchive.pipeline import _page_action_summary

    assert _page_action_summary(object()) == ""
    assert _page_action_summary(type("R", (), {"page_actions": {}})()) == ""


def test_unrenderable_input_raises_clear_error(tmp_path: Path) -> None:
    """An unforeseen non-renderable original fails with a clear message,
    not a raw PyMuPDF FileDataError dump."""
    import pytest as _pytest

    engine = _engine()
    junk = tmp_path / "expense.xls"
    junk.write_bytes(b"\xd0\xcf\x11\xe0")
    with _pytest.raises(RuntimeError, match="not a renderable document"):
        engine._render_pdf_pages(junk)


def test_image_original_flows_through_content_path(tmp_path: Path, monkeypatch) -> None:
    """Raster-image originals (paperless stores scans as JPG/PNG) are
    renderable by PyMuPDF as single-page documents and OCR via the legacy
    content path."""
    from PIL import Image

    engine = _engine()
    img_path = tmp_path / "scan.png"
    Image.new("RGB", (64, 64), color="white").save(img_path)

    monkeypatch.setattr(
        ChandraOcrEngine,
        "_ocr_page",
        lambda self, image, page_num, page_count=0: "ocr-text",
    )

    result = engine.ocr_document(img_path)
    assert "ocr-text" in result.markdown
    assert result.page_count == 1
    assert result.error_pages == []
