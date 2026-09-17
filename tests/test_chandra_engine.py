"""Tests for ChandraOcrEngine error semantics.

Transport/server errors (``ChandraClientError`` — retries exhausted at the
client) abort the whole document so the poller keeps the trigger tag and
retries; genuine per-page failures (empty result, garbage parse) become page
errors and fail the document with a partial result.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from paperless_chandra.engine.client import ChandraClientError

from paperless_rearchive.ocr.chandra_engine import ChandraOcrEngine


def _engine(concurrency: int = 1) -> ChandraOcrEngine:
    return ChandraOcrEngine(
        server_url="http://ai:8110/v1",
        model_name="chandra-ocr-2-q8",
        api_key="secret",
        concurrency=concurrency,
    )


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