"""Checksum helpers and atomic archive replacement.

The paperless REST API cannot replace the archive version of an existing
document, so the sidecar bind-mounts ``media/documents/archive`` and replaces
the file directly. paperless computes ``archive_checksum`` with
``documents.utils.compute_checksum`` — SHA-256 of the file bytes (verified
against the running instance); the database row is updated accordingly by
:mod:`paperless_rearchive.archive.db`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)


def checksum_of_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """SHA-256 of file bytes — identical to paperless's ``compute_checksum``.

    Note: paperless-ngx uses SHA-256 here (older releases used MD5); this
    matches the running instance's ``archive_checksum`` values (verified).
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


# Backwards-compatible alias used by earlier revisions / docs.
md5_of_file = checksum_of_file


class ArchiveReplaceError(RuntimeError):
    """The archive could not be replaced safely."""


def verify_current_checksum(archive_path: Path, expected: str | None) -> None:
    """Refuse to touch the archive if it changed underneath us."""
    if not archive_path.is_file():
        raise ArchiveReplaceError(f"Archive file not found: {archive_path}")
    if not expected:
        log.warning("No archive_checksum known for %s; proceeding without verification.", archive_path)
        return
    current = checksum_of_file(archive_path)
    if current != expected:
        raise ArchiveReplaceError(
            f"Checksum mismatch for {archive_path}: on disk {current!r} != DB {expected!r}. "
            "The archive changed since it was fetched; refusing to replace it."
        )


def backup_destination(
    archive_path: Path,
    *,
    backup_dir: Path,
    archive_dir: Path | None = None,
) -> Path:
    """Return the ``<name>.bak-<timestamp>`` path for ``archive_path``.

    The archive's path relative to ``archive_dir`` is recreated below
    ``backup_dir``, so template sub-directories are preserved. An archive
    that cannot be related to ``archive_dir`` is stored flat.

    Raises:
        ArchiveReplaceError: if the computed destination would land inside
            the archive directory. Backups are **never** written there —
            this is a runtime backstop for mounts that turned out
            to overlap despite startup validation
            (e.g. a symlink created after boot).
    """
    name = f"{archive_path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    rel: Path | None = None
    if archive_dir is not None:
        try:
            rel = archive_path.resolve().relative_to(Path(archive_dir).resolve())
        except ValueError:
            log.warning(
                "archive %s is not inside %s; storing its backup flat",
                archive_path,
                archive_dir,
            )
    dest = (Path(backup_dir) / rel).with_name(name) if rel is not None else Path(backup_dir) / name
    _refuse_destination_inside_archive_dir(dest, archive_dir)
    return dest


def _refuse_destination_inside_archive_dir(dest: Path, archive_dir: Path | None) -> None:
    """Hard backstop: a backup destination inside the archive tree is fatal."""
    if archive_dir is None:
        return
    try:
        inside = dest.resolve().is_relative_to(Path(archive_dir).resolve())
    except OSError:  # unresolvable path - let the later mkdir/copy fail loudly
        return
    if inside:
        raise ArchiveReplaceError(
            f"Refusing to write a backup into the archive directory: {dest} "
            f"is inside {archive_dir}. Mount /archive-backups outside "
            "the archive tree."
        )


def replace_archive(
    archive_path: Path,
    new_pdf: Path,
    *,
    backup_dir: Path,
    archive_dir: Path | None = None,
) -> str:
    """Atomically replace ``archive_path`` with ``new_pdf``.

    Returns the SHA-256 checksum of the newly written file. The previous
    archive version is **always** preserved as ``<name>.bak-<timestamp>``
    below the fixed ``/archive-backups`` mount (see :func:`backup_destination`)
    — a backup is never skipped and never written into the archive directory.
    The backup is *copied*, so it may live on a different
    filesystem/disk; keeping it outside the media directory is what stops
    paperless-ngx's health check from reporting the backups as orphaned
    files.
    """
    backup_path = backup_destination(
        archive_path, backup_dir=backup_dir, archive_dir=archive_dir
    )
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    # copy (never move): the target may be another disk, and the source
    # must stay readable for verification until os.replace() runs.
    shutil.copy2(archive_path, backup_path)
    log.info("Backed up old archive to %s", backup_path)

    # Stage on the same filesystem so os.replace() is atomic.
    staged = archive_path.with_name(f".{archive_path.name}.rearch-tmp")
    shutil.copy2(new_pdf, staged)
    os.replace(staged, archive_path)

    checksum = checksum_of_file(archive_path)
    log.info("Replaced %s (new sha256 %s)", archive_path, checksum)
    return checksum
