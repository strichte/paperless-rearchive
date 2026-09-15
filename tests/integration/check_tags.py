"""Probe how /api/tags/ filters by name, to validate PaperlessAPI.tag_id().

Finding (2026-09-16, paperless-ngx 3.1.3): ``GET /api/tags/?name=X`` IGNORES the
parameter and returns every tag, so ``results[0]`` was always tag id 223.
The reliable approach is to page through ``/api/tags/`` and match ``name``
exactly in the client.

Run:  python tests/integration/check_tags.py
"""

from __future__ import annotations

import os

import requests

BASE = os.environ.get("PAPERLESS_BASE_URL", "http://paperless:8000").rstrip("/")
TOKEN = os.environ["PAPERLESS_API_TOKEN"]
session = requests.Session()
session.headers["Authorization"] = f"Token {TOKEN}"


def probe(params: dict, label: str) -> None:
    r = session.get(f"{BASE}/api/tags/", params=params, timeout=30)
    r.raise_for_status()
    payload = r.json()
    names = [(t["id"], t["name"]) for t in payload.get("results", [])][:5]
    print(f"{label:32} count={payload.get('count'):6} first5={names}")


print("""=== filter semantics ===
NOTE: ?name=X is ignored; every row below reports count=236 unless noted.""")
probe({"name": "re-ocr-all"}, "?name=re-ocr-all")
probe({"name__iexact": "re-ocr-all"}, "?name__iexact=re-ocr-all")
probe({"name__icontains": "re-ocr-all"}, "?name__icontains=re-ocr-all")
probe({"name__exact": "re-ocr-all"}, "?name__exact=re-ocr-all")
probe({"name": "re-ocr-all", "page_size": 100}, "?name=..&page_size=100")

print("\n=== client-side exact match over all pages ===")
found: dict[str, int] = {}
url: str | None = f"{BASE}/api/tags/"
while url:
    r = session.get(url, params={"page_size": 100}, timeout=30)
    r.raise_for_status()
    payload = r.json()
    for tag in payload.get("results", []):
        found[tag["name"]] = tag["id"]
    url = payload.get("next")

print(f"total tags paged: {len(found)}")
for wanted in ("re-ocr-all", "re-ocr-content", "re-ocr-all-success",
               "re-ocr-content-success", "re-ocr-content-failure", "Test"):
    print(f"  exact {wanted:26} -> id {found.get(wanted, 'NOT FOUND')}")
