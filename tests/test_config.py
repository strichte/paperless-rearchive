"""Tests for environment-driven settings and backup-directory safety checks.

The important one is the refusal to accept ``REARCHIVE_BACKUP_DIRECTORY`` at
or below the archive directory: backups there are reported by paperless-ngx's
health check as orphaned files in the media directory.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from paperless_rearchive.config import Settings


def _settings(**env: str) -> Settings:
    base = {"PAPERLESS_API_TOKEN": "t"}
    base.update(env)
    # clear=True keeps these tests deterministic regardless of the caller's
    # environment (e.g. an integration shell exporting REARCHIVE_DRY_RUN).
    with patch.dict("os.environ", base, clear=True):
        return Settings.from_env()


def test_backup_dir_unset_by_default() -> None:
    assert _settings().backup_dir is None


def test_backup_dir_from_env() -> None:
    s = _settings(REARCHIVE_BACKUP_DIRECTORY="/archive-backups")
    assert s.backup_dir == Path("/archive-backups")


def test_validate_rejects_backup_dir_equal_to_archive_dir(tmp_path: Path) -> None:
    s = _settings(
        REARCHIVE_ARCHIVE_DIR=str(tmp_path),
        REARCHIVE_BACKUP_DIRECTORY=str(tmp_path),
    )
    with pytest.raises(ValueError, match="archive directory"):
        s.validate()


def test_validate_rejects_backup_dir_inside_archive_dir(tmp_path: Path) -> None:
    s = _settings(
        REARCHIVE_ARCHIVE_DIR=str(tmp_path),
        REARCHIVE_BACKUP_DIRECTORY=str(tmp_path / "backups"),
    )
    with pytest.raises(ValueError, match="inside it"):
        s.validate()


def test_validate_sees_through_symlinks(tmp_path: Path) -> None:
    link = tmp_path / "link-to-archive"
    link.symlink_to(tmp_path, target_is_directory=True)
    s = _settings(
        REARCHIVE_ARCHIVE_DIR=str(tmp_path),
        REARCHIVE_BACKUP_DIRECTORY=str(link),
    )
    with pytest.raises(ValueError):
        s.validate()


def test_validate_accepts_distinct_backup_dir(tmp_path: Path) -> None:
    s = _settings(
        REARCHIVE_ARCHIVE_DIR=str(tmp_path / "archive"),
        REARCHIVE_BACKUP_DIRECTORY=str(tmp_path / "backups"),
    )
    s.validate()  # no raise


def test_validate_noop_when_unset() -> None:
    _settings().validate()  # no raise


def test_validate_rejects_backup_dir_that_is_a_file(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_text("x")
    s = _settings(REARCHIVE_BACKUP_DIRECTORY=str(not_a_dir))
    with pytest.raises(ValueError, match="not a directory"):
        s.validate()


def test_prepare_backup_dir_creates_and_is_idempotent(tmp_path: Path) -> None:
    backup_dir = tmp_path / "deep" / "backups"
    s = _settings(
        REARCHIVE_ARCHIVE_DIR=str(tmp_path),
        REARCHIVE_BACKUP_DIRECTORY=str(backup_dir),
    )
    s.prepare_backup_dir()
    assert backup_dir.is_dir()
    s.prepare_backup_dir()  # second call must not fail


def test_prepare_backup_dir_noop_when_unset() -> None:
    _settings().prepare_backup_dir()  # no raise


def test_prepare_backup_dir_dry_run_does_not_create(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    s = _settings(
        REARCHIVE_DRY_RUN="true",
        REARCHIVE_BACKUP_DIRECTORY=str(backup_dir),
    )
    s.prepare_backup_dir()
    assert not backup_dir.exists()
