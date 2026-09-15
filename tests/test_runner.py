"""Tests for the ocrmypdf argument builder and config parsing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from paperless_rearchive.config import Settings
from paperless_rearchive.ocr.runner import (
    build_ocrmypdf_args,
    calculate_a4_dpi,
    guess_mime_type,
    is_image,
)


def _settings(**env: str) -> Settings:
    base = {
        "PAPERLESS_API_TOKEN": "t",
        "PAPERLESS_CHANDRA_SERVER_URL": "http://ai:8110/v1",
        "REARCHIVE_OCR_USER_ARGS": json.dumps({"invalidate_digital_signatures": True}),
    }
    base.update(env)
    with patch.dict("os.environ", base, clear=False):
        return Settings.from_env()


class _FakeProvider:
    name = "fake"
    ocrmypdf_plugin_module = "fake_plugin"

    def ocrmypdf_kwargs(self) -> dict:
        return {"fake_url": "http://x"}

    def validate(self) -> None:
        pass


def test_build_args_defaults(tmp_path: Path) -> None:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    args = build_ocrmypdf_args(pdf, tmp_path / "out.pdf", tmp_path / "s.txt",
                               _FakeProvider(), _settings(), jobs=2)
    assert args["force_ocr"] is True
    assert args["plugins"] == ["fake_plugin"]
    assert args["fake_url"] == "http://x"
    assert args["output_type"] == "pdfa"
    assert args["deskew"] is True
    assert args["sidecar"] == tmp_path / "s.txt"
    assert args["invalidate_digital_signatures"] is True
    assert "image_dpi" not in args


def test_build_args_image_dpi(tmp_path: Path) -> None:
    from PIL import Image

    png = tmp_path / "doc.png"
    Image.new("RGB", (2480, 3508)).save(png, dpi=(300, 300))
    args = build_ocrmypdf_args(png, tmp_path / "out.pdf", tmp_path / "s.txt",
                               _FakeProvider(), _settings())
    assert args["image_dpi"] == 300


def test_build_args_max_pages(tmp_path: Path) -> None:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    s = _settings(REARCHIVE_MAX_PAGES="3")
    args = build_ocrmypdf_args(pdf, tmp_path / "o.pdf", tmp_path / "s.txt",
                               _FakeProvider(), s)
    assert args["pages"] == "1-3"
    assert "sidecar" not in args


def test_mime_helpers() -> None:
    assert guess_mime_type(Path("x.pdf")) == "application/pdf"
    assert guess_mime_type(Path("x.TIF")) == "image/tiff"
    assert is_image("image/png")
    assert not is_image("application/pdf")


def test_a4_dpi_wide_image(tmp_path: Path) -> None:
    from PIL import Image

    img = tmp_path / "wide.png"
    Image.new("RGB", (3000, 1000)).save(img)
    assert calculate_a4_dpi(img) == 363  # 3000 / 8.27


def test_settings_defaults() -> None:
    s = _settings()
    assert s.trigger_tag_all == "re-ocr-all"
    assert s.ocr_language == "eng"
    assert s.dry_run is False


def test_settings_bad_user_args() -> None:
    with pytest.raises(ValueError, match="REARCHIVE_OCR_USER_ARGS"):
        _settings(REARCHIVE_OCR_USER_ARGS="not-json")
