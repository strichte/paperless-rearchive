"""Checksum helpers and atomic archive replacement.

The paperless REST API cannot replace the archive version of an existing
document, so the sidecar bind-mounts ``media/documents/archives`` and replaces
the file directly. paperless computes ``archive_checksum`` as the MD5 of the
archive file bytes (``documents.utils.compute_checksum``); the database row is
updated accordingly by :mod:`paperless_rearchive.archive.db`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)


def md5_of_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.md5(usedforsecurity=False)  # noqa: S324 - paperless uses MD5
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


class ArchiveReplaceError(RuntimeError):
    """The archive could not be replaced safely."""


def verify_current_checksum(archive_path: Path, expected: str | None) -> None:
    """Refuse to touch the archive if it changed underneath us."""
    if not archive_path.is_file():
        raise ArchiveReplaceError(f"Archive file not found: {archive_path}")
    if not expected:
        log.warning("No archive_checksum known for %s; proceeding without verification.", archive_path)
        return
    current = md5_of_file(archive_path)
    if current != expected:
        raise ArchiveReplaceError(
            f"Checksum mismatch for {archive_path}: on disk {current!r} != DB {expected!r}. "
            "The archive changed since it was fetched; refusing to replace it."
        )


def replace_archive(
    archive_path: Path,
    new_pdf: Path,
    *,
    keep_backup: bool = True,
) -> str:
    """Atomically replace ``archive_path`` with ``new_pdf``.

    Returns the MD5 checksum of the newly written file. The previous archive
    version is preserved as ``<name>.bak-<timestamp>`` next to it.
    """
    backup_path: Path | None = None
    if keep_backup:
        backup_path = archive_path.with_name(
            f"{archive_path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        shutil.copy2(archive_path, backup_path)
        log.info("Backed up old archive to %s", backup_path)

    # Stage on the same filesystem so os.replace() is atomic.
    staged = archive_path.with_name(f".{archive_path.name}.rearch-tmp")
    shutil.copy2(new_pdf, staged)
    os.replace(staged, archive_path)

    checksum = md5_of_file(archive_path)
    log.info("Replaced %s (new md5 %s)", archive_path, checksum)
    return checksum
