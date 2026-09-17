"""Environment-driven configuration."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from paperless_rearchive.secrets import secret, secret_or_default

log = logging.getLogger(__name__)


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        logging.getLogger(__name__).warning("Invalid integer for %s; using %d", name, default)
        return default


def _is_within(path: Path, parent: Path) -> bool:
    """True when ``path`` is ``parent`` itself or lives underneath it.

    Both sides are resolved first so symlinks, ``..`` and trailing slashes
    cannot disguise e.g. ``/archives/link-to-self`` as a distinct directory.
    """
    resolved = path.resolve()
    root = parent.resolve()
    return resolved == root or root in resolved.parents


@dataclass(frozen=True)
class DbSettings:
    """Postgres connection for the archive_checksum UPDATE."""

    host: str
    port: int
    dbname: str
    user: str
    password: str = field(repr=False, default="")

    @classmethod
    def from_env(cls) -> DbSettings:
        return cls(
            host=_env("PAPERLESS_DBHOST", "postgres"),
            port=_env_int("PAPERLESS_DBPORT", 5432),
            dbname=_env("PAPERLESS_DBNAME", "paperless"),
            # Values may be supplied directly or via the *_FILE secret files
            # (paperless-ngx convention).
            user=secret_or_default("PAPERLESS_DBUSER", "paperless"),
            password=secret("PAPERLESS_DBPASS"),
        )


@dataclass(frozen=True)
class Settings:
    paperless_url: str
    api_token: str
    archive_dir: Path
    #: Where replaced archives are backed up. Required: backups must live
    #: outside the archive tree (enforced by :meth:`validate`), never next to
    #: the archives in paperless's media directory.
    backup_dir: Path

    trigger_tag_content: str
    trigger_tag_all: str
    success_suffix: str
    failure_suffix: str

    provider_name: str
    ocr_language: str
    ocr_mode: str  # auto | force | redo | off
    ocr_clean: str  # clean | final | none
    ocr_deskew: bool
    ocr_rotate: bool
    ocr_rotate_threshold: float
    #: Render DPI for the content-only fast path (PyMuPDF page render before
    #: the Chandra call). Archive-mode runs rasterise inside ocrmypdf instead.
    ocr_dpi: int
    ocr_output_type: str
    ocr_user_args: dict[str, object]
    archive_for_images: bool

    write_provenance: bool

    poll_interval: float
    batch_limit: int
    concurrency: int
    max_pages: int
    dry_run: bool
    run_once: bool
    log_level: str

    db: DbSettings = field(repr=False, default_factory=DbSettings)

    @property
    def needs_db(self) -> bool:
        return bool(self.trigger_tag_all)

    def validate(self) -> None:
        """Refuse obviously unsafe configurations (called once at startup).

        ``REARCHIVE_BACKUP_DIRECTORY`` must not be at or below the archive
        directory: paperless-ngx's health check (``sanity_checker``) walks
        ``PAPERLESS_MEDIA_ROOT`` and cannot tell sidecar backups apart from
        orphaned files, so backups written into the archive tree produce
        warnings like::

            Orphaned file in media dir: .../documents/archive/....pdf.bak-...

        Anything at or below the archive directory is therefore rejected.
        """
        if _is_within(self.backup_dir, self.archive_dir):
            raise ValueError(
                "REARCHIVE_BACKUP_DIRECTORY resolves to the archive directory "
                f"{self.archive_dir} itself or a directory inside it "
                f"({self.backup_dir}). paperless-ngx's health check then "
                "reports every backup as an orphaned file in the media "
                "directory. Point REARCHIVE_BACKUP_DIRECTORY at a directory "
                "outside the archive tree - typically a separate bind mount, "
                "possibly another disk."
            )
        if self.backup_dir.exists() and not self.backup_dir.is_dir():
            raise ValueError(
                f"REARCHIVE_BACKUP_DIRECTORY {self.backup_dir} exists but is "
                "not a directory."
            )

    def prepare_backup_dir(self) -> None:
        """Create/verify the backup directory (idempotent, startup-time).

        Called after :meth:`validate` so a bad mount surfaces before hours of
        OCR work rather than at the first archive replacement. Dry runs never
        write, so in dry-run mode a missing directory is only reported.
        """
        if not self.backup_dir.exists():
            if self.dry_run:
                log.warning(
                    "DRY-RUN: REARCHIVE_BACKUP_DIRECTORY %s does not exist; "
                    "it would be created on the first archive replacement.",
                    self.backup_dir,
                )
                return
            try:
                self.backup_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise ValueError(
                    f"REARCHIVE_BACKUP_DIRECTORY {self.backup_dir} could not "
                    f"be created: {e}"
                ) from e
            log.info("created backup directory %s", self.backup_dir)
        if not os.access(self.backup_dir, os.W_OK | os.X_OK):
            raise ValueError(
                f"REARCHIVE_BACKUP_DIRECTORY {self.backup_dir} is not writable "
                "by this process."
            )
        self._log_backup_filesystem()

    def _log_backup_filesystem(self) -> None:
        """Report whether backups cross a filesystem boundary (different disk)."""
        try:
            backup_dev = os.stat(self.backup_dir).st_dev
            archive_dev = os.stat(self.archive_dir).st_dev
        except OSError as e:
            log.warning(
                "could not stat archive (%s) / backup (%s) directory: %s",
                self.archive_dir,
                self.backup_dir,
                e,
            )
            return
        if backup_dev != archive_dev:
            log.info(
                "backup directory %s is on a different filesystem (device %d) "
                "than the archive directory %s (device %d); backups are copied "
                "across, at the cost of a full file copy per replaced archive",
                self.backup_dir,
                backup_dev,
                self.archive_dir,
                archive_dev,
            )

    @classmethod
    def from_env(cls) -> Settings:
        raw_user_args = _env("REARCHIVE_OCR_USER_ARGS", "")
        try:
            user_args: dict[str, object] = json.loads(raw_user_args) if raw_user_args else {}
        except json.JSONDecodeError as e:
            raise ValueError(f"REARCHIVE_OCR_USER_ARGS is not valid JSON: {e}") from e
        if not isinstance(user_args, dict):
            raise ValueError("REARCHIVE_OCR_USER_ARGS must decode to a JSON object")
        backup_raw = _env("REARCHIVE_BACKUP_DIRECTORY", "")
        if not backup_raw:
            raise ValueError(
                "REARCHIVE_BACKUP_DIRECTORY is required: replaced archives are "
                "backed up there before the new file is put in place. Point it "
                "at a directory OUTSIDE the archive tree (REARCHIVE_ARCHIVE_DIR) "
                "- typically a separate bind mount, possibly another disk - so "
                "paperless-ngx's health check does not report the backups as "
                "orphaned files in the media directory."
            )
        return cls(
            paperless_url=_env("PAPERLESS_BASE_URL", "http://paperless:8000").rstrip("/"),
            api_token=secret("PAPERLESS_API_TOKEN"),
            archive_dir=Path(_env("REARCHIVE_ARCHIVE_DIR", "/archives")),
            backup_dir=Path(backup_raw) if backup_raw else None,
            trigger_tag_content=_env("REARCHIVE_TRIGGER_TAG_CONTENT", "re-ocr-content"),
            trigger_tag_all=_env("REARCHIVE_TRIGGER_TAG_ALL", "re-ocr-all"),
            success_suffix=_env("REARCHIVE_SUCCESS_SUFFIX", "-success"),
            failure_suffix=_env("REARCHIVE_FAILURE_SUFFIX", "-failure"),
            provider_name=_env("REARCHIVE_PROVIDER", "chandra"),
            ocr_language=_env("REARCHIVE_OCR_LANGUAGE", "eng"),
            ocr_mode=_env("REARCHIVE_OCR_MODE", "redo").strip().lower(),
            ocr_clean=_env("REARCHIVE_OCR_CLEAN", "clean").strip().lower(),
            ocr_deskew=_env_bool("REARCHIVE_OCR_DESKEW", True),
            ocr_rotate=_env_bool("REARCHIVE_OCR_ROTATE_PAGES", True),
            ocr_rotate_threshold=float(_env("REARCHIVE_OCR_ROTATE_PAGES_THRESHOLD", "12.0")),
            ocr_dpi=_env_int("REARCHIVE_OCR_DPI", 300),
            ocr_output_type=_env("REARCHIVE_OCR_OUTPUT_TYPE", "pdfa"),
            ocr_user_args=user_args,
            archive_for_images=_env_bool("REARCHIVE_ARCHIVE_FOR_IMAGES", False),
            write_provenance=_env_bool("REARCHIVE_WRITE_PROVENANCE", True),
            poll_interval=float(_env("REARCHIVE_POLL_INTERVAL", "300")),
            batch_limit=_env_int("REARCHIVE_BATCH_LIMIT", 5),
            concurrency=_env_int("REARCHIVE_OCR_CONCURRENCY", 1),
            max_pages=_env_int("REARCHIVE_MAX_PAGES", 0),
            dry_run=_env_bool("REARCHIVE_DRY_RUN", False),
            run_once=_env_bool("REARCHIVE_RUN_ONCE", False),
            log_level=_env("REARCHIVE_LOG_LEVEL", "INFO").upper(),
            db=DbSettings.from_env(),
        )
