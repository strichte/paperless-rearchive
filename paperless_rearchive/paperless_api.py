"""paperless-ngx REST API client."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)


class PaperlessError(RuntimeError):
    """A paperless API call failed."""


class PaperlessAPI:
    def __init__(self, base_url: str, token: str, timeout: float = 120.0) -> None:
        if not token:
            raise PaperlessError("PAPERLESS_API_TOKEN is required")
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Token {token}"

    # ---------------------------------------------------------------- helpers

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _check(self, response: requests.Response, what: str) -> requests.Response:
        if not response.ok:
            raise PaperlessError(f"{what}: HTTP {response.status_code}: {response.text[:500]}")
        return response

    # ------------------------------------------------------------------- tags

    def tag_id(self, name: str) -> int | None:
        response = self._check(
            self.session.get(self._url("/api/tags/"), params={"name": name}, timeout=self.timeout),
            f"lookup tag {name!r}",
        )
        results = response.json().get("results", [])
        return int(results[0]["id"]) if results else None

    def ensure_tag(self, name: str) -> int:
        existing = self.tag_id(name)
        if existing is not None:
            return existing
        response = self._check(
            self.session.post(
                self._url("/api/tags/"),
                json={"name": name},
                timeout=self.timeout,
            ),
            f"create tag {name!r}",
        )
        return int(response.json()["id"])

    # -------------------------------------------------------------- documents

    def doc_ids_with_tag(self, tag_id: int, limit: int) -> list[int]:
        ids: list[int] = []
        url: str | None = self._url("/api/documents/")
        while url is not None and len(ids) < limit:
            params = {"tags__id__in": str(tag_id), "page_size": 100, "fields": "id"}
            response = self._check(
                self.session.get(url, params=params, timeout=self.timeout),
                "list documents",
            )
            payload = response.json()
            for result in payload.get("results", []):
                ids.append(int(result["id"]))
                if len(ids) >= limit:
                    break
            url = payload.get("next")
        return ids

    def document(self, doc_id: int) -> dict[str, Any]:
        response = self._check(
            self.session.get(self._url(f"/api/documents/{doc_id}/"), timeout=self.timeout),
            f"fetch document {doc_id}",
        )
        return dict(response.json())

    def download_original(self, doc_id: int, dest_dir: Path) -> Path:
        response = self._check(
            self.session.get(
                self._url(f"/api/documents/{doc_id}/download/"),
                timeout=self.timeout,
                stream=True,
            ),
            f"download original of document {doc_id}",
        )
        filename = "original.bin"
        disposition = response.headers.get("Content-Disposition", "")
        if "filename=" in disposition:
            filename = disposition.split("filename=", 1)[1].strip('"').split(";")[0]
        dest = dest_dir / filename
        with dest.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
        return dest

    def patch_content(self, doc_id: int, content: str) -> None:
        self._check(
            self.session.patch(
                self._url(f"/api/documents/{doc_id}/"),
                json={"content": content},
                timeout=self.timeout,
            ),
            f"patch content of document {doc_id}",
        )

    # ------------------------------------------------------- tag manipulation

    def set_tags(
        self,
        doc_id: int,
        *,
        remove: list[int],
        add: list[int],
        current_tags: list[int],
    ) -> None:
        """Replace the document's tag list in one PATCH."""
        tags = [t for t in current_tags if t not in remove]
        for tag in add:
            if tag not in tags:
                tags.append(tag)
        self._check(
            self.session.patch(
                self._url(f"/api/documents/{doc_id}/"),
                json={"tags": tags},
                timeout=self.timeout,
            ),
            f"update tags of document {doc_id}",
        )
