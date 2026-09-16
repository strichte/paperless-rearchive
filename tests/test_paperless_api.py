"""Tests for the paperless-ngx API client.

The tag-lookup regression is the important one: paperless-ngx ignores unknown
filter parameters, so ``GET /api/tags/?name=X`` returns the whole tag list. The
old implementation took the first result, which meant every tag was reported as
already existing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from paperless_rearchive.paperless_api import PaperlessAPI, PaperlessError


def _api_with(payload: dict, status: int = 200) -> PaperlessAPI:
    api = PaperlessAPI("http://paperless:8000", "t")
    response = MagicMock()
    response.ok = status < 400
    response.status_code = status
    response.json.return_value = payload
    response.text = ""
    api.session.get = MagicMock(return_value=response)  # type: ignore[method-assign]
    api.session.post = MagicMock(return_value=response)  # type: ignore[method-assign]
    api.session.patch = MagicMock(return_value=response)  # type: ignore[method-assign]
    return api


def test_tag_id_uses_supported_filter_param() -> None:
    api = _api_with({"results": [{"id": 251, "name": "re-ocr-all"}]})
    assert api.tag_id("re-ocr-all") == 251
    params = api.session.get.call_args.kwargs["params"]  # type: ignore[union-attr]
    assert params == {"name__iexact": "re-ocr-all"}


def test_tag_id_ignores_unrelated_results() -> None:
    """An ignored filter returns every tag; none of them must match."""
    api = _api_with({"results": [{"id": 223, "name": "1990"}, {"id": 225, "name": "1992"}]})
    assert api.tag_id("re-ocr-all-success") is None


def test_ensure_tag_creates_when_absent() -> None:
    api = _api_with({"results": [{"id": 223, "name": "1990"}]})
    newly_created = MagicMock()
    newly_created.ok = True
    newly_created.status_code = 201
    newly_created.text = ""
    newly_created.json.return_value = {"id": 257, "name": "re-ocr-all-success"}
    api.session.post = MagicMock(return_value=newly_created)  # type: ignore[method-assign]
    assert api.ensure_tag("re-ocr-all-success") == 257
    assert api.session.post.call_args.kwargs["json"] == {"name": "re-ocr-all-success"}


def test_ensure_tag_reuses_existing() -> None:
    api = _api_with({"results": [{"id": 251, "name": "re-ocr-all"}]})
    assert api.ensure_tag("re-ocr-all") == 251
    api.session.post.assert_not_called()  # type: ignore[union-attr]


def test_custom_field_id_uses_supported_filter_param() -> None:
    api = _api_with({"results": [{"id": 7, "name": "OCR engine"}]})
    assert api.custom_field_id("OCR engine") == 7
    params = api.session.get.call_args.kwargs["params"]  # type: ignore[union-attr]
    assert params == {"name__iexact": "OCR engine"}


def test_custom_field_id_ignores_unrelated_results() -> None:
    api = _api_with({"results": [{"id": 3, "name": "Invoice #"}, {"id": 4, "name": "Total"}]})
    assert api.custom_field_id("OCR engine") is None


def test_ensure_custom_field_creates_when_absent() -> None:
    api = _api_with({"results": []})
    newly_created = MagicMock()
    newly_created.ok = True
    newly_created.status_code = 201
    newly_created.text = ""
    newly_created.json.return_value = {"id": 9, "name": "OCR engine", "data_type": "string"}
    api.session.post = MagicMock(return_value=newly_created)  # type: ignore[method-assign]
    assert api.ensure_custom_field("OCR engine", "string") == 9
    assert api.session.post.call_args.kwargs["json"] == {
        "name": "OCR engine",
        "data_type": "string",
    }


def test_ensure_custom_field_reuses_existing() -> None:
    api = _api_with({"results": [{"id": 7, "name": "OCR engine"}]})
    assert api.ensure_custom_field("OCR engine", "string") == 7
    api.session.post.assert_not_called()  # type: ignore[union-attr]


def test_ensure_provenance_fields_covers_all_definitions() -> None:
    api = _api_with({"results": []})
    created: list[dict] = []

    def _post(url: str, json: dict, timeout: float) -> MagicMock:
        resp = MagicMock(ok=True, status_code=201, text="")
        resp.json.return_value = {"id": 100 + len(created), **json}
        created.append(json)
        return resp

    api.session.post = MagicMock(side_effect=_post)  # type: ignore[method-assign]
    ids = api.ensure_provenance_fields()
    assert set(ids) == {"OCR engine", "OCR date", "OCR pages", "OCR archive ratio"}
    by_name = {c["name"]: c["data_type"] for c in created}
    assert by_name == {
        "OCR engine": "string",
        "OCR date": "date",
        "OCR pages": "string",
        "OCR archive ratio": "float",
    }


def test_set_custom_fields_patch_shape() -> None:
    api = _api_with({"results": []})
    values = [
        {"field": 7, "value": "chandra-ocr-2-q8"},
        {"field": 8, "value": "2026-09-16"},
        {"field": 9, "value": "4/4 ok"},
        {"field": 10, "value": 1.001},
    ]
    api.set_custom_fields(5822, values)
    call = api.session.patch.call_args  # type: ignore[union-attr]
    assert call.args[0].endswith("/api/documents/5822/")
    assert call.kwargs["json"] == {"custom_fields": values}


def test_set_custom_fields_empty_is_noop() -> None:
    api = _api_with({"results": []})
    api.set_custom_fields(5822, [])
    api.session.patch.assert_not_called()  # type: ignore[union-attr]


def test_add_note_posts_to_notes_endpoint() -> None:
    api = _api_with([])
    text = "Re-OCR complete (re-ocr-all)\nEngine: chandra-ocr-2-q8\nPages: 8/8 ok"
    api.add_note(4221, text)
    call = api.session.post.call_args  # type: ignore[union-attr]
    assert call.args[0].endswith("/api/documents/4221/notes/")
    assert call.kwargs["json"] == {"note": text}


def test_add_note_accepts_notes_list_response() -> None:
    # paperless returns the full notes list (HTTP 200, not 201).
    api = _api_with(
        [{"id": 1, "note": "old", "created": "2026-09-16T00:00:00Z", "user": {"id": 1}}]
    )
    api.add_note(4221, "new")  # must not raise


def test_add_note_rejects_error_payload() -> None:
    # paperless may return 200 carrying {"error": ...} on internal failures.
    api = _api_with({"error": "Error saving note, check logs for more detail."})
    with pytest.raises(PaperlessError):
        api.add_note(4221, "boom")


def test_add_note_raises_on_http_error() -> None:
    api = _api_with({}, status=403)
    with pytest.raises(PaperlessError):
        api.add_note(4221, "boom")


def test_doc_ids_with_tag_paginates() -> None:
    api = PaperlessAPI("http://paperless:8000", "t")
    page1 = MagicMock(ok=True, status_code=200, text="")
    page1.json.return_value = {
        "results": [{"id": 1}, {"id": 2}],
        "next": "http://paperless:8000/api/documents/?page=2",
    }
    page2 = MagicMock(ok=True, status_code=200, text="")
    page2.json.return_value = {"results": [{"id": 3}], "next": None}
    api.session.get = MagicMock(side_effect=[page1, page2])  # type: ignore[method-assign]
    assert api.doc_ids_with_tag(251, limit=10) == [1, 2, 3]
    first_params = api.session.get.call_args_list[0].kwargs["params"]  # type: ignore[union-attr]
    assert first_params["tags__id__in"] == "251"
