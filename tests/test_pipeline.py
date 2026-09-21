"""Tests for the pipeline's error-classification consistency.

DB failures are transient (they must propagate so the poller keeps the
trigger tag and retries, exactly like ``fetch_archive_filename``); only an
unrepairable checksum drift reaches ``_finish(success=False)``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from paperless_rearchive.archive.replacer import ArchiveReplaceError
from paperless_rearchive.config import Settings
from paperless_rearchive.ocr.chandra_engine import OcrResult
from paperless_rearchive.ocr.provenance import (
    SCANNED,
    TEXT_BASED,
    PdfProvenance,
)
from paperless_rearchive.pipeline import DocumentContext, process_document


def _settings(tmp_path: Path) -> Settings:
    base = {
        "PAPERLESS_API_TOKEN": "t",
        "REARCHIVE_ARCHIVE_DIR": str(tmp_path / "archive"),
    }
    with patch.dict("os.environ", base, clear=True):
        settings = Settings.from_env()
    return dataclasses.replace(
        settings,
        archive_dir=tmp_path / "archive",
        backup_dir=tmp_path / "backups",
    )


class _FakeAPI:
    def document(self, doc_id: int) -> dict:
        return {"tags": []}

    def download_original(self, doc_id: int, dest_dir: Path) -> Path:
        original = Path(dest_dir) / "original.pdf"
        original.write_bytes(b"%PDF-1.4\n")
        return original


def _ctx() -> DocumentContext:
    return DocumentContext(
        doc_id=1,
        trigger_tag_id=11,
        trigger_tag_name="re-ocr-all",
        archive_mode=True,
        current_tags=[11],
    )


class _RecordingAPI:
    """Fake API recording writes, so gate decisions are observable."""

    def __init__(self) -> None:
        self.patched: list[tuple[int, str]] = []
        self.removed_tags: list[list[int]] = []
        self.added_tags: list[list[int]] = []
        self.notes: list[str] = []
        self._tags: dict[str, int] = {}
        self._next = 1000

    def document(self, doc_id: int) -> dict:
        return {"tags": []}

    def download_original(self, doc_id: int, dest_dir: Path) -> Path:
        original = Path(dest_dir) / "original.pdf"
        original.write_bytes(b"%PDF-1.4\n")
        return original

    def ensure_tag(self, name: str) -> int:
        if name not in self._tags:
            self._next += 1
            self._tags[name] = self._next
        return self._tags[name]

    def set_tags(self, doc_id: int, *, remove, add, current_tags) -> None:
        self.removed_tags.append(list(remove))
        self.added_tags.append(list(add))

    def add_note(self, doc_id: int, note: str) -> None:
        self.notes.append(note)

    def patch_content(self, doc_id: int, content: str) -> None:
        self.patched.append((doc_id, content))

    def ensure_provenance_fields(self) -> dict:
        return {}

    def set_custom_fields(self, doc_id: int, values) -> None:
        pass


def _provider() -> SimpleNamespace:
    return SimpleNamespace(
        server_url="http://ai:8110/v1",
        model_name="chandra-ocr",
        api_key="secret",
        content_format="markdown",
        max_output_tokens=12384,
    )


def _content_ctx(*, force: bool = False, force_tag_id: int | None = None) -> DocumentContext:
    tags = [11] + ([force_tag_id] if force_tag_id is not None else [])
    return DocumentContext(
        doc_id=1,
        trigger_tag_id=11,
        trigger_tag_name="re-ocr-content",
        archive_mode=False,
        current_tags=tags,
        force=force,
        force_tag_id=force_tag_id,
    )


def test_born_digital_is_preserved_without_writes(tmp_path: Path, monkeypatch) -> None:
    """A text_based verdict writes nothing (no patch, no engine) and swaps the
    trigger for -success + the preserved tag."""
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        "paperless_rearchive.pipeline.classify_pdf",
        lambda *a, **kw: PdfProvenance(
            kind=TEXT_BASED, page_count=1, native_markdown={1: "native"}, source="pdf_inspector"
        ),
    )
    api = _RecordingAPI()
    process_document(settings, api, None, _content_ctx())

    assert api.patched == []
    assert api.removed_tags == [[11]]
    preserved_id = api._tags["re-ocr-preserved"]
    success_id = api._tags["re-ocr-content-success"]
    assert api.added_tags[0] == [success_id, preserved_id]
    assert any("born-digital" in note for note in api.notes)


def test_force_tag_bypasses_the_gate(tmp_path: Path, monkeypatch) -> None:
    """re-ocr-force ignores the text_based verdict, OCRs, and is removed."""
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        "paperless_rearchive.pipeline.classify_pdf",
        lambda *a, **kw: PdfProvenance(
            kind=TEXT_BASED, page_count=1, native_markdown={1: "native"}, source="pdf_inspector"
        ),
    )
    called: dict = {}

    def fake_ocr(self, pdf_path, **kwargs):
        called.update(kwargs)
        return OcrResult(markdown="forced content", pdf_path=None, page_count=1)

    monkeypatch.setattr(
        "paperless_rearchive.pipeline.ChandraOcrEngine.ocr_document", fake_ocr
    )
    api = _RecordingAPI()
    process_document(settings, api, _provider(), _content_ctx(force=True, force_tag_id=99))

    assert called["force"] is True
    assert api.patched and api.patched[0][1] == "forced content"
    assert api.removed_tags == [[11, 99]]


def test_scanned_document_passes_provenance_to_engine(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        "paperless_rearchive.pipeline.classify_pdf",
        lambda *a, **kw: PdfProvenance(
            kind=SCANNED, page_count=1, pages_needing_ocr=frozenset({1}), source="pdf_inspector"
        ),
    )
    called: dict = {}

    def fake_ocr(self, pdf_path, **kwargs):
        called.update(kwargs)
        return OcrResult(markdown="scanned content", pdf_path=None, page_count=1)

    monkeypatch.setattr(
        "paperless_rearchive.pipeline.ChandraOcrEngine.ocr_document", fake_ocr
    )
    api = _RecordingAPI()
    process_document(settings, api, _provider(), _content_ctx())

    assert called["provenance"].kind == SCANNED
    assert called["force"] is False
    assert api.patched and api.patched[0][1] == "scanned content"


def test_db_checksum_error_propagates_trigger_kept(tmp_path: Path) -> None:
    """Postgres down at the checksum fetch -> RuntimeError propagates out of
    process_document (poller keeps the trigger tag and retries); the document
    is NOT failed via _finish."""
    settings = _settings(tmp_path)
    finishes: list[dict] = []
    with (
        patch("paperless_rearchive.pipeline.fetch_archive_filename", return_value="doc.pdf"),
        patch(
            "paperless_rearchive.pipeline.fetch_archive_checksum",
            side_effect=RuntimeError("db connection lost"),
        ),
        patch("paperless_rearchive.pipeline._finish", side_effect=lambda *a, **kw: finishes.append(kw)),
        pytest.raises(RuntimeError, match="db connection lost"),
    ):
        process_document(settings, _FakeAPI(), None, _ctx())
    assert finishes == []


def test_unrepairable_drift_fails_the_document(tmp_path: Path) -> None:
    """Checksum drift that cannot be adopted is a decision -> failure tag."""
    settings = _settings(tmp_path)
    (tmp_path / "archive").mkdir(parents=True)
    (tmp_path / "archive" / "doc.pdf").write_bytes(b"%PDF-1.4\n")
    finishes: list[dict] = []
    with (
        patch("paperless_rearchive.pipeline.fetch_archive_filename", return_value="doc.pdf"),
        patch("paperless_rearchive.pipeline.fetch_archive_checksum", return_value="0" * 64),
        patch(
            "paperless_rearchive.pipeline.verify_current_checksum",
            side_effect=ArchiveReplaceError("Checksum mismatch"),
        ),
        patch(
            "paperless_rearchive.pipeline._repair_checksum_drift",
            side_effect=ArchiveReplaceError("Checksum mismatch"),
        ),
        patch("paperless_rearchive.pipeline._finish", side_effect=lambda *a, **kw: finishes.append(kw)),
    ):
        process_document(settings, _FakeAPI(), None, _ctx())

    assert len(finishes) == 1
    assert finishes[0]["success"] is False
    assert "Checksum mismatch" in finishes[0]["note"]


def test_non_ocrable_original_is_skipped_with_marker_tag(tmp_path: Path, monkeypatch) -> None:
    """Regression (doc 5080): an Office-document original (.xls, ...) has no
    OCR text layer to re-run - the pipeline must resolve the trigger cleanly
    (success + re-ocr-skipped) instead of crashing 3 cycles into a failure."""
    settings = _settings(tmp_path)

    class _XlsAPI(_RecordingAPI):
        def download_original(self, doc_id: int, dest_dir: Path) -> Path:
            original = Path(dest_dir) / "expense.xls"
            original.write_bytes(b"\xd0\xcf\x11\xe0")  # OLE2 magic
            return original

    monkeypatch.setattr(
        "paperless_rearchive.pipeline.ChandraOcrEngine",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("engine must not run")),
    )
    api = _XlsAPI()
    ctx = DocumentContext(
        doc_id=5080,
        trigger_tag_id=11,
        trigger_tag_name="re-ocr-all",
        archive_mode=True,
        current_tags=[11],
    )
    process_document(settings, api, None, ctx)

    assert api.patched == []  # content untouched
    skipped_id = api._tags["re-ocr-skipped"]
    success_id = api._tags["re-ocr-all-success"]
    assert api.added_tags[0] == [success_id, skipped_id]
    assert any("digital-born" in note and "not PDF" in note for note in api.notes)


def test_dry_run_keeps_trigger_for_non_ocrable_original(tmp_path: Path) -> None:
    """Dry-run: log only, no tag changes, no note."""
    settings = dataclasses.replace(_settings(tmp_path), dry_run=True)

    class _XlsAPI(_RecordingAPI):
        def download_original(self, doc_id: int, dest_dir: Path) -> Path:
            original = Path(dest_dir) / "memo.docx"
            original.write_bytes(b"PK\x03\x04")
            return original

    api = _XlsAPI()
    ctx = DocumentContext(
        doc_id=1,
        trigger_tag_id=11,
        trigger_tag_name="re-ocr-all",
        archive_mode=True,
        current_tags=[11],
    )
    process_document(settings, api, None, ctx)

    assert api.removed_tags == [] and api.added_tags == []
    assert api.notes == []
