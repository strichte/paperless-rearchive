"""Shared logging configuration for the poller and the ad-hoc runners.

Without this the container only surfaces ocrmypdf's own warnings (Python's
``logging.lastResort`` prints WARNING+ to stderr and silently drops INFO and
DEBUG), which made an earlier end-to-end run look like it had done nothing.
"""

from __future__ import annotations

import logging

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(level: str = "INFO") -> None:
    """Install a stdout handler on the root logger; safe to call repeatedly."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format=LOG_FORMAT)
    root.setLevel(level)
