"""Diagnose what the API hands us for a document (original vs archive, text layer).

Usage: python tests/integration/diag_original.py <doc_id>
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from paperless_rearchive.config import Settings
from paperless_rearchive.logging_setup import configure_logging
from paperless_rearchive.ocr.runner import guess_mime_type, has_text_layer
from paperless_rearchive.paperless_api import PaperlessAPI


def run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)  # noqa: S603
    except Exception as exc:  # noqa: BLE001
        return f"<{type(exc).__name__}: {exc}>"
    return out.stdout


def main() -> None:
    doc_id = int(sys.argv[1])
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    api = PaperlessAPI(settings.paperless_url, settings.api_token)
    doc = api.document(doc_id)
    print("original_file_name:", doc.get("original_file_name"))
    print("archived_file_name:", doc.get("archived_file_name"))
    print("mime_type (api):", doc.get("mime_type"))

    with tempfile.TemporaryDirectory(prefix="diag-") as tmp:
        path = api.download_original(doc_id, Path(tmp))
        print("downloaded as:", path.name, path.stat().st_size, "bytes")
        print("guess_mime_type:", guess_mime_type(path))
        print("has_text_layer:", has_text_layer(path))
        text = run(["pdftotext", str(path), "-"])
        print("pdftotext chars:", len(text.strip()))
        print("pdfinfo:")
        print(run(["pdfinfo", str(path)])[:600])
        print("pages/images:")
        print(run(["pdfimages", "-list", str(path)])[:600])


if __name__ == "__main__":
    main()
