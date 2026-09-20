"""Fail-fast validation of the configured Chandra model name.

The paperless-chandra provider applies ``PAPERLESS_CHANDRA_MODEL_NAME`` to
every request without checking that the inference server actually serves it.
A typo therefore 404s on *every* page, and the upstream retry ladder re-sends
the same failing request ``MAX_VLLM_RETRIES`` times per page (six by default,
with growing backoff - roughly 40 s per page) before the error finally
surfaces as an opaque ``Error during VLLM generation: ...``.

This module probes the server's ``GET /models`` endpoint once per worker
process and fails fast with the requested name *and* the list of models the
server does serve, so a misconfiguration costs one HTTP round-trip instead of
the whole retry ladder per page.

Servers that do not expose a usable ``/models`` list are skipped, not failed:
the probe is a guard rail, not a requirement.

A mismatch raises :class:`ModelNotServedError` - a distinct
:class:`~paperless_chandra.engine.client.ChandraClientError` subtype - so the
poller can tell a deployment-wide misconfiguration (retrying is pointless,
tags must not be touched or escalated) from a transient inference failure.
"""

from __future__ import annotations

import json
import logging
import threading

from paperless_chandra.engine.client import ChandraClientError, normalize_server_url

log = logging.getLogger(__name__)


class ModelNotServedError(ChandraClientError):
    """The configured model is not in the server's advertised model list.

    Deployment-wide and permanent for this process: every document would fail
    identically, so retrying (per page or per document) cannot succeed until
    ``PAPERLESS_CHANDRA_MODEL_NAME`` is changed and the poller restarted.
    """


#: ``GET /models`` probe timeout (seconds): this is a preflight, so it must
#: never stall ingestion on a slow inference server.
_PROBE_TIMEOUT = 5.0

#: Probe results per ``(base_url, model)``: the advertised ids when the server
#: answered with a usable list, ``None`` when it could not be consulted. The
#: presence of the key is the once-per-process guard; the value feeds the
#: "server advertises" hint in the error message.
_PROBE_CACHE: dict[tuple[str, str], list[str] | None] = {}
#: Keys whose mismatch has already been logged, so a misconfigured poller
#: repeats the (verbose) hint once per process instead of once per cycle.
_PROBE_REPORTED: set[tuple[str, str]] = set()
_PROBE_LOCK = threading.Lock()


def parse_model_ids(payload: object) -> list[str]:
    """Model ids from an OpenAI-compatible ``/models`` response body."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    return [str(entry["id"]) for entry in data if isinstance(entry, dict) and entry.get("id")]


def list_models(
    server_url: str, api_key: str = "", timeout: float = _PROBE_TIMEOUT
) -> list[str] | None:
    """Best-effort ``GET {base}/models``; ``None`` when it cannot be used.

    Returns the advertised model ids, or ``None`` when the endpoint is missing,
    unreachable, rejects the key, or answers without a usable list. Callers
    treat ``None`` (and an empty list) as "cannot validate" and skip the check
    rather than failing a run over a non-essential probe.
    """
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    try:
        base = normalize_server_url(server_url)
    except ChandraClientError:
        return None
    request = Request(f"{base}/models")  # noqa: S310 - operator-configured URL
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except (HTTPError, URLError, OSError, ValueError) as exc:
        log.debug("Could not list models from %s/models: %s", base, exc)
        return None
    return parse_model_ids(payload)


def model_not_found_message(server_url: str, model_name: str, available: list[str]) -> str:
    """Actionable message naming the requested model and what the server serves."""
    listing = ", ".join(sorted(available)) if available else "(none)"
    return (
        f"Chandra model {model_name!r} is not served by {server_url} "
        f"(server advertises: {listing}). Set PAPERLESS_CHANDRA_MODEL_NAME to "
        "one of those names."
    )


def ensure_model_served(
    server_url: str, model_name: str, api_key: str = "", timeout: float = _PROBE_TIMEOUT
) -> None:
    """Fail fast when the server does not advertise *model_name*.

    The probe runs once per ``(url, model)`` per worker process; its result is
    cached, so later documents (and concurrent pages) do not re-query the
    server. The actionable message is logged once per process - callers
    report the abort themselves without repeating it every cycle.

    Raises:
        ModelNotServedError: the server answered ``/models`` with a usable list
            that does not contain *model_name*.
    """
    try:
        base = normalize_server_url(server_url)
    except ChandraClientError:
        return  # empty server URL: the provider's own validation reports that
    key = (base, model_name)
    with _PROBE_LOCK:
        if key not in _PROBE_CACHE:
            _PROBE_CACHE[key] = list_models(base, api_key, timeout)
        available = _PROBE_CACHE[key]
        if not available or model_name in available:
            return
        first_report = key not in _PROBE_REPORTED
        _PROBE_REPORTED.add(key)
    message = model_not_found_message(base, model_name, available)
    if first_report:
        log.error("%s", message)
    raise ModelNotServedError(message)


def cached_models_for(model_name: str) -> list[str] | None:
    """Advertised models for *model_name* from the last probe, if any.

    Lets the retry/error log interceptor add the server's model list to an
    upstream generation error when a probe already knows it.
    """
    with _PROBE_LOCK:
        for (_base, model), available in _PROBE_CACHE.items():
            if model == model_name and available:
                return available
    return None
