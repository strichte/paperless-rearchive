"""Shared logging configuration for the poller and the ad-hoc runners.

Without this the container only surfaces ocrmypdf's own warnings (Python's
``logging.lastResort`` prints WARNING+ to stderr and silently drops INFO and
DEBUG), which made an earlier end-to-end run look like it had done nothing.
"""

from __future__ import annotations

import logging

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

#: Third-party loggers that are chatty at INFO (font subsetting details,
#: per-request httpx lines, ocrmypdf progress chatter). Their INFO records
#: carry no signal for operators, so they are raised to WARNING unless the
#: operator explicitly runs at DEBUG. Warnings and errors always surface.
#: Parent names cover children (e.g. ``fontTools`` covers
#: ``fontTools.subset``) via logger inheritance.
_NOISY_LOGGERS = (
    "fontTools",
    "httpx",
    "httpcore",
    "ocrmypdf",
    "pikepdf",
    "img2pdf",
    "PIL",
)


def configure_logging(level: str = "INFO") -> None:
    """Install a stdout handler on the root logger; safe to call repeatedly."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format=LOG_FORMAT)
    root.setLevel(level)
    if level.upper() == "DEBUG":
        # Operator asked for everything: release the noisy loggers back to
        # inheritance so their DEBUG records flow again.
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)
    else:
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
