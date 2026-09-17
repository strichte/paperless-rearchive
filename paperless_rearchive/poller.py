"""Main loop: poll trigger tags and feed the pipeline.

Polling is adaptive while a backlog drains: as long as a cycle made progress
or documents remain tagged, the next cycle starts ~10 s later (fixed); once
the queue is empty, the poller settles back to the full idle
``REARCHIVE_POLL_INTERVAL``. Cycles that attempt documents but never succeed
(e.g. an OCR server outage - those documents keep their trigger tag) back off
exponentially from the active interval up to the full interval, so a broken
server never turns into a tight retry loop.

Documents that fail ``_MAX_CONSECUTIVE_FAILURES`` times in a row are
escalated: the trigger tag is swapped for ``<trigger>-failure`` plus an audit
note, so permanently broken documents do not retry forever.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass

from paperless_rearchive import __version__
from paperless_rearchive.config import Settings
from paperless_rearchive.logging_setup import configure_logging
from paperless_rearchive.ocr.base import get_provider
from paperless_rearchive.paperless_api import PaperlessAPI, PaperlessError
from paperless_rearchive.pipeline import (
    DocumentContext,
    _finish,
    process_document,
)

log = logging.getLogger("rearchive")

_wake = threading.Event()

#: Consecutive failed attempts (per document + trigger tag) before the
#: document is escalated to ``<trigger>-failure`` instead of being retried
#: forever. Deliberately not configurable - re-OCR is idempotent and cheap
#: to retry, 3 strikes are enough to tell "broken server" from "broken doc".
_MAX_CONSECUTIVE_FAILURES = 3

#: Seconds between cycles while a backlog is being drained. Fixed (not
#: configurable): it only needs to be small relative to a document's OCR
#: time so draining a large queue wastes ~no time between cycles.
_ACTIVE_POLL_INTERVAL_S = 10.0

#: (doc_id, trigger_tag_name) -> consecutive failed attempts. Process-local:
#: a restart resets the counters, which is fine (re-OCR is idempotent).
_FAILURE_ATTEMPTS: dict[tuple[int, str], int] = {}


@dataclass
class CycleResult:
    """Outcome of one poll cycle, used to schedule the next one."""

    processed: int
    succeeded: int
    failed: int
    #: Documents still tagged when the cycle ended (backlog left to drain).
    remaining: int


def _on_signal(signum: int, _frame: object) -> None:
    _wake.set()
    log.info("signal %d received; forcing a poll cycle", signum)


def cycle(settings: Settings, api: PaperlessAPI, provider_name: str) -> CycleResult:
    triggers = {
        settings.trigger_tag_content: False,  # archive_mode
        settings.trigger_tag_all: True,
    }
    tag_ids = {name: api.ensure_tag(name) for name in triggers}
    # Backlog snapshot before this cycle (for progress + tuning advice).
    backlog_start = {name: len(api.doc_ids_with_tag(tid, limit=100_000)) for name, tid in tag_ids.items()}
    total_backlog = sum(backlog_start.values())
    processed = 0
    succeeded = 0
    failed = 0
    cycle_started = time.monotonic()

    for name, archive_mode in triggers.items():
        tag_id = tag_ids[name]
        doc_ids = api.doc_ids_with_tag(tag_id, limit=settings.batch_limit - processed)
        if not doc_ids:
            continue
        log.info("processing %d document(s) tagged %r: %s", len(doc_ids), name, doc_ids)
        for doc_id in doc_ids:
            doc = api.document(doc_id)
            ctx = DocumentContext(
                doc_id=doc_id,
                trigger_tag_id=tag_id,
                trigger_tag_name=name,
                archive_mode=archive_mode,
                current_tags=list(doc.get("tags", [])),
            )
            try:
                process_document(settings, api, get_provider(provider_name), ctx)
                succeeded += 1
                _FAILURE_ATTEMPTS.pop((doc_id, name), None)
            except PaperlessError as exc:
                log.exception("API error on document %d; keeping trigger tag.", doc_id)
                failed += 1
                _record_failure(settings, api, ctx, exc)
            except Exception as exc:  # noqa: BLE001 - unexpected bug: keep trigger, keep going
                log.exception("Unexpected error on document %d; keeping trigger tag.", doc_id)
                failed += 1
                _record_failure(settings, api, ctx, exc)
            processed += 1
            if processed >= settings.batch_limit:
                break
        if processed >= settings.batch_limit:
            break

    elapsed = time.monotonic() - cycle_started
    if processed:
        remaining_start = {name: len(api.doc_ids_with_tag(tid, limit=100_000)) for name, tid in tag_ids.items()}
        remaining = sum(remaining_start.values())
        _log_cycle_summary(
            settings, processed, succeeded, failed, elapsed, total_backlog, remaining
        )
    else:
        # Nothing attempted: either the queue is empty or everything in it
        # exceeded the batch limit mid-cycle (impossible today) - either way
        # the tagged documents are what remains.
        remaining = total_backlog
    return CycleResult(
        processed=processed, succeeded=succeeded, failed=failed, remaining=remaining
    )


def _record_failure(
    settings: Settings, api: PaperlessAPI, ctx: DocumentContext, exc: Exception
) -> None:
    """Count a failed attempt for (doc, trigger); escalate after N in a row.

    Escalation swaps the trigger tag for ``<trigger>-failure`` plus an audit
    note (via :func:`paperless_rearchive.pipeline._finish`), so permanently
    broken documents stop being retried every cycle. Counters are
    process-local and reset on success or restart.
    """
    key = (ctx.doc_id, ctx.trigger_tag_name)
    attempts = _FAILURE_ATTEMPTS.get(key, 0) + 1
    _FAILURE_ATTEMPTS[key] = attempts
    if attempts < _MAX_CONSECUTIVE_FAILURES:
        return
    log.error(
        "Document %d failed %d consecutive times; escalating to %r.",
        ctx.doc_id,
        attempts,
        f"{ctx.trigger_tag_name}{settings.failure_suffix}",
    )
    if settings.dry_run:
        log.warning(
            "DRY-RUN: escalation for document %d skipped (dry runs change no tags).",
            ctx.doc_id,
        )
        _FAILURE_ATTEMPTS.pop(key, None)
        return
    try:
        _finish(
            api,
            ctx,
            success=False,
            note=(
                f"Escalated after {attempts} consecutive failed re-OCR attempts. "
                f"Last error: {type(exc).__name__}: {exc}"
            ),
            settings=settings,
        )
    except Exception:  # noqa: BLE001 - API down: keep trigger, count restarts
        log.exception("Escalation failed for document %d; keeping trigger tag.", ctx.doc_id)
        return
    _FAILURE_ATTEMPTS.pop(key, None)


def _log_cycle_summary(
    settings: Settings,
    processed: int,
    succeeded: int,
    failed: int,
    elapsed: float,
    total_backlog: int,
    remaining: int,
) -> None:
    """Log throughput + backlog progress and recommend batch/poll tuning.

    Goal: never sit idle in poll sleep while 100s of documents wait. The
    recommendation keeps cycle *work* time dominant over *sleep* time: with
    the measured docs/min, pick a batch that fills ~10 minutes of work and a
    poll interval that is idle at most ~10% of the work time.
    """
    docs_per_min = (processed / elapsed * 60) if elapsed > 0 else 0.0
    done = total_backlog - remaining
    log.info(
        "cycle done: %d processed (%d ok, %d error) in %.1fs = %.2f docs/min; "
        "backlog %d -> %d remaining (%d cleared this cycle)",
        processed,
        succeeded,
        failed,
        elapsed,
        docs_per_min,
        total_backlog,
        remaining,
        done,
    )
    if remaining <= 0 or docs_per_min <= 0:
        return
    eta_min = remaining / docs_per_min
    # Batch that covers ~10 min of measured work, clamped to [batch, 200].
    target_batch = max(settings.batch_limit, min(200, int(round(docs_per_min * 10))))
    work_s = (target_batch / docs_per_min * 60) if docs_per_min > 0 else 0.0
    # Poll sleep ~10% of that work time, clamped to [15s, 15min].
    target_poll = max(15, min(900, int(round(work_s * 0.1))))
    idle_share = (
        settings.poll_interval / (settings.poll_interval + work_s) if work_s > 0 else 0.0
    )
    log.info(
        "backlog: %d document(s) still tagged, ETA ~%.0f min at current pace "
        "(%.2f docs/min). Recommendation: REARCHIVE_BATCH_LIMIT=%d "
        "(now %d), REARCHIVE_POLL_INTERVAL=%d (now %.0f) so cycles do ~10 min "
        "of work with ~10%% idle.",
        remaining,
        eta_min,
        docs_per_min,
        target_batch,
        settings.batch_limit,
        target_poll,
        settings.poll_interval,
    )
    if settings.batch_limit < remaining and idle_share > 0.25:
        log.warning(
            "poll interval dominates cycle time (%.0fs sleep vs ~%.0fs work): "
            "the sidecar idles while %d document(s) wait - raise "
            "REARCHIVE_BATCH_LIMIT toward %d and/or lower "
            "REARCHIVE_POLL_INTERVAL toward %d.",
            settings.poll_interval,
            work_s,
            remaining,
            target_batch,
            target_poll,
        )


def _next_wait(settings: Settings, result: CycleResult, no_progress_streak: int) -> float:
    """Wait before the next cycle, from the just-finished cycle's outcome.

    * idle (nothing attempted, nothing tagged) -> full ``REARCHIVE_POLL_INTERVAL``
    * progress or known backlog -> the fixed active interval (drain mode)
    * attempts but no successes -> exponential backoff from the active
      interval, capped at the full interval (never a tight retry loop)
    """
    if result.processed == 0 and result.remaining == 0:
        return settings.poll_interval
    if no_progress_streak <= 0:
        return _ACTIVE_POLL_INTERVAL_S
    return min(settings.poll_interval, _ACTIVE_POLL_INTERVAL_S * (2**no_progress_streak))


def _sleep_or_immediate(poll_interval: float, wake: threading.Event) -> bool:
    """Post-cycle wait; returns True when the next cycle must start now.

    A SIGHUP/SIGUSR1 arriving *while a cycle is running* sets ``wake``, but the
    old code unconditionally cleared it right after the cycle and slept the
    full interval - silently losing the signal (observed 2026-09-17: a HUP'd
    poller sat idle through its whole interval). Clear-and-return-True here
    consumes the signal and forces an immediate cycle instead. A signal that
    arrives after this check but during ``wait()`` still wakes ``wait()``
    immediately, as before.
    """
    if wake.is_set():
        wake.clear()
        return True
    # Event.wait returns True when the event was set (signal arrived during
    # the wait -> immediate cycle), False on timeout (normal sleep).
    return wake.wait(poll_interval)


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    try:
        settings.validate()
        settings.prepare_backup_dir()
    except ValueError as e:
        sys.exit(f"configuration error: {e}")
    if not settings.api_token:
        sys.exit("PAPERLESS_API_TOKEN (or PAPERLESS_API_TOKEN_FILE) is required")

    api = PaperlessAPI(settings.paperless_url, settings.api_token)
    log.info(
        "paperless-rearchive %s starting — paperless=%s provider=%s dry_run=%s "
        "poll=%ss idle (~%ss while draining) batch=%d",
        __version__,
        settings.paperless_url,
        settings.provider_name,
        settings.dry_run,
        settings.poll_interval,
        _ACTIVE_POLL_INTERVAL_S,
        settings.batch_limit,
    )

    signal.signal(signal.SIGHUP, _on_signal)
    signal.signal(signal.SIGUSR1, _on_signal)

    no_progress_streak = 0
    previous_wait: float | None = None
    while True:
        result: CycleResult | None = None
        try:
            result = cycle(settings, api, settings.provider_name)
            if result.succeeded > 0 or (result.processed == 0 and result.remaining == 0):
                no_progress_streak = 0
            elif result.processed > 0:
                # Attempts, but nothing succeeded: those documents keep their
                # trigger tag and will be retried - back off.
                no_progress_streak += 1
            wait = _next_wait(settings, result, no_progress_streak)
        except Exception:  # noqa: BLE001 - network hiccup: back off, retry next cycle
            log.exception("cycle failed; retrying after backoff")
            no_progress_streak += 1
            wait = min(
                settings.poll_interval,
                _ACTIVE_POLL_INTERVAL_S * (2**no_progress_streak),
            )
        if settings.run_once:
            return
        if wait != previous_wait:
            state = "draining" if wait < settings.poll_interval else "idle"
            log.info(
                "next cycle in %.0fs (%s; streak=%d)",
                wait,
                state,
                no_progress_streak,
            )
            previous_wait = wait
        if _sleep_or_immediate(wait, _wake):
            continue


if __name__ == "__main__":
    main()
