"""One-off: compare the existing archive of a document against a freshly
re-OCR'd archive produced by the real pipeline (runner.run_ocr), PDF/A and all.
Usage: python tests/size_compare.py <doc_id>
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from paperless_rearchive.config import Settings
from paperless_rearchive.ocr.base import get_provider
from paperless_rearchive.ocr.runner import run_ocr
from paperless_rearchive.paperless_api import PaperlessAPI


def human(n: int) -> str:
    return f"{n / 1024:.1f} KiB"


def main() -> None:
    doc_id = int(sys.argv[1])
    settings = Settings.from_env()
    api = PaperlessAPI(settings.paperless_url, os.environ["PAPERLESS_API_TOKEN"])
    doc = api.document(doc_id)

    out_dir = Path(tempfile.mkdtemp(prefix=f"sizecmp-{doc_id}-"))
    old_archive = None
    try:  # the on-disk archive path lives only in the DB
        from paperless_rearchive.archive.db import fetch_archive_filename

        rel = fetch_archive_filename(settings.db, doc_id)
        if rel:
            old_archive = settings.archive_dir / rel
    except Exception as e:  # noqa: BLE001
        print(f"(DB lookup failed: {e})")

    with tempfile.TemporaryDirectory() as tmp:
        original = api.download_original(doc_id, Path(tmp))
        new_archive = out_dir / "new-archive.pdf"
        run_ocr(original, new_archive, out_dir / "sidecar.txt", get_provider(settings.provider_name), settings)
        print(f"document           : {doc_id} ({doc.get('original_file_name')})")
        print(f"original (source)  : {human(original.stat().st_size)}")
        if old_archive is not None and old_archive.is_file():
            print(f"current archive    : {old_archive} -> {human(old_archive.stat().st_size)}")
        elif old_archive is not None:
            print(f"current archive    : MISSING on disk ({old_archive})")
        else:
            print("current archive    : none (born-digital or generation disabled)")
        print(f"new archive (PDF/A): {new_archive} -> {human(new_archive.stat().st_size)}")
        if old_archive is not None and old_archive.is_file():
            ratio = new_archive.stat().st_size / old_archive.stat().st_size
            print(f"new/current ratio  : {ratio:.2f}x")


if __name__ == "__main__":
    main()
