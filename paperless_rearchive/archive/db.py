"""Direct Postgres access for checksum reads and the archive_checksum UPDATE.

Connects to the paperless Postgres container over the backend network using
the same secret as paperless itself (``paperless_db_paperless_passwd``).

Reads are needed because the REST API does not expose ``archive_checksum``,
``archive_filename`` or ``checksum``: ``archived_file_name`` is a flattened
display name and cannot locate the file on the bind mount.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from paperless_rearchive.config import DbSettings

log = logging.getLogger(__name__)

_INSTALL_HINT = "psycopg is not installed. Install with: pip install 'paperless-rearchive[db]'"


@contextmanager
def _connect(db: DbSettings) -> Iterator[Any]:
    """Yield a psycopg connection, or raise RuntimeError if unusable."""
    try:
        import psycopg
    except ImportError as e:
        raise RuntimeError(_INSTALL_HINT) from e

    if not db.password:
        raise RuntimeError(
            "PAPERLESS_DBPASS_FILE is not set or empty; cannot access the database."
        )
    with psycopg.connect(
        host=db.host,
        port=db.port,
        dbname=db.dbname,
        user=db.user,
        password=db.password,
        connect_timeout=10,
    ) as conn:
        yield conn


def _fetch_scalar(db: DbSettings, column: str, document_id: int) -> str | None:
    """SELECT one column from documents_document for a single document."""
    if column not in ("archive_filename", "archive_checksum", "checksum"):
        raise ValueError(f"Unsupported column {column!r}")
    with _connect(db) as conn, conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {column} FROM documents_document WHERE id = %s",  # noqa: S608
            (document_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError(f"Document {document_id} does not exist in the database.")
    return row[0]


def fetch_archive_filename(db: DbSettings, document_id: int) -> str | None:
    """SELECT archive_filename (the on-disk path relative to ARCHIVE_DIR).

    The REST API's ``archived_file_name`` is a flattened display/download name
    (spaces instead of template subdirectories) and must NOT be used to locate
    the file on the bind mount.
    """
    return _fetch_scalar(db, "archive_filename", document_id)


def fetch_archive_checksum(db: DbSettings, document_id: int) -> str | None:
    """SELECT archive_checksum (sha256 of the archive file on disk)."""
    return _fetch_scalar(db, "archive_checksum", document_id)


def fetch_original_checksum(db: DbSettings, document_id: int) -> str | None:
    """SELECT checksum (sha256 of the immutable original file).

    Verifies that the bytes downloaded from the API really are the original:
    ``/download/`` silently serves the *archive* unless ``?original=true``.
    """
    return _fetch_scalar(db, "checksum", document_id)


def update_archive_checksum(db: DbSettings, document_id: int, checksum: str) -> None:
    """UPDATE documents_document SET archive_checksum = %s WHERE id = %s."""
    query = "UPDATE documents_document SET archive_checksum = %s WHERE id = %s"
    with _connect(db) as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, (checksum, document_id))
            if cursor.rowcount != 1:
                conn.rollback()
                raise RuntimeError(
                    f"archive_checksum UPDATE matched {cursor.rowcount} rows for "
                    f"document {document_id}; expected 1. Rolled back."
                )
        conn.commit()
    log.info(
        "Updated archive_checksum for document %d in database (sha256 %s)",
        document_id,
        checksum,
    )
