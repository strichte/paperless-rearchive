"""Tests for the pipeline's error-classification consistency.

DB failures are transient (they must propagate so the poller keeps the
trigger tag and retries, exactly like ``fetch_archive_filename``); only an
unrepairable checksum drift reaches ``_finish(success=False)``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from paperless_rearchive.archive.replacer import ArchiveReplaceError
from paperless_rearchive.config import Settings
from paperless_rearchive.pipeline import DocumentContext, process_document


def _settings(tmp_path: Path) -> Settings:
    base = {
        "PAPERLESS_API_TOKEN": "t",
        "REARCHIVE_ARCHIVE_DIR": str(tmp_path / "archive"),
        "REARCHIVE_BACKUP_DIRECTORY": str(tmp_path / "backups"),
    }
    with patch.dict("os.environ", base, clear=True):
        return Settings.from_env()


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
