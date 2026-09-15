"""Tests for the paperless-ngx API client.

The tag-lookup regression is the important one: paperless-ngx ignores unknown
filter parameters, so ``GET /api/tags/?name=X`` returns the whole tag list. The
old implementation took the first result, which meant every tag was reported as
already existing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from paperless_rearchive.paperless_api import PaperlessAPI


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
