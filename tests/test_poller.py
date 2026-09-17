"""Tests for the poller: wake handling, adaptive wait, failure escalation."""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from paperless_rearchive.config import Settings
from paperless_rearchive.paperless_api import PaperlessError
from paperless_rearchive.poller import (
    _ACTIVE_POLL_INTERVAL_S,
    _FAILURE_ATTEMPTS,
    _next_wait,
    _sleep_or_immediate,
    cycle,
)


def test_signal_during_cycle_forces_immediate_cycle() -> None:
    """A wake set *during* a cycle must not be swallowed by the post-cycle
    clear (the 2026-09-17 stall: HUP arrived mid-cycle, poller slept the full
    interval anyway)."""
    wake = threading.Event()
    wake.set()
    assert _sleep_or_immediate(300, wake) is True
    # consuming the signal must also clear it, so the following wait is a
    # normal one and we don't busy-loop immediate cycles
    assert not wake.is_set()


def test_quiet_cycle_waits_full_interval() -> None:
    wake = threading.Event()
    start = time.monotonic()
    assert _sleep_or_immediate(0.2, wake) is False
    assert time.monotonic() - start >= 0.15


def test_signal_during_wait_returns_promptly() -> None:
    """A signal arriving while waiting wakes the wait immediately."""
    wake = threading.Event()
    result: list[bool] = []

    def later() -> None:
        time.sleep(0.1)
        wake.set()

    t = threading.Thread(target=later)
    t.start()
    result.append(_sleep_or_immediate(30, wake))
    t.join()
    assert result == [True]


# ── adaptive polling ─────────────────────────────────────────────────────────


def _settings(**env: str) -> Settings:
    base = {
        "PAPERLESS_API_TOKEN": "t",
        "REARCHIVE_BACKUP_DIRECTORY": "/archive-backups",
    }
    base.update(env)
    with patch.dict("os.environ", base, clear=True):
        return Settings.from_env()


def _result(processed=0, succeeded=0, failed=0, remaining=0):
    from paperless_rearchive.poller import CycleResult

    return CycleResult(
        processed=processed, succeeded=succeeded, failed=failed, remaining=remaining
    )


def test_next_wait_idle_uses_full_interval() -> None:
    assert _next_wait(_settings(), _result(), 0) == 300


def test_next_wait_progress_or_backlog_uses_active_interval() -> None:
    s = _settings()
    assert (
        _next_wait(s, _result(processed=1, succeeded=1, remaining=9), 0)
        == _ACTIVE_POLL_INTERVAL_S
    )
    assert _next_wait(s, _result(remaining=4), 0) == _ACTIVE_POLL_INTERVAL_S


def test_next_wait_backs_off_on_no_progress() -> None:
    s = _settings()
    assert _next_wait(s, _result(processed=5, failed=5, remaining=5), 1) == pytest.approx(
        _ACTIVE_POLL_INTERVAL_S * 2
    )
    assert _next_wait(s, _result(processed=5, failed=5, remaining=5), 2) == pytest.approx(
        _ACTIVE_POLL_INTERVAL_S * 4
    )


def test_next_wait_backoff_capped_at_full_interval() -> None:
    s = _settings()
    assert _next_wait(s, _result(processed=5, failed=5, remaining=5), 20) == 300


# ── failure escalation ───────────────────────────────────────────────────────


class _FakeAPI:
    """Minimal PaperlessAPI stand-in: 3 docs on the content tag, 2 on 'all'."""

    def ensure_tag(self, name: str) -> int:
        return {"re-ocr-content": 11, "re-ocr-all": 22}[name]

    def tag_id(self, name: str) -> int | None:
        # No ``re-ocr-force`` modifier tag exists in this fixture.
        return None

    def doc_ids_with_tag(self, tag_id: int, limit: int) -> list[int]:
        if limit <= 0:
            return []
        return ([101, 102, 103] if tag_id == 11 else [101, 102])[:limit]

    def document(self, doc_id: int) -> dict:
        return {"tags": []}


@pytest.fixture(autouse=True)
def _clean_attempts():
    _FAILURE_ATTEMPTS.clear()
    yield
    _FAILURE_ATTEMPTS.clear()


def test_escalation_after_three_consecutive_failures() -> None:
    """process_document always failing: the 3rd attempt per (doc, tag) swaps
    the trigger tag for <trigger>-failure (via _finish) and clears the
    counter. A document tagged with both triggers escalates independently
    per trigger."""
    settings = _settings()
    finishes: list[dict] = []
    with (
        patch(
            "paperless_rearchive.poller.process_document",
            side_effect=PaperlessError("boom"),
        ),
        patch(
            "paperless_rearchive.poller._finish",
            side_effect=lambda *a, **kw: finishes.append({"ctx": a[1], **kw}),
        ),
    ):
        cycle(settings, _FakeAPI(), "chandra")  # attempt 1 everywhere
        cycle(settings, _FakeAPI(), "chandra")  # attempt 2 everywhere
        escalated_after_two = {call["ctx"].doc_id for call in finishes}
        cycle(settings, _FakeAPI(), "chandra")  # attempt 3 -> escalate all

    assert escalated_after_two == set()  # nothing escalates before 3 strikes
    escalated = {call["ctx"].doc_id for call in finishes}
    assert escalated == {101, 102, 103}
    assert all(call["success"] is False for call in finishes)
    assert all("Escalated after 3 consecutive failed" in call["note"] for call in finishes)
    # escalated documents no longer have a counter
    assert (101, "re-ocr-content") not in _FAILURE_ATTEMPTS
    assert (102, "re-ocr-all") not in _FAILURE_ATTEMPTS


def test_success_resets_failure_counter() -> None:
    """A document that fails once then succeeds has its counter cleared."""
    settings = _settings()

    attempts: dict[int, int] = {}

    def flaky(settings, api, provider, ctx):
        n = attempts.get(ctx.doc_id, 0) + 1
        attempts[ctx.doc_id] = n
        if ctx.doc_id == 102 and n == 1:
            raise PaperlessError("flaky")
        return None  # success

    with (
        patch("paperless_rearchive.poller.get_provider", return_value=object()),
        patch("paperless_rearchive.poller.process_document", side_effect=flaky),
        patch("paperless_rearchive.poller._finish"),
    ):
        cycle(settings, _FakeAPI(), "chandra")  # 102 fails once (content tag)
        assert _FAILURE_ATTEMPTS[(102, "re-ocr-content")] == 1
        cycle(settings, _FakeAPI(), "chandra")  # 102 succeeds -> counters gone
        assert (102, "re-ocr-content") not in _FAILURE_ATTEMPTS
        assert (102, "re-ocr-all") not in _FAILURE_ATTEMPTS
        assert (101, "re-ocr-content") not in _FAILURE_ATTEMPTS


def test_dry_run_escalation_changes_no_tags() -> None:
    settings = _settings(REARCHIVE_DRY_RUN="true")
    finishes: list[dict] = []
    with (
        patch(
            "paperless_rearchive.poller.process_document",
            side_effect=PaperlessError("boom"),
        ),
        patch(
            "paperless_rearchive.poller._finish",
            side_effect=lambda *a, **kw: finishes.append({"ctx": a[1], **kw}),
        ),
    ):
        cycle(settings, _FakeAPI(), "chandra")
        cycle(settings, _FakeAPI(), "chandra")
    assert finishes == []  # dry runs never swap tags, even on escalation
