"""Verify download_original() fetches the immutable original, not the archive.

Run:  PAPERLESS_API_TOKEN=... python tests/check_download.py <doc_id>
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from paperless_rearchive.ocr.runner import guess_mime_type, has_text_layer  # noqa: E402
from paperless_rearchive.paperless_api import PaperlessAPI  # noqa: E402


def main() -> None:
    doc_id = int(sys.argv[1]) if len(sys.argv) > 1 else 5820
    api = PaperlessAPI(
        os.environ.get("PAPERLESS_BASE_URL", "http://localhost:8001"),
        os.environ["PAPERLESS_API_TOKEN"],
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = api.download_original(doc_id, pathlib.Path(tmp))
        size = path.stat().st_size
        print(f"downloaded : {path.name} ({size} bytes)")
        print(f"mime       : {guess_mime_type(path)}")
        print(f"has_text   : {has_text_layer(path)}")
    print()
    print("For doc 5820: original = 786621 bytes, archive = 624103 bytes")
    print("VERDICT    :", "ORIGINAL (correct)" if size == 786621 else f"size {size} - check manually")


if __name__ == "__main__":
    main()
