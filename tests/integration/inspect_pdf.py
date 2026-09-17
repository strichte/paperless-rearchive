#!/usr/bin/env python3
"""Inspect PDF documents using pdf-inspector (v1.20.0, firecrawl).

Classifies PDFs as text-based vs scanned, with optional per-page
breakdown and original-vs-archive comparison. Useful for testing the
digital-born detection logic against documents with known provenance.

USAGE (always via the project venv - system python3 lacks pdf_inspector):
    .venv/bin/python tests/integration/pdf_inspector.py 3452
    .venv/bin/python tests/integration/pdf_inspector.py --per-page 3452
    .venv/bin/python tests/integration/pdf_inspector.py --compare 3452
    .venv/bin/python tests/integration/pdf_inspector.py /path/to/file.pdf

ENVIRONMENT:
    Token is read from ~/paperless-lxc/.env.paperless-gpt
    (PAPERLESS_API_TOKEN=...). The PAPERLESS_API_TOKEN env var,
    if set, takes precedence. Base URL is hardcoded to
    http://localhost:8001.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

PAPERLESS_BASE_URL = "http://localhost:8001"
TOKEN_FILE = Path.home() / "paperless-lxc" / ".env.paperless-gpt"


def _load_token() -> str:
    """Return API token: env var wins, else read KEY=VALUE file."""
    token = os.environ.get("PAPERLESS_API_TOKEN", "").strip()
    if token:
        return token
    try:
        for line in TOKEN_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("PAPERLESS_API_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""

_SCRIPT_PATH = Path(__file__).resolve()
_PROJECT_ROOT = _SCRIPT_PATH.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from pdf_inspector import classify_pdf, detect_pdf, extract_pages_markdown
    PDF_INSPECTOR_AVAILABLE = True
except ImportError:
    PDF_INSPECTOR_AVAILABLE = False

from paperless_rearchive.ocr.provenance import classify_pdf as classify_provenance  # noqa: E402
from paperless_rearchive.paperless_api import (  # noqa: E402
    PaperlessAPI,
    filename_from_disposition,
)


def _label(pdf_type: str) -> str:
    return {
        "text_based": "TEXT-BASED (digital-born)",
        "scanned": "SCANNED (needs OCR)",
        "image_based": "IMAGE-BASED (no text)",
        "mixed": "MIXED (some pages text, some scanned)",
    }.get(pdf_type, pdf_type)


def _download(api: PaperlessAPI, doc_id: int, *, original: bool, dest_dir: Path) -> Path:
    """Download original (?original=true) or archive (no param) via API."""
    params = {"original": "true"} if original else {}
    resp = api.session.get(
        api._url(f"/api/documents/{doc_id}/download/"),
        params=params,
        timeout=api.timeout,
        stream=True,
    )
    api._check(resp, f"download {'original' if original else 'archive'} of {doc_id}")
    name = filename_from_disposition(resp.headers.get("Content-Disposition", ""))
    prefix = "orig_" if original else "arch_"
    dest = dest_dir / f"{prefix}{name or f'doc{doc_id}.pdf'}"
    with dest.open("wb") as fh:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            fh.write(chunk)
    return dest
def inspect_path(path: Path, label: str, *, per_page: bool, decide: bool = False) -> dict:
    size = path.stat().st_size
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    print(f"Path: {path}\nSize: {size:,} bytes")
    result: dict = {"label": label, "path": str(path), "size_bytes": size}
    print("\n--- detect_pdf (fast; needs_ocr is 1-indexed) ---")
    det = detect_pdf(str(path))
    print(f"PDF Type: {_label(det.pdf_type)}")
    print(f"Confidence: {det.confidence:.2f}")
    print(f"Page Count: {det.page_count}")
    print(f"Pages Needing OCR: {det.pages_needing_ocr or 'none'}")
    if getattr(det, "has_encoding_issues", False):
        print("Encoding issues: YES (fall back to OCR)")
    result.update(pdf_type=det.pdf_type, confidence=det.confidence,
                  page_count=det.page_count,
                  pages_needing_ocr=list(det.pages_needing_ocr))
    print("\n--- classify_pdf (lightweight; needs_ocr is 0-indexed!) ---")
    cls = classify_pdf(str(path))
    cls_1idx = [p + 1 for p in cls.pages_needing_ocr]
    print(f"PDF Type: {_label(cls.pdf_type)}")
    print(f"Confidence: {cls.confidence:.2f}")
    print(f"Pages Needing OCR (0-idx): {cls.pages_needing_ocr or 'none'}"
          f" -> 1-indexed: {cls_1idx or 'none'}")
    if per_page:
        print("\n--- extract_pages_markdown per-page (PageMarkdown.page is 0-indexed) ---")
        per = extract_pages_markdown(str(path))
        print(f"Tables: {per.pages_with_tables or 'none'}")
        print(f"Columns: {per.pages_with_columns or 'none'}")
        for pg in per.pages:
            n1 = pg.page + 1
            st = "NEEDS OCR" if pg.needs_ocr else "TEXT ok"
            extra = f" reason={pg.ocr_reason}" if pg.needs_ocr and pg.ocr_reason else ""
            print(f"  Page {n1:3d}: {st}{extra}")
            if pg.markdown and not pg.needs_ocr:
                prev = " ".join(pg.markdown.strip().split())[:100]
                print(f"             Preview: {prev}...")
        result["per_page_ocr_1idx"] = [p.page + 1 for p in per.pages if p.needs_ocr]
    if decide:
        print("\n--- Gate decision (paperless_rearchive.ocr.provenance) ---")
        prov = classify_provenance(path)
        print(f"Kind: {prov.kind}")
        print(f"Pages: {prov.page_count}; needing OCR: {sorted(prov.pages_needing_ocr) or 'none'}")
        print(f"Native pages: {sorted(prov.native_pages) or 'none'}")
        print(f"Source: {prov.source}")
        action = {
            "text_based": "PRESERVE native text (no OCR, no writes)",
            "scanned": "OCR every page",
            "mixed": "OCR only the pages listed above; native pages kept",
            "unknown": "provenance unknown -> mode-driven fallback (REARCHIVE_OCR_MODE)",
        }.get(prov.kind, prov.kind)
        print(f"Sidecar action: {action}")
        result["gate_kind"] = prov.kind
    return result
def assessment(pdf_type: str) -> None:
    print("\n--- Digital-Born Assessment ---")
    if pdf_type == "text_based":
        print("TEXT-BASED (digital-born): do NOT OCR, keep native text.")
    elif pdf_type == "scanned":
        print("SCANNED: OCR is appropriate.")
    elif pdf_type == "mixed":
        print("MIXED: OCR only listed pages, keep rest native.")
    else:
        print(f"Type: {pdf_type}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Classify PDFs with pdf-inspector.")
    ap.add_argument("documents", nargs="+", help="Document ID(s) or file path(s)")
    ap.add_argument("--per-page", action="store_true")
    ap.add_argument("--compare", action="store_true", help="Original vs archive")
    ap.add_argument("--decide", action="store_true",
                    help="Show the sidecar's provenance gate decision for each file")
    ap.add_argument("--version", choices=["original", "archive"], default="original")
    args = ap.parse_args()
    if not PDF_INSPECTOR_AVAILABLE:
        print("ERROR: run with .venv/bin/python", file=sys.stderr)
        sys.exit(2)
    api = None
    if any(d.isdigit() for d in args.documents):
        token = _load_token()
        if not token:
            print(f"ERROR: no token in PAPERLESS_API_TOKEN or {TOKEN_FILE}",
                  file=sys.stderr)
            sys.exit(2)
        api = PaperlessAPI(PAPERLESS_BASE_URL, token)
    results: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for doc in args.documents:
            if doc.isdigit() and api is not None:
                doc_id = int(doc)
                if args.compare:
                    print(f"\n{'#' * 70}\n# Doc {doc_id} - Original vs Archive\n{'#' * 70}")
                    orig_p = _download(api, doc_id, original=True, dest_dir=tmpdir)
                    arch_p = _download(api, doc_id, original=False, dest_dir=tmpdir)
                    orig = inspect_path(orig_p, f"Doc {doc_id} (original)",
                                        per_page=args.per_page, decide=args.decide)
                    arch = inspect_path(arch_p, f"Doc {doc_id} (archive)",
                                        per_page=args.per_page, decide=args.decide)
                    print(f"\nOriginal: {orig['pdf_type']} ocr={orig['pages_needing_ocr'] or 'none'}")
                    print(f"Archive:  {arch['pdf_type']} ocr={arch['pages_needing_ocr'] or 'none'}")
                    assessment(orig["pdf_type"])
                    results += [orig, arch]
                else:
                    p = _download(api, doc_id, original=(args.version == "original"), dest_dir=tmpdir)
                    results.append(inspect_path(p, f"Doc {doc_id} ({args.version})",
                                                per_page=args.per_page, decide=args.decide))
            else:
                p = Path(doc)
                if not p.exists():
                    print(f"File not found: {p}", file=sys.stderr)
                    continue
                results.append(inspect_path(p, f"File: {p.name}", per_page=args.per_page,
                                            decide=args.decide))
    print(f"\n{'#' * 70}\n# Summary\n{'#' * 70}")
    for r in results:
        print(f"  [{r.get('pdf_type', '?'):11s}] {r.get('label')}")
    print(f"\nTotal: {len(results)}")


if __name__ == "__main__":
    main()

