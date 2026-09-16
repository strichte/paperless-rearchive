"""Main loop: poll trigger tags and feed the pipeline."""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time

from paperless_rearchive.config import Settings
from paperless_rearchive.logging_setup import configure_logging
from paperless_rearchive.ocr.base import get_provider
from paperless_rearchive.paperless_api import PaperlessAPI, PaperlessError
from paperless_rearchive.pipeline import DocumentContext, process_document

log = logging.getLogger("rearchive")

_wake = threading.Event()


def _on_signal(signum: int, _frame: object) -> None:
    _wake.set()
    log.info("signal %d received; forcing a poll cycle", signum)


def cycle(settings: Settings, api: PaperlessAPI, provider_name: str) -> None:
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
            except PaperlessError:
                log.exception("API error on document %d; keeping trigger tag.", doc_id)
                failed += 1
            except Exception:  # noqa: BLE001 - unexpected bug: keep trigger, keep going
                log.exception("Unexpected error on document %d; keeping trigger tag.", doc_id)
                failed += 1
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


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    if not settings.api_token:
        sys.exit("PAPERLESS_API_TOKEN (or PAPERLESS_API_TOKEN_FILE) is required")

    api = PaperlessAPI(settings.paperless_url, settings.api_token)
    log.info(
        "paperless-rearchive starting — paperless=%s provider=%s dry_run=%s "
        "poll=%ss batch=%d",
        settings.paperless_url,
        settings.provider_name,
        settings.dry_run,
        settings.poll_interval,
        settings.batch_limit,
    )

    signal.signal(signal.SIGHUP, _on_signal)
    signal.signal(signal.SIGUSR1, _on_signal)

    while True:
        try:
            cycle(settings, api, settings.provider_name)
        except Exception:  # noqa: BLE001 - network hiccup: retry next cycle
            log.exception("cycle failed; retrying in %.0fs", settings.poll_interval)
        if settings.run_once:
            return
        _wake.clear()
        _wake.wait(settings.poll_interval)


if __name__ == "__main__":
    main()
