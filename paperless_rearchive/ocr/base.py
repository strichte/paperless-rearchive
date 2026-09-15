"""Abstract OCR provider plugin.

A provider encapsulates everything LLM-specific:

* the ocrmypdf plugin module that supplies the ``OcrEngine`` (hOCR + invisible
  text layer rendering + markdown sidecar),
* the kwargs forwarded to :func:`ocrmypdf.ocr`,
* a fail-fast configuration probe.

Adding a new LLM means writing an ocrmypdf plugin (an ``OcrEngine``) plus a
small :class:`OcrProviderPlugin` wrapper registered in :data:`PROVIDERS`.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

log = logging.getLogger(__name__)


class OcrProviderPlugin(ABC):
    """Base class for OCR engine providers."""

    #: provider id used by REARCHIVE_PROVIDER
    name: str = ""

    #: module path passed as plugins=[...] to ocrmypdf.ocr()
    ocrmypdf_plugin_module: str = ""

    @abstractmethod
    def ocrmypdf_kwargs(self) -> dict[str, Any]:
        """Kwargs merged into every ocrmypdf.ocr() call."""

    def validate(self) -> None:
        """Fail fast on misconfiguration. Default: no-op."""


#: Registry of available providers, populated lazily so optional
#: dependencies (paperless-chandra) are only imported when selected.
PROVIDERS: dict[str, str] = {
    "chandra": "paperless_rearchive.ocr.chandra:ChandraProvider",
}


def get_provider(name: str) -> OcrProviderPlugin:
    """Instantiate the provider registered under ``name``."""
    spec = PROVIDERS.get(name)
    if spec is None or ":" not in spec:
        raise ValueError(
            f"Unknown REARCHIVE_PROVIDER={name!r}. Available: {sorted(PROVIDERS)}"
        )
    module_path, _, class_name = spec.partition(":")
    import importlib

    cls = getattr(importlib.import_module(module_path), class_name)
    provider: OcrProviderPlugin = cls()
    provider.validate()
    log.info("Using OCR provider %r (%s)", name, cls.__name__)
    return provider
