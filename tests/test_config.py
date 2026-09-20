"""Tests for environment-driven settings and backup-directory safety checks.

Backups live at the fixed ``/archive-backups`` mount: startup refuses to run
when the mounts overlap (backups inside the archive tree are reported by
paperless-ngx's health check as orphaned files in the media directory).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

import pytest

from paperless_rearchive.config import Settings


def _settings(**env: str) -> Settings:
    base = {
        "PAPERLESS_API_TOKEN": "t",
    }
    base.update(env)
    # clear=True keeps these tests deterministic regardless of the caller's
    # environment (e.g. an integration shell exporting REARCHIVE_DRY_RUN).
    with patch.dict("os.environ", base, clear=True):
        return Settings.from_env()


def _settings_with_dirs(
    archive_dir: Path, backup_dir: Path, **env: str
) -> Settings:
    """Like :func:`_settings` but with explicit on-disk directories.

    ``backup_dir`` is hardwired to ``/archive-backups`` in production; tests
    inject tmp paths via :func:`dataclasses.replace`.
    """
    return dataclasses.replace(
        _settings(REARCHIVE_ARCHIVE_DIR=str(archive_dir), **env),
        archive_dir=archive_dir,
        backup_dir=backup_dir,
    )


def test_backup_dir_is_hardwired() -> None:
    s = _settings()
    assert s.backup_dir == Path("/archive-backups")


def test_backup_dir_env_is_ignored() -> None:
    s = _settings(REARCHIVE_BACKUP_DIRECTORY="/tmp/should-be-ignored")
    assert s.backup_dir == Path("/archive-backups")


def test_archive_dir_default() -> None:
    assert _settings().archive_dir == Path("/archive")


def test_archive_dir_from_env() -> None:
    s = _settings(REARCHIVE_ARCHIVE_DIR="/custom/archive")
    assert s.archive_dir == Path("/custom/archive")


def test_validate_rejects_backup_dir_equal_to_archive_dir(tmp_path: Path) -> None:
    s = _settings_with_dirs(tmp_path, tmp_path)
    with pytest.raises(ValueError, match="archive directory"):
        s.validate()


def test_validate_rejects_backup_dir_inside_archive_dir(tmp_path: Path) -> None:
    s = _settings_with_dirs(tmp_path, tmp_path / "backups")
    with pytest.raises(ValueError, match="inside it"):
        s.validate()


def test_validate_sees_through_symlinks(tmp_path: Path) -> None:
    link = tmp_path / "link-to-archive"
    link.symlink_to(tmp_path, target_is_directory=True)
    s = _settings_with_dirs(tmp_path, link)
    with pytest.raises(ValueError):
        s.validate()


def test_validate_accepts_distinct_backup_dir(tmp_path: Path) -> None:
    s = _settings_with_dirs(tmp_path / "archive", tmp_path / "backups")
    s.validate()  # no raise


def test_validate_noop_when_unset() -> None:
    _settings().validate()  # no raise


def test_validate_rejects_backup_dir_that_is_a_file(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_text("x")
    s = _settings_with_dirs(tmp_path / "archive", not_a_dir)
    with pytest.raises(ValueError, match="not a directory"):
        s.validate()


def test_prepare_backup_dir_creates_and_is_idempotent(tmp_path: Path) -> None:
    backup_dir = tmp_path / "deep" / "backups"
    s = _settings_with_dirs(tmp_path, backup_dir)
    s.prepare_backup_dir()
    assert backup_dir.is_dir()
    s.prepare_backup_dir()  # second call must not fail


def test_prepare_backup_dir_run_does_not_raise(tmp_path: Path) -> None:
    _settings_with_dirs(tmp_path / "archive", tmp_path / "backups").prepare_backup_dir()


def test_prepare_backup_dir_dry_run_does_not_create(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    s = _settings_with_dirs(
        tmp_path / "archive", backup_dir, REARCHIVE_DRY_RUN="true"
    )
    s.prepare_backup_dir()
    assert not backup_dir.exists()


# ── provenance gate settings ─────────────────────────────────────────────────


def test_provenance_defaults() -> None:
    s = _settings()
    assert s.pdf_provenance == "on"
    assert s.skip_born_digital is True
    assert s.preserved_tag == "re-ocr-preserved"
    assert s.ocr_mixed_mode == "skip"
    assert s.provenance_max_pages == 0
    assert s.force_tag == "re-ocr-force"


def test_provenance_settings_from_env() -> None:
    s = _settings(
        REARCHIVE_PDF_PROVENANCE="off",
        REARCHIVE_SKIP_BORN_DIGITAL="false",
        REARCHIVE_PRESERVED_TAG="",
        REARCHIVE_OCR_MIXED_MODE="redo",
        REARCHIVE_PROVENANCE_MAX_PAGES="3",
        REARCHIVE_FORCE_TAG="re-ocr-hard",
    )
    assert s.pdf_provenance == "off"
    assert s.skip_born_digital is False
    assert s.preserved_tag == ""
    assert s.ocr_mixed_mode == "redo"
    assert s.provenance_max_pages == 3
    assert s.force_tag == "re-ocr-hard"


def test_validate_rejects_unknown_provenance_mode() -> None:
    s = _settings(REARCHIVE_PDF_PROVENANCE="sometimes")
    with pytest.raises(
        ValueError, match="REARCHIVE_PDF_PROVENANCE must be 'on' or 'off'"
    ):
        s.validate()


def test_validate_rejects_unknown_mixed_mode() -> None:
    s = _settings(REARCHIVE_OCR_MIXED_MODE="turbo")
    with pytest.raises(ValueError, match="REARCHIVE_OCR_MIXED_MODE"):
        s.validate()
