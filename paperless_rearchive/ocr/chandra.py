"""Chandra OCR provider, backed by the paperless-chandra ocrmypdf plugin."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from paperless_rearchive.ocr.base import OcrProviderPlugin


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _read_secret_file(path: str) -> str:
    if not path:
        return ""
    return Path(path).read_text(encoding="utf-8").strip()


class ChandraProvider(OcrProviderPlugin):
    """Runs ocrmypdf with the paperless-chandra engine via its plugin module."""

    name = "chandra"
    ocrmypdf_plugin_module = "paperless_chandra.ocrmypdf_plugin"

    def __init__(self) -> None:
        # Fallbacks mirror the paperless-chandra defaults.
        self.server_url = _env("PAPERLESS_CHANDRA_SERVER_URL")
        self.model_name = _env("PAPERLESS_CHANDRA_MODEL_NAME", "chandra")
        self.api_key = (
            _env("PAPERLESS_CHANDRA_API_KEY")
            or _read_secret_file(_env("PAPERLESS_CHANDRA_API_KEY_FILE"))
        )
        self.content_format = _env("PAPERLESS_CHANDRA_CONTENT_FORMAT", "markdown")
        self.max_output_tokens = int(_env("PAPERLESS_CHANDRA_MAX_OUTPUT_TOKENS", "12384"))

    def ocrmypdf_kwargs(self) -> dict[str, Any]:
        return {
            "chandra_server_url": self.server_url,
            "chandra_model_name": self.model_name,
            "chandra_api_key": self.api_key,
            "chandra_max_output_tokens": self.max_output_tokens,
            "chandra_content_format": self.content_format,
        }

    def validate(self) -> None:
        if not self.server_url:
            raise ValueError(
                "Chandra provider requires PAPERLESS_CHANDRA_SERVER_URL "
                "(e.g. http://ai:8110/v1)."
            )
        if self.content_format not in ("markdown", "text"):
            raise ValueError(
                f"PAPERLESS_CHANDRA_CONTENT_FORMAT must be markdown or text, "
                f"got {self.content_format!r}"
            )
        try:
            import paperless_chandra.ocrmypdf_plugin  # noqa: F401
        except ImportError as e:
            raise ValueError(
                "paperless-chandra is not installed. Install with: "
                "pip install 'paperless-rearchive[chandra]'"
            ) from e
