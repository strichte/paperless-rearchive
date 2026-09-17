"""Unit tests for checksum and archive replacement."""

from __future__ import annotations

from pathlib import Path

import pytest

from paperless_rearchive.archive.replacer import (
    ArchiveReplaceError,
    checksum_of_file,
    replace_archive,
    verify_current_checksum,
)


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_md5_of_file(tmp_path: Path) -> None:
    f = _write(tmp_path / "a.pdf", b"hello world")
    assert checksum_of_file(f) == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


def test_verify_checksum_ok_and_missing(tmp_path: Path) -> None:
    f = _write(tmp_path / "a.pdf", b"data")
    verify_current_checksum(f, checksum_of_file(f))  # no raise
    verify_current_checksum(f, None)  # no checksum known -> allowed
    with pytest.raises(ArchiveReplaceError, match="not found"):
        verify_current_checksum(tmp_path / "missing.pdf", "x")


def test_verify_checksum_mismatch(tmp_path: Path) -> None:
    f = _write(tmp_path / "a.pdf", b"data")
    with pytest.raises(ArchiveReplaceError, match="Checksum mismatch"):
        verify_current_checksum(f, "0" * 32)


def test_replace_archive_atomic_and_backup(tmp_path: Path) -> None:
    old = _write(tmp_path / "0000123.pdf", b"old-archive-bytes")
    new = _write(tmp_path / "new.pdf", b"new-archive-bytes")
    backup_dir = tmp_path / "backups"

    checksum = replace_archive(old, new, backup_dir=backup_dir)

    assert old.read_bytes() == b"new-archive-bytes"
    assert checksum_of_file(old) == checksum
    assert checksum == checksum_of_file(new)
    backups = list(backup_dir.glob("0000123.pdf.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"old-archive-bytes"
    # no backup next to the archive (no orphaned file in the media dir)
    assert not list(tmp_path.glob("0000123.pdf.bak-*"))
    # no stray staging files
    assert not list(tmp_path.glob(".0000123.pdf.rearch-tmp"))


def test_replace_archive_refuses_backup_inside_archive_dir(tmp_path: Path) -> None:
    """Runtime backstop: even a misconfigured backup_dir that slipped past
    startup validation must never yield a backup inside the archive tree."""
    from paperless_rearchive.archive.replacer import backup_destination

    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    old = _write(archive_dir / "foo.pdf", b"old")
    new = _write(tmp_path / "new.pdf", b"new")
    bad_backup_dir = archive_dir / "backups"  # inside the archive tree!

    with pytest.raises(ArchiveReplaceError, match="Refusing to write a backup"):
        backup_destination(old, backup_dir=bad_backup_dir, archive_dir=archive_dir)
    with pytest.raises(ArchiveReplaceError, match="Refusing to write a backup"):
        replace_archive(old, new, backup_dir=bad_backup_dir, archive_dir=archive_dir)
    # nothing was written anywhere
    assert not list(archive_dir.rglob("*.bak-*"))


def test_replace_archive_backup_dir_mirrors_relative_path(tmp_path: Path) -> None:
    """A configured backup dir gets the archive's template sub-dirs."""
    archive_dir = tmp_path / "archive"
    target_dir = archive_dir / "Passports" / "DE" / "1971"
    target_dir.mkdir(parents=True)
    old = _write(target_dir / "foo.pdf", b"old-bytes")
    new = _write(tmp_path / "new.pdf", b"new-bytes")
    backup_dir = tmp_path / "backups"

    checksum = replace_archive(old, new, backup_dir=backup_dir, archive_dir=archive_dir)

    assert old.read_bytes() == b"new-bytes"
    assert checksum_of_file(old) == checksum
    backups = list((backup_dir / "Passports" / "DE" / "1971").glob("foo.pdf.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"old-bytes"
    # nothing left next to the archive (no orphaned file in the media dir)
    assert not list(target_dir.glob("*.bak-*"))


def test_replace_archive_backup_dir_flattens_outside_archive_dir(tmp_path: Path) -> None:
    """An archive not below archive_dir is stored flat, not lost."""
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    old = _write(elsewhere / "foo.pdf", b"old")
    new = _write(tmp_path / "new.pdf", b"new")
    backup_dir = tmp_path / "backups"

    replace_archive(old, new, backup_dir=backup_dir, archive_dir=archive_dir)

    assert len(list(backup_dir.glob("foo.pdf.bak-*"))) == 1
    assert not list(elsewhere.glob("*.bak-*"))


def test_replace_archive_backup_dir_created_on_demand(tmp_path: Path) -> None:
    """Nested backup dirs are created; a different disk is just a copy."""
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    old = _write(archive_dir / "a.pdf", b"old")
    new = _write(tmp_path / "new.pdf", b"new")
    backup_dir = tmp_path / "deep" / "backups"  # outside the archive tree

    replace_archive(old, new, backup_dir=backup_dir, archive_dir=archive_dir)

    assert old.read_bytes() == b"new"
    assert len(list(backup_dir.glob("a.pdf.bak-*"))) == 1


def test_backup_destination_flat_when_no_archive_dir(tmp_path: Path) -> None:
    from paperless_rearchive.archive.replacer import backup_destination

    archive = _write(tmp_path / "a.pdf", b"x")
    backup_dir = tmp_path / "backups"
    dest = backup_destination(archive, backup_dir=backup_dir)
    assert dest.parent == backup_dir
    assert dest.name.startswith("a.pdf.bak-")
