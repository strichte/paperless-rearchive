"""Tests for the ingest-parity ocrmypdf argument builder (ocr/ingest_args.py)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from paperless_rearchive.ocr.ingest_args import (
    PDF_TEXT_MIN_LENGTH,
    build_ocrmypdf_args,
    is_born_digital_text,
    is_tagged_pdf,
    pdf_born_digital_text,
    post_process_text,
    resolve_mode,
    sidecar_content,
)

_CHANDRA = {
    "chandra_server_url": "http://ai:8110",
    "chandra_model_name": "chandra-ocr",
    "chandra_api_key": "secret",
    "chandra_max_output_tokens": 12384,
    "chandra_content_format": "markdown",
}


def _args(**kw):
    base = dict(
        input_file=Path("/in/doc.pdf"),
        output_file=Path("/out/archive.pdf"),
        sidecar_file=Path("/out/sidecar.txt"),
        language="eng",
        mode="redo",
        user_args=None,
        **_CHANDRA,
    )
    base.update(kw)
    return build_ocrmypdf_args(**base)


# ── post_process_text ────────────────────────────────────────────────────────


def test_post_process_text_normalises_whitespace() -> None:
    # whitespace *after* newlines is stripped; before them is collapsed only
    assert post_process_text("  a   b \r\n   c ") == "a b \r\nc"
    assert post_process_text("a\x00b") == "a b"
    assert post_process_text(None) is None
    assert post_process_text("   \r\n ") is None


def test_post_process_text_matches_paperless_samples() -> None:
    # form-feed padding from pdftotext disappears into normalisation
    assert post_process_text("\x0c\x0c") is None
    assert post_process_text("word\x0cword") == "word word"


# ── build_ocrmypdf_args ──────────────────────────────────────────────────────


def test_base_args_mirror_ingest() -> None:
    args = _args()
    assert args["input_file_or_options"] == Path("/in/doc.pdf")
    assert args["use_threads"] is True
    assert args["jobs"] == 1
    assert args["language"] == "eng"
    assert args["output_type"] == "pdfa"
    assert args["color_conversion_strategy"] == "RGB"
    assert args["progress_bar"] is False
    assert args["plugins"] == ["paperless_chandra.ocrmypdf_plugin"]
    assert args["chandra_api_key"] == "secret"


def test_redo_drops_deskew_and_keeps_clean() -> None:
    args = _args(mode="redo", deskew=True)
    assert args["redo_ocr"] is True
    assert "deskew" not in args
    assert args["clean"] is True
    assert args["sidecar"] == Path("/out/sidecar.txt")


def test_clean_final_with_redo_becomes_clean() -> None:
    args = _args(mode="redo", clean="final")
    assert args["clean"] is True
    assert "clean_final" not in args


def test_clean_final_without_redo_is_clean_final() -> None:
    args = _args(mode="force", clean="final")
    assert args["clean_final"] is True
    assert "clean" not in args


def test_safe_fallback_switches_to_force_ocr() -> None:
    args = _args(mode="redo", safe_fallback=True)
    assert args["force_ocr"] is True
    assert "redo_ocr" not in args
    # paperless keeps clean per settings; deskew stays dropped because the
    # configured mode is redo
    assert args["clean"] is True
    assert "deskew" not in args


def test_safe_fallback_auto_keeps_deskew() -> None:
    args = _args(mode="auto", safe_fallback=True)
    assert args["force_ocr"] is True
    assert args["deskew"] is True


def test_off_mode_uses_skip_text() -> None:
    args = _args(mode="off")
    assert args["skip_text"] is True


def test_pages_xors_sidecar() -> None:
    args = _args(max_pages=5)
    assert args["pages"] == "1-5"
    assert "sidecar" not in args
    args = _args(max_pages=0)
    assert "pages" not in args
    assert args["sidecar"] == Path("/out/sidecar.txt")


def test_user_args_merged_last_can_override() -> None:
    args = _args(user_args={"output_type": "pdf", "redo_ocr": False})
    assert args["output_type"] == "pdf"
    assert args["redo_ocr"] is False


def test_no_pdfa_no_color_strategy() -> None:
    args = _args(output_type="pdf")
    assert "color_conversion_strategy" not in args


def test_jobs_floor_is_one() -> None:
    assert _args(jobs=0)["jobs"] == 1
    assert _args(jobs=4)["jobs"] == 4


# ── mode resolution ──────────────────────────────────────────────────────────


def test_resolve_mode_auto_upgrades_to_redo_with_text() -> None:
    assert resolve_mode("auto", pdf_has_text=True) == "redo"


def test_resolve_mode_auto_stays_without_text() -> None:
    assert resolve_mode("auto", pdf_has_text=False) == "auto"


def test_resolve_mode_explicit_modes_untouched() -> None:
    for mode in ("force", "redo", "off"):
        assert resolve_mode(mode, pdf_has_text=True) == mode


def test_resolve_mode_legacy_names_map_to_auto() -> None:
    assert resolve_mode("skip", pdf_has_text=False) == "auto"
    assert resolve_mode("skip_noarchive", pdf_has_text=False) == "auto"


def test_resolve_mode_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        resolve_mode("turbo", pdf_has_text=False)


# ── born-digital rule ────────────────────────────────────────────────────────


def _untagged_pdf(tmp_path: Path) -> Path:
    from PIL import Image

    path = tmp_path / "untagged.pdf"
    Image.new("RGB", (10, 10), "white").save(path, format="PDF")
    return path


def test_born_digital_threshold(tmp_path: Path) -> None:
    path = _untagged_pdf(tmp_path)
    assert not is_tagged_pdf(path)
    assert not is_born_digital_text("x" * PDF_TEXT_MIN_LENGTH, path)
    assert is_born_digital_text("x" * (PDF_TEXT_MIN_LENGTH + 1), path)
    assert not is_born_digital_text(None, path)


def test_tagged_pdf_counts_as_born_digital(tmp_path: Path) -> None:
    import pikepdf

    path = _untagged_pdf(tmp_path)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root.MarkInfo = pikepdf.Dictionary(Marked=True)
        pdf.save(path)
    assert is_tagged_pdf(path)
    assert is_born_digital_text("short", path)  # under the threshold, but tagged


def test_pdf_born_digital_text_uses_normalised_length(tmp_path: Path) -> None:
    path = _untagged_pdf(tmp_path)
    # whitespace-only extraction normalises to nothing -> not born-digital
    with patch(
        "paperless_rearchive.ocr.ingest_args.extract_pdf_text",
        return_value="\x0c   \x0c" * 30,
    ):
        assert not pdf_born_digital_text(path)


# ── sidecar content ──────────────────────────────────────────────────────────


def test_sidecar_content_normalises(tmp_path: Path) -> None:
    sc = tmp_path / "s.txt"
    sc.write_text("hello \x00 world  \n  next  ", encoding="utf-8", newline="")
    # \0 becomes a space (kept separate from the collapse rule, as in paperless)
    assert sidecar_content(sc, tmp_path / "out.pdf") == "hello   world \nnext"


def test_sidecar_placeholder_discards_and_uses_pdftotext(tmp_path: Path) -> None:
    sc = tmp_path / "s.txt"
    sc.write_text("[OCR skipped on page 1 (already has text)]", encoding="utf-8")
    out = tmp_path / "out.pdf"
    with patch(
        "paperless_rearchive.ocr.ingest_args.extract_pdf_text",
        return_value="recovered text  ",
    ) as m:
        assert sidecar_content(sc, out) == "recovered text"
    m.assert_called_once_with(out)


def test_sidecar_missing_file_falls_back(tmp_path: Path) -> None:
    with patch(
        "paperless_rearchive.ocr.ingest_args.extract_pdf_text",
        return_value="fallback",
    ):
        assert sidecar_content(tmp_path / "nope.txt", tmp_path / "out.pdf") == "fallback"
