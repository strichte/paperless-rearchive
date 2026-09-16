"""paperless-ngx REST API client."""

from __future__ import annotations

import logging
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import requests

log = logging.getLogger(__name__)


class PaperlessError(RuntimeError):
    """A paperless API call failed."""


def filename_from_disposition(disposition: str) -> str | None:
    """Extract a usable basename from a ``Content-Disposition`` header.

    Splitting on ``filename=`` and stripping quotes (the previous approach) left
    the trailing quote of ``filename="a b.pdf"`` in place, which produced a
    ``.pdf"`` suffix: the file was then typed ``application/octet-stream`` and
    the OCR strategy selection silently degraded to ``skip_text``. ``Message``
    implements the header parsing rules properly, including RFC 2231/5987
    (``filename*=UTF-8''...``) and quoted-string unescaping. Any directory
    component is dropped so a hostile filename cannot escape the temp dir.
    """
    if not disposition:
        return None
    message = Message()
    message["Content-Disposition"] = disposition
    name = message.get_filename()
    if not name:
        return None
    name = unquote(name).strip()
    return Path(name).name or None


class PaperlessAPI:
    def __init__(self, base_url: str, token: str, timeout: float = 120.0) -> None:
        if not token:
            raise PaperlessError("PAPERLESS_API_TOKEN (or PAPERLESS_API_TOKEN_FILE) is required")
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
        """Resolve a tag id by exact name (case-insensitive).

        .. warning::

            paperless-ngx silently ignores unknown filter parameters and returns
            the *unfiltered* list, so the obvious ``?name=`` query yields the
            first tag in the database rather than nothing. That bug once made
            :meth:`ensure_tag` report every tag as existing, so trigger tags were
            never created and documents were never re-tagged. The supported
            parameter is ``name__iexact``; the returned ``name`` is checked
            anyway, because a silently ignored filter must never be mistaken for
            a match.
        """
        response = self._check(
            self.session.get(
                self._url("/api/tags/"),
                params={"name__iexact": name},
                timeout=self.timeout,
            ),
            f"lookup tag {name!r}",
        )
        for result in response.json().get("results", []):
            if result.get("name") == name:
                return int(result["id"])
        return None

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

    # -------------------------------------------------------- custom fields

    #: Provenance fields the sidecar manages: (field name, data_type).
    #: ``string`` caps at 128 chars; ``date`` needs ISO YYYY-MM-DD; ``float``
    #: is used for the archive size ratio so it stays sortable/filterable.
    PROVENANCE_FIELDS: tuple[tuple[str, str], ...] = (
        ("OCR engine", "string"),
        ("OCR date", "date"),
        ("OCR pages", "string"),
        ("OCR archive ratio", "float"),
    )

    def custom_field_id(self, name: str) -> int | None:
        """Resolve a custom-field id by exact name.

        Same caution as :meth:`tag_id`: unknown filter params are silently
        ignored server-side, so the returned ``name`` is always verified.
        """
        response = self._check(
            self.session.get(
                self._url("/api/custom_fields/"),
                params={"name__iexact": name},
                timeout=self.timeout,
            ),
            f"lookup custom field {name!r}",
        )
        for result in response.json().get("results", []):
            if result.get("name") == name:
                return int(result["id"])
        return None

    def ensure_custom_field(self, name: str, data_type: str) -> int:
        """Return the custom-field id, creating the definition if missing.

        ``data_type`` is immutable server-side, so a name clash with a
        different type is a hard error rather than a silent reuse.
        """
        existing = self.custom_field_id(name)
        if existing is not None:
            return existing
        try:
            response = self._check(
                self.session.post(
                    self._url("/api/custom_fields/"),
                    json={"name": name, "data_type": data_type},
                    timeout=self.timeout,
                ),
                f"create custom field {name!r}",
            )
        except PaperlessError as e:
            # A 400 here is usually a duplicate name with a different type
            # (names are unique); surface that plainly.
            raise PaperlessError(
                f"could not create custom field {name!r} ({data_type}): {e}"
            ) from e
        return int(response.json()["id"])

    def ensure_provenance_fields(self) -> dict[str, int]:
        """Ensure all sidecar-managed custom-field definitions exist."""
        return {
            name: self.ensure_custom_field(name, data_type)
            for name, data_type in self.PROVENANCE_FIELDS
        }

    def set_custom_fields(
        self, doc_id: int, values: list[dict[str, Any]]
    ) -> None:
        """Upsert custom-field values on a document (one PATCH).

        ``values`` is a list of ``{"field": <id>, "value": ...}``; the
        server's ``update_or_create(document, field)`` semantics mean only
        the listed fields are touched - other fields' instances are left
        alone. ``value: None`` clears that field's instance.
        """
        if not values:
            return
        self._check(
            self.session.patch(
                self._url(f"/api/documents/{doc_id}/"),
                json={"custom_fields": values},
                timeout=self.timeout,
            ),
            f"update custom fields of document {doc_id}",
        )

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
        """Download the immutable *original* file of a document.

        ``/api/documents/<id>/download/`` serves the **archive** version when the
        document has one - ``DocumentViewSet.file_response`` calls
        ``serve_file(use_archive=not self.original_requested(request) ...)``. The
        original is only returned when the ``original=true`` query parameter is
        present, so it must always be sent here: re-OCR must read the untouched
        source, never the derived archive.
        """
        response = self._check(
            self.session.get(
                self._url(f"/api/documents/{doc_id}/download/"),
                params={"original": "true"},
                timeout=self.timeout,
                stream=True,
            ),
            f"download original of document {doc_id}",
        )
        filename = filename_from_disposition(response.headers.get("Content-Disposition", ""))
        dest = dest_dir / (filename or "original.bin")
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
