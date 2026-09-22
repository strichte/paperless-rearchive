"""Reachability preflight for the Chandra inference server.

Regression guard for the 2026-09-22 outage: with the server down, the poller
treated every document as an ordinary failure - one wasted attempt and one
escalation strike per document per cycle, mis-tagging healthy documents
``<trigger>-failure`` after three no-progress cycles.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request

import pytest
from paperless_chandra.engine.client import ChandraClientError

from paperless_rearchive.ocr.server_check import (
    ServerUnreachableError,
    ensure_server_reachable,
    outage,
)


@pytest.fixture(autouse=True)
def _no_real_http(monkeypatch):
    """Never touch the network from these tests."""
    monkeypatch.setattr(urllib.request, "urlopen", _fail_connect)


def _fail_connect(request, timeout=None):  # noqa: ANN001, ARG001
    raise urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))


def test_outage_true_when_connection_refused(monkeypatch):
    def refused(request, timeout=None):  # noqa: ANN001, ARG001
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    assert outage("http://ai:8000/v1") is True


def test_outage_true_on_timeout(monkeypatch):
    def slow(request, timeout=None):  # noqa: ANN001, ARG001
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", slow)
    assert outage("http://ai:8000/v1") is True


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_http_answer_is_not_an_outage(monkeypatch, status):
    """Any HTTP status proves the server is up; auth/endpoint problems are
    configuration errors, not outages."""

    def http_error(request, timeout=None):  # noqa: ANN001, ARG001
        raise urllib.error.HTTPError("http://ai:8000/v1/models", status, "nope", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", http_error)
    assert outage("http://ai:8000/v1") is False


def test_outage_false_for_unusable_url():
    """An empty/unusable URL is a configuration error the provider's own
    validate() reports - not an outage (and never a network call)."""
    assert outage("") is False


def test_server_url_normalised_like_the_model_probe(monkeypatch):
    seen: list[str] = []

    def ok(request, timeout=None):  # noqa: ANN001, ARG001
        seen.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 404, "no", None, None)

    # 404 counts as "answered"; the request URL shows the normalisation.
    monkeypatch.setattr(urllib.request, "urlopen", ok)
    assert outage("http://ai:8000") is False
    assert seen == ["http://ai:8000/v1/models"]


def test_ensure_server_reachable_raises_chandra_client_error(monkeypatch):
    def refused(request, timeout=None):  # noqa: ANN001, ARG001
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    with pytest.raises(ServerUnreachableError) as excinfo:
        ensure_server_reachable("http://ai:8000/v1")
    # A ChandraClientError subclass, so the poller's deployment-wide gate
    # (shared with ModelNotServedError) catches it.
    assert isinstance(excinfo.value, ChandraClientError)
    assert "http://ai:8000/v1" in str(excinfo.value)
    assert "PAPERLESS_CHANDRA_SERVER_URL" in str(excinfo.value)


def test_ensure_server_reachable_quiet_when_up(monkeypatch):
    from contextlib import nullcontext

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: nullcontext())
    ensure_server_reachable("http://ai:8000/v1")  # must not raise


def test_ensure_server_reachable_skips_unusable_url():
    ensure_server_reachable("")  # must not raise, no network call
