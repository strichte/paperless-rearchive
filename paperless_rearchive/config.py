"""Environment-driven configuration."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from paperless_rearchive.secrets import secret, secret_or_default


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

    trigger_tag_content: str
    trigger_tag_all: str
    success_suffix: str
    failure_suffix: str

    provider_name: str
    ocr_language: str
    ocr_mode: str  # auto | force | redo
    ocr_deskew: bool
    ocr_output_type: str
    ocr_user_args: dict[str, object]
    archive_for_images: bool

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

    @classmethod
    def from_env(cls) -> Settings:
        raw_user_args = _env("REARCHIVE_OCR_USER_ARGS", "")
        try:
            user_args: dict[str, object] = json.loads(raw_user_args) if raw_user_args else {}
        except json.JSONDecodeError as e:
            raise ValueError(f"REARCHIVE_OCR_USER_ARGS is not valid JSON: {e}") from e
        if not isinstance(user_args, dict):
            raise ValueError("REARCHIVE_OCR_USER_ARGS must decode to a JSON object")
        return cls(
            paperless_url=_env("PAPERLESS_BASE_URL", "http://paperless:8000").rstrip("/"),
            api_token=secret("PAPERLESS_API_TOKEN"),
            archive_dir=Path(_env("REARCHIVE_ARCHIVE_DIR", "/archives")),
            trigger_tag_content=_env("REARCHIVE_TRIGGER_TAG_CONTENT", "re-ocr-content"),
            trigger_tag_all=_env("REARCHIVE_TRIGGER_TAG_ALL", "re-ocr-all"),
            success_suffix=_env("REARCHIVE_SUCCESS_SUFFIX", "-success"),
            failure_suffix=_env("REARCHIVE_FAILURE_SUFFIX", "-failure"),
            provider_name=_env("REARCHIVE_PROVIDER", "chandra"),
            ocr_language=_env("REARCHIVE_OCR_LANGUAGE", "eng"),
            ocr_mode=_env("REARCHIVE_OCR_MODE", "auto").strip().lower(),
            ocr_deskew=_env_bool("REARCHIVE_OCR_DESKEW", True),
            ocr_output_type=_env("REARCHIVE_OCR_OUTPUT_TYPE", "pdfa"),
            ocr_user_args=user_args,
            archive_for_images=_env_bool("REARCHIVE_ARCHIVE_FOR_IMAGES", False),
            poll_interval=float(_env("REARCHIVE_POLL_INTERVAL", "300")),
            batch_limit=_env_int("REARCHIVE_BATCH_LIMIT", 5),
            concurrency=_env_int("REARCHIVE_OCR_CONCURRENCY", 2),
            max_pages=_env_int("REARCHIVE_MAX_PAGES", 0),
            dry_run=_env_bool("REARCHIVE_DRY_RUN", False),
            run_once=_env_bool("REARCHIVE_RUN_ONCE", False),
            log_level=_env("REARCHIVE_LOG_LEVEL", "INFO").upper(),
            db=DbSettings.from_env(),
        )
