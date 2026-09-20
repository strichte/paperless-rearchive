"""Model-name preflight: fail fast when the server does not serve the model.

Regression guard for the ``Error during VLLM generation: ... 404 ... no router
for requested model`` incident: a typo in PAPERLESS_CHANDRA_MODEL_NAME used to
cost the upstream retry ladder (six attempts, ~40 s) on *every* page before the
same opaque error surfaced.
"""

from __future__ import annotations

import logging

import pytest
from paperless_chandra.engine.client import ChandraClientError

from paperless_rearchive.ocr import model_check
from paperless_rearchive.ocr.model_check import ModelNotServedError


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    model_check._PROBE_CACHE.clear()
    model_check._PROBE_REPORTED.clear()
    yield
    model_check._PROBE_CACHE.clear()
    model_check._PROBE_REPORTED.clear()


def test_parse_model_ids_handles_openai_shape():
    assert model_check.parse_model_ids({"data": [{"id": "a"}, {"id": "b"}, {}]}) == ["a", "b"]


def test_parse_model_ids_tolerates_unexpected_payloads():
    assert model_check.parse_model_ids({"data": "nope"}) == []
    assert model_check.parse_model_ids(["not", "a", "dict"]) == []


def test_list_models_returns_none_for_empty_url():
    # Unusable configuration must skip validation, not explode here.
    assert model_check.list_models("   ") is None


def test_ensure_model_served_rejects_unadvertised_model(monkeypatch, caplog):
    monkeypatch.setattr(model_check, "list_models", lambda *a, **k: ["chandra-ocr-2-q8"])
    with caplog.at_level(logging.ERROR), pytest.raises(ModelNotServedError) as excinfo:
        model_check.ensure_model_served("http://ai:8110", "typo-model")
    message = str(excinfo.value)
    assert "'typo-model'" in message
    assert "chandra-ocr-2-q8" in message
    assert "not served" in message
    # A distinct type, so the poller can tell it from a transient failure...
    assert isinstance(excinfo.value, ChandraClientError)
    assert any(
        record.levelno >= logging.ERROR and "typo-model" in record.getMessage()
        for record in caplog.records
    )


def test_model_error_logged_once_but_raised_every_call(monkeypatch, caplog):
    """The verbose hint is logged once per (url, model); the exception still
    fires on every call so any caller aborts promptly."""
    monkeypatch.setattr(model_check, "list_models", lambda *a, **k: ["chandra"])
    for _ in range(3):
        with pytest.raises(ModelNotServedError):
            model_check.ensure_model_served("http://ai:8110", "typo-model")
    assert sum(1 for r in caplog.records if r.levelno >= logging.ERROR) == 1


def test_ensure_model_served_accepts_advertised_model(monkeypatch):
    monkeypatch.setattr(model_check, "list_models", lambda *a, **k: ["chandra"])
    model_check.ensure_model_served("http://ai:8110", "chandra")  # must not raise


@pytest.mark.parametrize("models", [None, []])
def test_ensure_model_served_skips_when_probe_unavailable(monkeypatch, models):
    # Keep the /models endpoint optional: no usable list means "cannot validate".
    monkeypatch.setattr(model_check, "list_models", lambda *a, **k: models)
    model_check.ensure_model_served("http://ai:8110", "anything")  # must not raise


def test_probe_runs_once_per_url_and_model(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_list_models(server_url, api_key="", timeout=model_check._PROBE_TIMEOUT):
        calls.append((server_url, api_key))
        return ["chandra"]

    monkeypatch.setattr(model_check, "list_models", fake_list_models)
    model_check.ensure_model_served("http://ai:8110", "chandra", "k")
    model_check.ensure_model_served("http://ai:8110", "chandra", "k")
    assert calls == [("http://ai:8110/v1", "k")]  # normalised, probed once


def test_probe_repeats_for_a_different_model(monkeypatch):
    calls: list[str] = []

    def fake_list_models(server_url, api_key="", timeout=model_check._PROBE_TIMEOUT):
        calls.append(server_url)
        return ["chandra", "other"]

    monkeypatch.setattr(model_check, "list_models", fake_list_models)
    model_check.ensure_model_served("http://ai:8110", "chandra")
    model_check.ensure_model_served("http://ai:8110", "other")
    assert len(calls) == 2


def test_cached_models_for_returns_probe_result(monkeypatch):
    monkeypatch.setattr(model_check, "list_models", lambda *a, **k: ["chandra", "other"])
    model_check.ensure_model_served("http://ai:8110", "chandra")
    assert model_check.cached_models_for("chandra") == ["chandra", "other"]
    assert model_check.cached_models_for("unknown") is None
