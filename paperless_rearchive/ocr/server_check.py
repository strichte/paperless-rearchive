"""Fail-fast reachability check for the Chandra inference server.

A poller cycle that starts while the inference server is down cannot succeed:
every tagged document would fail with the same ``ConnectionRefusedError``.
Treating that as a per-document failure burns escalation strikes on healthy
documents - three no-progress cycles in a row would mis-tag them
``<trigger>-failure`` (observed 2026-09-22: an outage escalated a perfectly
good document). This module answers one question - "can we reach the server
right now?" - so the poller can abort the whole cycle the same way it aborts
on :class:`~paperless_rearchive.ocr.model_check.ModelNotServedError`.

The probe is a plain ``GET /models``: *any* HTTP answer (including 401/403/404)
proves the server is up - auth and endpoint problems are configuration errors
that the per-document path handles correctly. Only transport-level failures
(connection refused, DNS, timeout) count as unreachable.
"""

from __future__ import annotations

import logging

from paperless_chandra.engine.client import ChandraClientError, normalize_server_url

log = logging.getLogger(__name__)


class ServerUnreachableError(ChandraClientError):
    """The Chandra server cannot be reached (transport-level failure).

    Deployment-wide but transient: no document can be OCR'd right now, and
    retrying later is exactly what should happen once the server is back -
    so the poller aborts the cycle without recording per-document failures.
    """


#: Reachability probe timeout (seconds). A connection refusal answers
#: instantly; this only bounds firewalled / packet-dropping hosts.
_PROBE_TIMEOUT = 5.0


def _models_probe(base: str, api_key: str, timeout: float) -> None:
    """One ``GET {base}/models``; raises ConnectionError on transport failure."""
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    request = Request(f"{base}/models")  # noqa: S310 - operator-configured URL
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urlopen(request, timeout=timeout):  # noqa: S310
            pass
    except HTTPError:
        # The server answered (bad endpoint/key are not outages).
        return
    except (URLError, OSError) as exc:
        raise ConnectionError(f"no answer from {base}/models: {exc}") from exc


def outage(server_url: str, api_key: str = "", timeout: float = _PROBE_TIMEOUT) -> bool:
    """True when *server_url* was probed and could not be reached.

    An empty or otherwise unusable server URL is *not* an outage: it is a
    configuration error the provider's own ``validate()`` reports (and it
    lets bare test doubles pass the poller's gate).
    """
    try:
        base = normalize_server_url(server_url)
    except ChandraClientError:
        return False
    try:
        _models_probe(base, api_key, timeout)
    except ConnectionError:
        return True
    return False


def ensure_server_reachable(
    server_url: str, api_key: str = "", timeout: float = _PROBE_TIMEOUT
) -> None:
    """Raise :class:`ServerUnreachableError` when the server cannot be reached.

    Used as a per-document preflight (next to
    :func:`~paperless_rearchive.ocr.model_check.ensure_model_served`), so an
    outage surfaces as a :class:`~paperless_chandra.engine.client.ChandraClientError`
    subclass instead of ocrmypdf's opaque ``MissingDependencyError``.
    """
    try:
        base = normalize_server_url(server_url)
    except ChandraClientError:
        return
    try:
        _models_probe(base, api_key, timeout)
    except ConnectionError as exc:
        raise ServerUnreachableError(
            f"The Chandra server at {base} is not reachable ({exc}). Check "
            "PAPERLESS_CHANDRA_SERVER_URL and that the inference server "
            "container is running."
        ) from exc
