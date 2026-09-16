"""Tests for the poller's post-cycle wake handling (lost-signal regression)."""

from __future__ import annotations

import threading
import time

from paperless_rearchive.poller import _sleep_or_immediate


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
