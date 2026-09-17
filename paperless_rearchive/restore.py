"""Restore archives and/or the ``content`` field from ``.bak-*`` backups.

The re-OCR pipeline keeps the pre-replacement archive version as
``<name>.pdf.bak-<timestamp>`` below ``REARCHIVE_BACKUP_DIRECTORY`` (mirroring
the archive's template sub-directories). This tool restores from those backups:

* archive restore: copy the backup over the live archive file on the
  ``/archives`` bind mount, then ``UPDATE documents_document SET
  archive_checksum`` (the REST API cannot replace an archive).
* content restore: ``pdftotext -q -layout -enc UTF-8`` on the *backed-up*
  archive, cleaned with :func:`post_process_text` (same normalization the
  paperless-ngx Tesseract parser applies), then ``PATCH`` the document's
  ``content`` via the REST API.

Both the document lookup (id / ``archive_filename`` -> doc) and the checksum
update use the paperless Postgres database directly, because the REST API
does not expose ``archive_filename`` or ``archive_checksum``.

Anything that would overwrite live state asks for confirmation first, unless
``-f/--force`` is given.
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from paperless_rearchive import __version__
from paperless_rearchive.archive import db as archive_db
from paperless_rearchive.archive.replacer import checksum_of_file
from paperless_rearchive.config import DbSettings, Settings
from paperless_rearchive.logging_setup import configure_logging
from paperless_rearchive.paperless_api import PaperlessAPI, PaperlessError

log = logging.getLogger("rearchive.restore")

#: Backup suffix pattern: ``<archive-name>.bak-<timestamp>`` (see
#: :func:`paperless_rearchive.archive.replacer.backup_destination`).
BACKUP_SUFFIX_RE = re.compile(r"^(?P<archive_name>.+\.pdf)\.bak-(?P<stamp>.+)$")


def post_process_text(text: str | None) -> str | None:
    """Normalize extracted PDF text the way paperless-ngx does.

    Mirrors ``paperless.parsers.utils.post_process_text`` (paperless-ngx
    3.1.3): collapse non-line-break whitespace runs to a single space, drop
    indentation after line breaks, strip surrounding/trailing whitespace, and
    replace NUL bytes (PostgreSQL text fields reject them).
    """
    if not text:
        return None

    collapsed_spaces = re.sub(r"([^\S\r\n]+)", " ", text)
    no_leading_whitespace = re.sub(r"([\n\r]+)([^\S\n\r]+)", "\\1", collapsed_spaces)
    no_trailing_whitespace = re.sub(r"([^\S\n\r]+)$", "", no_leading_whitespace)

    result = no_trailing_whitespace.strip().replace("\0", " ")
    return result or None


def extract_text_from_pdf(path: Path) -> str:
    """Extract text with ``pdftotext -q -layout -enc UTF-8``, cleaned.

    Raises:
        FileNotFoundError: the backup PDF does not exist.
        RuntimeError: ``pdftotext`` is missing or the extraction failed /
            produced no usable text.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Backup file not found: {path}")
    if shutil.which("pdftotext") is None:
        raise RuntimeError(
            "pdftotext is not installed in this container "
            "(expected from the poppler-utils package)."
        )
    completed = None
    try:
        completed = subprocess.run(
            ["pdftotext", "-q", "-layout", "-enc", "UTF-8", str(path), "-"],
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"pdftotext failed for {path}: {e}") from e
    assert completed is not None
    try:
        raw = completed.stdout.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RuntimeError(f"pdftotext output for {path} is not valid UTF-8: {e}") from e
    text = post_process_text(raw)
    if not text:
        raise RuntimeError(f"pdftotext produced no usable text for {path}.")
    return text


@dataclass(frozen=True)
class BackupCandidate:
    """One ``.bak-*`` file together with the archive name it was taken from."""

    path: Path
    archive_name: str


def find_backups_for_archive(backup_dir: Path, archive_name: str) -> list[BackupCandidate]:
    """Return all backups of ``archive_name``, oldest first.

    ``backup_destination`` mirrors the archive's template sub-directories below
    ``backup_dir``, so the search is recursive: a backup of
    ``Travel/2024/a.pdf`` lives at ``<backup_dir>/Travel/2024/a.pdf.bak-...``.
    Matches are sorted by path so repeated runs present versions
    deterministically.
    """
    if not backup_dir.is_dir():
        return []
    candidates: list[BackupCandidate] = []
    basename = Path(archive_name).name
    for path in sorted(backup_dir.rglob(f"{basename}.bak-*")):
        if not path.is_file():
            continue
        match = BACKUP_SUFFIX_RE.match(path.name)
        if not match or match.group("archive_name") != basename:
            continue
        # The backup mirrors the archive's sub-directory layout; only accept
        # the one whose path below backup_dir reproduces archive_name.
        try:
            rel_parent = path.relative_to(backup_dir).parent
        except ValueError:
            continue
        if str(rel_parent / basename) != archive_name:
            continue
        candidates.append(BackupCandidate(path=path, archive_name=archive_name))
    return candidates


def _archive_name_of(backup_path: Path, backup_dir: Path | None = None) -> str:
    """Recover the archive file name a ``.bak-*`` file was taken from.

    The replacer mirrors the archive's sub-directory layout below the backup
    directory, so when ``backup_dir`` is known the full ``archive_filename``
    (e.g. ``Travel/2024/a.pdf``) is recovered; otherwise just the basename.
    """
    match = BACKUP_SUFFIX_RE.match(backup_path.name)
    basename = match.group("archive_name") if match else backup_path.name.split(".bak-")[0]
    if backup_dir is not None:
        try:
            rel_parent = backup_path.relative_to(backup_dir).parent
            if str(rel_parent) != ".":
                return str(rel_parent / basename)
        except ValueError:
            pass
    return basename


def _match_backup_by_name(backup_dir: Path, name: str) -> list[BackupCandidate]:
    """Match ``name`` against backups below ``backup_dir``.

    Accepts a full ``<archive>.pdf.bak-<stamp>`` name (as stored or as a path
    whose basename is used), a bare ``<archive>.pdf`` name (all its versions),
    or a glob. Returns at most one entry per distinct backup file.
    """
    if not backup_dir.is_dir():
        return []
    by_path: dict[Path, BackupCandidate] = {}
    wanted = Path(name).name
    has_wildcards = any(ch in wanted for ch in "*?[")
    if BACKUP_SUFFIX_RE.match(wanted) and not has_wildcards:
        for path in sorted(backup_dir.rglob(wanted)):
            if path.is_file():
                archive_name = _archive_name_of(path, backup_dir)
                by_path[path] = BackupCandidate(path=path, archive_name=archive_name)
    else:
        pattern = wanted if has_wildcards else f"{wanted}.bak-*"
        for path in sorted(backup_dir.rglob("*.bak-*")):
            if not path.is_file():
                continue
            archive_name = _archive_name_of(path, backup_dir)
            if fnmatch(path.name, pattern) or fnmatch(archive_name, wanted):
                by_path[path] = BackupCandidate(path=path, archive_name=archive_name)
    return [by_path[path] for path in sorted(by_path)]


def _doc_id_for_backup(candidate: BackupCandidate, db: DbSettings) -> int:
    """Find the document whose ``archive_filename`` matches the backup."""
    try:
        import psycopg
    except ImportError as e:
        raise RuntimeError(
            "psycopg is not installed. Install with: pip install 'paperless-rearchive[db]'"
        ) from e

    with (
        psycopg.connect(
            host=db.host,
            port=db.port,
            dbname=db.dbname,
            user=db.user,
            password=db.password,
            connect_timeout=10,
        ) as conn,
        conn.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT id FROM documents_document WHERE archive_filename = %s",
            (candidate.archive_name,),
        )
        rows = cursor.fetchall()
    if not rows:
        raise RuntimeError(
            f"No document with archive_filename {candidate.archive_name!r} "
            f"(from backup {candidate.path}) exists in the database."
        )
    if len(rows) > 1:
        raise RuntimeError(
            f"Multiple documents share archive_filename {candidate.archive_name!r}: "
            + ", ".join(str(r[0]) for r in rows)
        )
    return int(rows[0][0])


def resolve_operand_to_doc_id(operand: str, *, backup_dir: Path, db: DbSettings) -> int:
    """Resolve one CLI operand to a paperless document id.

    A bare number is a document id. Anything else is a backup file name: it is
    matched against ``*.bak-*`` files below ``backup_dir`` (exact name first,
    then basename/glob), and the owning document is found via the database
    (the ``.bak-`` suffix is stripped to recover ``archive_filename``).
    """
    if operand.isdigit():
        return int(operand)
    matches = _match_backup_by_name(backup_dir, operand)
    if not matches:
        raise RuntimeError(
            f"No backup matching {operand!r} found below {backup_dir}. "
            "Pass a numeric document id or a backup file name."
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"Ambiguous backup name {operand!r}; matches: "
            + ", ".join(str(m.path) for m in matches)
        )
    return _doc_id_for_backup(matches[0], db)


@dataclass(frozen=True)
class RestorePlan:
    """Everything needed to restore one document from one backup."""

    doc_id: int
    backup: Path
    archive_path: Path


def plan_restore(doc_id: int, backup_path: Path | None, settings: Settings) -> RestorePlan:
    """Locate the live archive file and pick the backup version to restore.

    When ``backup_path`` is given it is used as-is. Otherwise all backups of
    the document's ``archive_filename`` are collected and - when more than one
    exists - the operator picks one interactively.
    """
    archive_name = archive_db.fetch_archive_filename(settings.db, doc_id)
    if not archive_name:
        raise RuntimeError(f"Document {doc_id} has no archive file (archive_filename is NULL).")
    archive_path = settings.archive_dir / archive_name
    if backup_path is not None:
        if not backup_path.is_file():
            raise RuntimeError(f"Backup file not found: {backup_path}")
        return RestorePlan(doc_id=doc_id, backup=backup_path, archive_path=archive_path)

    candidates = find_backups_for_archive(settings.backup_dir, archive_name)
    if not candidates:
        raise RuntimeError(
            f"No backups of {archive_name!r} (document {doc_id}) found below {settings.backup_dir}."
        )
    if len(candidates) == 1:
        log.info("Using the only backup for document %d: %s", doc_id, candidates[0].path)
        return RestorePlan(doc_id=doc_id, backup=candidates[0].path, archive_path=archive_path)
    return RestorePlan(
        doc_id=doc_id,
        backup=choose_backup(doc_id, candidates),
        archive_path=archive_path,
    )


def _prompt(text: str) -> str:
    """Read one line from stdin; abort cleanly when stdin is closed."""
    try:
        return input(text)
    except (EOFError, KeyboardInterrupt):
        raise RuntimeError("Aborted (no input - run interactively or use -f).") from None


def choose_backup(doc_id: int, candidates: list[BackupCandidate]) -> Path:
    """Ask the operator which of several backup versions to restore."""
    print(f"Document {doc_id} has {len(candidates)} backups:")
    for index, candidate in enumerate(candidates, start=1):
        print(f"  [{index}] {candidate.path}")
    while True:
        answer = _prompt(f"Choose a backup [1-{len(candidates)}]: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1].path
        print("Please enter one of the listed numbers.", file=sys.stderr)


def confirm(prompt: str, *, force: bool) -> bool:
    """Ask for confirmation unless ``-f/--force`` was given."""
    if force:
        return True
    answer = _prompt(f"{prompt} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def restore_archive(plan: RestorePlan, settings: Settings, *, force: bool) -> bool:
    """Copy the backup over the live archive and update ``archive_checksum``.

    Returns True when the archive was restored, False when skipped.
    """
    if not confirm(
        f"Overwrite archive {plan.archive_path} with backup {plan.backup}?",
        force=force,
    ):
        print(f"Skipped archive restore for document {plan.doc_id}.")
        return False
    if not plan.archive_path.is_file():
        raise RuntimeError(f"Live archive file not found: {plan.archive_path}")
    plan.archive_path.parent.mkdir(parents=True, exist_ok=True)
    staged = plan.archive_path.with_name(f".{plan.archive_path.name}.restore-tmp")
    shutil.copy2(plan.backup, staged)
    staged.replace(plan.archive_path)
    checksum = checksum_of_file(plan.archive_path)
    archive_db.update_archive_checksum(settings.db, plan.doc_id, checksum)
    print(f"Document {plan.doc_id}: archive restored from {plan.backup} (sha256 {checksum}).")
    return True


def restore_content(
    plan: RestorePlan, api: PaperlessAPI, *, force: bool, content_text: str | None = None
) -> bool:
    """Re-extract the backup's text and PATCH the document's ``content``.

    Returns True when the content field was restored, False when skipped.
    """
    if not confirm(
        f"Overwrite the content field of document {plan.doc_id} "
        f"with text extracted from {plan.backup}?",
        force=force,
    ):
        print(f"Skipped content restore for document {plan.doc_id}.")
        return False
    text = content_text if content_text is not None else extract_text_from_pdf(plan.backup)
    api.patch_content(plan.doc_id, text)
    print(
        f"Document {plan.doc_id}: content field restored "
        f"({len(text)} characters from {plan.backup})."
    )
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="restore_backup",
        description=(
            "Restore a paperless-ngx document's archive file and/or content "
            "field from a .bak-<timestamp> backup created by paperless-rearchive."
        ),
    )
    parser.add_argument(
        "-a",
        "--archive-only",
        action="store_true",
        help="Restore only the archive file.",
    )
    parser.add_argument(
        "-c",
        "--content-only",
        action="store_true",
        help="Restore only the content field.",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite without asking for confirmation.",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show paperless-rearchive's version and exit.",
    )
    parser.add_argument(
        "operands",
        nargs="+",
        metavar="DOC_ID|BACKUP",
        help=(
            "One or more numeric document IDs or backup file names "
            "(basename, '<name>.pdf.bak-<stamp>' or glob, matched below "
            "REARCHIVE_BACKUP_DIRECTORY)."
        ),
    )
    return parser


def _plan_for_operand(operand: str, settings: Settings) -> RestorePlan:
    """Resolve one operand to a :class:`RestorePlan`.

    Numeric operands are document ids (backup version chosen interactively when
    several exist). An unambiguous full ``.bak-<stamp>`` backup name restores
    exactly that version; anything else falls back to the document's
    interactive backup choice.
    """
    if operand.isdigit():
        return plan_restore(int(operand), None, settings)
    matches = _match_backup_by_name(settings.backup_dir, operand)
    if not matches:
        raise RuntimeError(f"No backup matching {operand!r} found below {settings.backup_dir}.")
    if (
        len(matches) == 1
        and BACKUP_SUFFIX_RE.match(Path(operand).name)
        and not any(ch in operand for ch in "*?[")
    ):
        doc_id = _doc_id_for_backup(matches[0], settings.db)
        return plan_restore(doc_id, matches[0].path, settings)
    # Anything else (bare archive name, glob, ambiguous name): resolve to one
    # document, then go through the interactive per-document backup choice.
    # resolve_operand_to_doc_id raises on ambiguous names, so the operator is
    # never silently pointed at one of several same-named backups.
    doc_id = resolve_operand_to_doc_id(operand, backup_dir=settings.backup_dir, db=settings.db)
    return plan_restore(doc_id, None, settings)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.archive_only and args.content_only:
        parser.error("-a/--archive-only and -c/--content-only are mutually exclusive.")

    try:
        settings = Settings.from_env()
    except ValueError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)

    restore_archive_part = not args.content_only
    restore_content_part = not args.archive_only

    try:
        api = PaperlessAPI(settings.paperless_url, settings.api_token)
    except PaperlessError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    failures = 0
    for operand in args.operands:
        try:
            plan = _plan_for_operand(operand, settings)
            if restore_archive_part:
                restore_archive(plan, settings, force=args.force)
            if restore_content_part:
                restore_content(plan, api, force=args.force)
        except (RuntimeError, PaperlessError, OSError) as e:
            print(f"error: {operand!r}: {e}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
