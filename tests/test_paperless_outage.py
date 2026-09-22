"""Tests for paperless-connection failure handling (short, clear log lines)."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
import requests

from paperless_rearchive.paperless_api import PaperlessError
from paperless_rearchive.poller import (
    _FAILURE_ATTEMPTS,
    _is_connection_error,
    _short_error,
    cycle,
)


def _chain() -> requests.exceptions.ConnectionError:
    """Rebuild the urllib3 chain from the 23:46 outage log."""
    from urllib3.connection import HTTPConnection
    from urllib3.exceptions import MaxRetryError, NewConnectionError

    root = ConnectionRefusedError(111, "Connection refused")
    nce = NewConnectionError(
        HTTPConnection(host="paperless", port=8110),
        f"Failed to establish a new connection: {root}",
    )
    nce.__cause__ = root
    mre = MaxRetryError(
        object(), "/api/tags/?name__iexact=re-ocr-content", nce
    )
    mre.__cause__ = nce
    exc = requests.exceptions.ConnectionError(mre)
    exc.__cause__ = mre
    return exc


def test_short_error_names_root_cause() -> None:
    assert _short_error(_chain()) == "ConnectionRefusedError: [Errno 111] Connection refused"


def test_short_error_keeps_api_message() -> None:
    exc = PaperlessError("lookup tag 'x': HTTP 500: boom")
    assert _short_error(exc) == "PaperlessError: lookup tag 'x': HTTP 500: boom"


def test_is_connection_error_distinguishes() -> None:
    assert _is_connection_error(_chain()) is True
    assert _is_connection_error(PaperlessError("lookup tag: HTTP 500: boom")) is False
    assert _is_connection_error(ValueError("bug")) is False
    assert _is_connection_error(TimeoutError("timed out")) is True


def _settings(**env: str):
    from paperless_rearchive.config import Settings

    base = {"PAPERLESS_API_TOKEN": "t"}
    base.update(env)
    with patch.dict("os.environ", base, clear=True):
        return Settings.from_env()


class _DownAPI:
    def ensure_tag(self, name: str) -> int:
        raise _chain()

    def tag_id(self, name: str):  # pragma: no cover - never reached
        raise AssertionError

    def doc_ids_with_tag(self, tag_id: int, limit: int):  # pragma: no cover
        raise AssertionError


def test_cycle_start_outage_is_one_clear_line(caplog: pytest.LogCaptureFixture) -> None:
    _FAILURE_ATTEMPTS.clear()
    with caplog.at_level(logging.ERROR, logger="rearchive"):
        result = cycle(_settings(), _DownAPI(), "chandra")
    assert result.aborted is True
    assert result.processed == 0
    assert _FAILURE_ATTEMPTS == {}
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "Cannot reach paperless" in msg
    assert "ConnectionRefusedError" in msg
    assert "Traceback" not in caplog.text
    assert "MaxRetryError" not in msg  # root cause, not wrapper chain


class _MidCycleDownAPI:
    """Backlog snapshot succeeds, then the connection drops on document fetch."""

    def ensure_tag(self, name: str) -> int:
        return 1

    def tag_id(self, name: str):
        return None

    def doc_ids_with_tag(self, tag_id: int, limit: int):
        return [11]

    def document(self, doc_id: int):
        raise _chain()


def test_mid_cycle_outage_aborts_without_strike(caplog: pytest.LogCaptureFixture) -> None:
    _FAILURE_ATTEMPTS.clear()
    with (
        patch("paperless_rearchive.poller.get_provider", return_value=object()),
        patch("paperless_rearchive.poller.server_check.outage", return_value=False),
        caplog.at_level(logging.ERROR, logger="rearchive"),
    ):
        result = cycle(_settings(), _MidCycleDownAPI(), "chandra")
    assert result.aborted is True
    assert _FAILURE_ATTEMPTS == {}
    assert any("Lost connection to paperless" in r.getMessage() for r in caplog.records)
    assert "Traceback" not in caplog.text
