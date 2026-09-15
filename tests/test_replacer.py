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

    checksum = replace_archive(old, new, keep_backup=True)

    assert old.read_bytes() == b"new-archive-bytes"
    assert checksum_of_file(old) == checksum
    assert checksum == checksum_of_file(new)
    backups = list(tmp_path.glob("0000123.pdf.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"old-archive-bytes"
    # no stray staging files
    assert not list(tmp_path.glob(".0000123.pdf.rearch-tmp"))


def test_replace_archive_no_backup(tmp_path: Path) -> None:
    old = _write(tmp_path / "0000123.pdf", b"old")
    new = _write(tmp_path / "new.pdf", b"new")
    replace_archive(old, new, keep_backup=False)
    assert not list(tmp_path.glob("*.bak-*"))
