"""Main loop: poll trigger tags and feed the pipeline."""

from __future__ import annotations

import logging
import signal
import sys
import threading

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
    processed = 0

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
            except PaperlessError:
                log.exception("API error on document %d; keeping trigger tag.", doc_id)
            except Exception:  # noqa: BLE001 - unexpected bug: keep trigger, keep going
                log.exception("Unexpected error on document %d; keeping trigger tag.", doc_id)
            processed += 1
            if processed >= settings.batch_limit:
                return


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
