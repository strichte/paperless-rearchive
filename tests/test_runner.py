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
        "REARCHIVE_BACKUP_DIRECTORY": "/archive-backups",
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
                               _FakeProvider(), _settings(REARCHIVE_OCR_MODE="auto"), jobs=2)
    # auto + no text layer in the input -> ocrmypdf may OCR bare pages only.
    assert args["skip_text"] is True
    assert "force_ocr" not in args
    assert args["plugins"] == ["fake_plugin"]
    assert args["fake_url"] == "http://x"
    assert args["output_type"] == "pdfa"
    assert args["deskew"] is True
    assert args["sidecar"] == tmp_path / "s.txt"
    assert args["invalidate_digital_signatures"] is True
    assert "image_dpi" not in args


def _with_text_layer():
    return patch("paperless_rearchive.ocr.runner.has_text_layer", return_value=True)


def test_auto_prefers_redo_on_existing_text(tmp_path: Path) -> None:
    """auto must never rasterise: text-bearing PDFs get redo_ocr, and deskew is
    dropped because ocrmypdf rejects --redo-ocr together with --deskew."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with _with_text_layer():
        args = build_ocrmypdf_args(pdf, tmp_path / "o.pdf", tmp_path / "s.txt",
                                   _FakeProvider(), _settings())
    assert args["redo_ocr"] is True
    assert "force_ocr" not in args
    assert "skip_text" not in args
    assert "deskew" not in args


def test_redo_mode_drops_deskew(tmp_path: Path) -> None:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    s = _settings(REARCHIVE_OCR_MODE="redo", REARCHIVE_OCR_DESKEW="true")
    args = build_ocrmypdf_args(pdf, tmp_path / "o.pdf", tmp_path / "s.txt", _FakeProvider(), s)
    assert args["redo_ocr"] is True
    assert "deskew" not in args


def test_force_mode_rasterises_and_keeps_deskew(tmp_path: Path) -> None:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    s = _settings(REARCHIVE_OCR_MODE="force")
    args = build_ocrmypdf_args(pdf, tmp_path / "o.pdf", tmp_path / "s.txt", _FakeProvider(), s)
    assert args["force_ocr"] is True
    assert "redo_ocr" not in args
    assert args["deskew"] is True


def test_no_deskew_when_disabled(tmp_path: Path) -> None:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    s = _settings(REARCHIVE_OCR_DESKEW="false")
    args = build_ocrmypdf_args(pdf, tmp_path / "o.pdf", tmp_path / "s.txt", _FakeProvider(), s)
    assert "deskew" not in args


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


# --------------------------------------------------------------------------
# Regression tests for the bugs found during the first real end-to-end run.
# --------------------------------------------------------------------------


class TestFilenameFromDisposition:
    """The old split-on-'filename=' parser left a trailing quote behind."""

    def test_quoted_filename_has_no_trailing_quote(self) -> None:
        from paperless_rearchive.paperless_api import filename_from_disposition

        name = filename_from_disposition('attachment; filename="2026-01-07 a b.pdf"')
        assert name == "2026-01-07 a b.pdf"
        assert not name.endswith('"')

    def test_unquoted_filename(self) -> None:
        from paperless_rearchive.paperless_api import filename_from_disposition

        assert filename_from_disposition("attachment; filename=scan.pdf") == "scan.pdf"

    def test_rfc5987_filename(self) -> None:
        from paperless_rearchive.paperless_api import filename_from_disposition

        name = filename_from_disposition(
            "attachment; filename*=UTF-8''%C3%9Cbersicht%20Q1.pdf",
        )
        assert name == "Übersicht Q1.pdf"

    def test_directory_components_are_stripped(self) -> None:
        from paperless_rearchive.paperless_api import filename_from_disposition

        assert filename_from_disposition('attachment; filename="../../etc/passwd"') == "passwd"

    def test_empty_disposition(self) -> None:
        from paperless_rearchive.paperless_api import filename_from_disposition

        assert filename_from_disposition("") is None


class TestSniffMimeType:
    """A PDF must never be typed as octet-stream: it decides the OCR mode."""

    def test_pdf_magic(self, tmp_path: Path) -> None:
        from paperless_rearchive.ocr.runner import sniff_mime_type

        pdf = tmp_path / "no-extension"
        pdf.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        assert sniff_mime_type(pdf) == "application/pdf"

    def test_guess_falls_back_to_sniffing(self, tmp_path: Path) -> None:
        from paperless_rearchive.ocr.runner import guess_mime_type

        pdf = tmp_path / 'weird.pdf"'
        pdf.write_bytes(b"%PDF-1.4\n")
        assert guess_mime_type(pdf) == "application/pdf"

    def test_unknown_bytes(self, tmp_path: Path) -> None:
        from paperless_rearchive.ocr.runner import sniff_mime_type

        blob = tmp_path / "x.bin"
        blob.write_bytes(b"\x00\x01\x02\x03")
        assert sniff_mime_type(blob) == "application/octet-stream"


class TestOcrSkippedAll:
    """Placeholder-only sidecars must never reach the document content field."""

    def test_placeholder_only(self) -> None:
        from paperless_rearchive.ocr.runner import ocr_skipped_all

        assert ocr_skipped_all("[OCR skipped on page(s) 1-3]")

    def test_placeholder_among_real_text(self) -> None:
        from paperless_rearchive.ocr.runner import ocr_skipped_all

        assert not ocr_skipped_all("[OCR skipped on page(s) 1]\n\nReal recognised text.")

    def test_plain_text(self) -> None:
        from paperless_rearchive.ocr.runner import ocr_skipped_all

        assert not ocr_skipped_all("# Invoice\n\nTotal: 42 EUR")

    def test_empty(self) -> None:
        from paperless_rearchive.ocr.runner import ocr_skipped_all

        assert ocr_skipped_all("")


class TestSelectOcrStrategy:
    """auto must never rasterise: redo for text PDFs, skip_text otherwise."""

    def test_force_mode(self, tmp_path: Path) -> None:
        from paperless_rearchive.ocr.runner import select_ocr_strategy

        pdf = tmp_path / "a.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        assert select_ocr_strategy("force", pdf, "application/pdf") == "force"

    def test_redo_mode(self, tmp_path: Path) -> None:
        from paperless_rearchive.ocr.runner import select_ocr_strategy

        pdf = tmp_path / "a.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        assert select_ocr_strategy("redo", pdf, "application/pdf") == "redo"

    def test_auto_without_text_layer_uses_skip_text(self, tmp_path: Path) -> None:
        from paperless_rearchive.ocr.runner import select_ocr_strategy

        pdf = tmp_path / "a.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        assert select_ocr_strategy("auto", pdf, "application/pdf") == "skip_text"

    def test_auto_with_text_layer_uses_redo(self, tmp_path: Path) -> None:
        from paperless_rearchive.ocr.runner import select_ocr_strategy

        pdf = tmp_path / "a.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        with patch("paperless_rearchive.ocr.runner.has_text_layer", return_value=True):
            assert select_ocr_strategy("auto", pdf, "application/pdf") == "redo"

    def test_auto_with_text_layer_skips_digital_born(self, tmp_path: Path) -> None:
        from paperless_rearchive.ocr.runner import select_ocr_strategy

        pdf = tmp_path / "a.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        # When pdf_is_digital_born=True is passed AND there's a text layer,
        # the function should return "off" to skip OCR on digital-born PDFs
        with patch(
            "paperless_rearchive.ocr.runner.has_text_layer", return_value=True
        ):
            assert select_ocr_strategy(
                "auto", pdf, "application/pdf", pdf_is_digital_born=True
            ) == "off"

    def test_auto_non_pdf_uses_skip_text(self, tmp_path: Path) -> None:
        from paperless_rearchive.ocr.runner import select_ocr_strategy

        png = tmp_path / "a.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        assert select_ocr_strategy("auto", png, "image/png") == "skip_text"


def test_redo_drops_deskew(tmp_path: Path) -> None:
    """ocrmypdf rejects --redo-ocr together with --deskew."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with patch("paperless_rearchive.ocr.runner.has_text_layer", return_value=True):
        args = build_ocrmypdf_args(
            pdf, tmp_path / "o.pdf", tmp_path / "s.txt", _FakeProvider(), _settings()
        )
    assert args["redo_ocr"] is True
    assert "deskew" not in args
    assert "force_ocr" not in args
