"""Direct Postgres access for the archive_checksum UPDATE.

Connects to the paperless Postgres container over the backend network using
the same secret as paperless itself (``paperless_db_paperless_passwd``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from paperless_rearchive.config import DbSettings

log = logging.getLogger(__name__)


def fetch_archive_filename(db: "DbSettings", document_id: int) -> str | None:
    """SELECT archive_filename ... (the real on-disk path relative to ARCHIVE_DIR).

    The REST API's ``archived_file_name`` is a flattened display/download name
    (spaces instead of template subdirectories) and must NOT be used to locate
    the file on the bind mount.
    """
    try:
        import psycopg
    except ImportError as e:
        raise RuntimeError(
            "psycopg is not installed. Install with: pip install 'paperless-rearchive[db]'"
        ) from e

    if not db.password:
        raise RuntimeError(
            "PAPERLESS_DBPASS_FILE is not set or empty; cannot read archive_filename."
        )
    with psycopg.connect(
        host=db.host,
        port=db.port,
        dbname=db.dbname,
        user=db.user,
        password=db.password,
        connect_timeout=10,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT archive_filename FROM documents_document WHERE id = %s",
                (document_id,),
            )
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError(f"Document {document_id} does not exist in the database.")
    return row[0]


def fetch_archive_checksum(db: "DbSettings", document_id: int) -> str | None:
    """SELECT archive_checksum ... (not exposed by the REST API serializer)."""
    try:
        import psycopg
    except ImportError as e:
        raise RuntimeError(
            "psycopg is not installed. Install with: pip install 'paperless-rearchive[db]'"
        ) from e

    if not db.password:
        raise RuntimeError(
            "PAPERLESS_DBPASS_FILE is not set or empty; cannot read archive_checksum."
        )
    with psycopg.connect(
        host=db.host,
        port=db.port,
        dbname=db.dbname,
        user=db.user,
        password=db.password,
        connect_timeout=10,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT archive_checksum FROM documents_document WHERE id = %s",
                (document_id,),
            )
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError(f"Document {document_id} does not exist in the database.")
    return row[0]


def update_archive_checksum(db: "DbSettings", document_id: int, checksum: str) -> None:
    """UPDATE documents_document SET archive_checksum = %s WHERE id = %s."""
    try:
        import psycopg
    except ImportError as e:
        raise RuntimeError(
            "psycopg is not installed. Install with: pip install 'paperless-rearchive[db]'"
        ) from e

    if not db.password:
        raise RuntimeError(
            "PAPERLESS_DBPASS_FILE is not set or empty; cannot update archive_checksum."
        )

    query = "UPDATE documents_document SET archive_checksum = %s WHERE id = %s"
    with psycopg.connect(
        host=db.host,
        port=db.port,
        dbname=db.dbname,
        user=db.user,
        password=db.password,
        connect_timeout=10,
    ) as conn:
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
