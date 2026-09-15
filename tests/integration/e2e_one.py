"""One-off: run the full pipeline (including writes) for a single document.

Usage: python tests/integration/e2e_one.py <doc_id>            # re-ocr-all
       python tests/integration/e2e_one.py <doc_id> content    # re-ocr-content
"""

from __future__ import annotations

import os
import sys

from paperless_rearchive.config import Settings
from paperless_rearchive.logging_setup import configure_logging
from paperless_rearchive.ocr.base import get_provider
from paperless_rearchive.paperless_api import PaperlessAPI
from paperless_rearchive.pipeline import DocumentContext, process_document


def main() -> None:
    doc_id = int(sys.argv[1])
    archive_mode = len(sys.argv) < 3 or sys.argv[2] != "content"
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    api = PaperlessAPI(settings.paperless_url, os.environ["PAPERLESS_API_TOKEN"])
    doc = api.document(doc_id)
    trigger = settings.trigger_tag_all if archive_mode else settings.trigger_tag_content
    ctx = DocumentContext(
        doc_id=doc_id,
        trigger_tag_id=api.ensure_tag(trigger),
        trigger_tag_name=trigger,
        archive_mode=archive_mode,
        current_tags=list(doc.get("tags", [])),
    )
    process_document(settings, api, get_provider(settings.provider_name), ctx)


if __name__ == "__main__":
    main()
