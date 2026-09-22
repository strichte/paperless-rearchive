"""Tests for shared logging configuration (noisy third-party loggers)."""

from __future__ import annotations

import logging

from paperless_rearchive.logging_setup import _NOISY_LOGGERS, configure_logging


def _reset_noisy() -> None:
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)


def test_noisy_loggers_silenced_at_info() -> None:
    _reset_noisy()
    try:
        configure_logging("INFO")
        for name in _NOISY_LOGGERS:
            assert logging.getLogger(name).level == logging.WARNING
        # Our own loggers still inherit the INFO root level.
        assert logging.getLogger("rearchive").getEffectiveLevel() == logging.INFO
        # Warnings/errors from noisy loggers still pass their own level.
        assert logging.getLogger("fontTools.subset").getEffectiveLevel() == logging.WARNING
    finally:
        _reset_noisy()


def test_noisy_loggers_released_at_debug() -> None:
    _reset_noisy()
    try:
        configure_logging("INFO")
        configure_logging("DEBUG")
        for name in _NOISY_LOGGERS:
            assert logging.getLogger(name).level == logging.NOTSET
    finally:
        _reset_noisy()
