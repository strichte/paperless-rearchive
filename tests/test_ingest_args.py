"""Tests for the ingest-parity ocrmypdf argument builder (ocr/ingest_args.py)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from paperless_rearchive.ocr.ingest_args import (
    PDF_TEXT_MIN_LENGTH,
    build_ocrmypdf_args,
    effective_mode,
    has_visible_text_content,
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


def test_resolve_mode_auto_skips_digital_born() -> None:
    """auto + text + digital-born => off (preserve visible text)."""
    assert resolve_mode("auto", pdf_has_text=True, pdf_is_digital_born=True) == "off"


def test_resolve_mode_auto_redoes_ocr_text() -> None:
    """auto + text + not digital-born (OCR overlay) => redo."""
    assert resolve_mode("auto", pdf_has_text=True, pdf_is_digital_born=False) == "redo"


def test_resolve_mode_auto_unchanged_without_text() -> None:
    """auto + no text => auto, regardless of digital-born guess."""
    assert resolve_mode("auto", pdf_has_text=False, pdf_is_digital_born=True) == "auto"


def test_resolve_mode_explicit_modes_untouched() -> None:
    for mode in ("force", "redo", "off"):
        assert resolve_mode(mode, pdf_has_text=True, pdf_is_digital_born=True) == mode


def test_resolve_mode_legacy_names_map_to_auto() -> None:
    assert resolve_mode("skip", pdf_has_text=False, pdf_is_digital_born=False) == "auto"
    assert resolve_mode("skip_noarchive", pdf_has_text=False, pdf_is_digital_born=False) == "auto"


def test_resolve_mode_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        resolve_mode("turbo", pdf_has_text=False, pdf_is_digital_born=False)


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


# ── explicit skip mode ───────────────────────────────────────────────────────


def test_skip_mode_uses_skip_text() -> None:
    """``skip`` is the explicit per-page ``skip_text`` mode (layer 2)."""
    args = _args(mode="skip")
    assert args["skip_text"] is True
    assert "redo_ocr" not in args


# ── provenance-driven mode mapping (layer 1 -> layer 2) ──────────────────────


def test_effective_mode_explicit_overrides_pass_through() -> None:
    for mode in ("force", "off"):
        for kind in ("text_based", "scanned", "mixed", "unknown"):
            assert effective_mode(mode, kind) == mode


def test_effective_mode_text_based_disables_ocr() -> None:
    assert effective_mode("redo", "text_based") == "off"
    assert effective_mode("auto", "text_based") == "off"


def test_effective_mode_scanned_follows_configured_mode() -> None:
    assert effective_mode("redo", "scanned") == "redo"
    assert effective_mode("auto", "scanned") == "auto"


def test_effective_mode_mixed_degrades_redo_to_skip() -> None:
    """ocrmypdf applies one mode per file: ``redo`` would strip native pages."""
    assert effective_mode("redo", "mixed") == "skip"
    assert effective_mode("redo", "mixed", mixed_mode="redo") == "redo"
    assert effective_mode("auto", "mixed") == "skip"


def test_effective_mode_unknown_falls_back_to_auto() -> None:
    assert effective_mode("redo", "unknown") == "auto"


def test_effective_mode_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        effective_mode("turbo", "scanned")
    with pytest.raises(ValueError):
        effective_mode("redo", "alien")
    with pytest.raises(ValueError):
        effective_mode("redo", "mixed", mixed_mode="turbo")


def test_has_visible_text_content_distinguishes_digital_born(tmp_path: Path) -> None:
    """Visible native text -> True; a page that is one big scan image -> False."""
    import fitz

    born = tmp_path / "born.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    for line in range(20):
        page.insert_text((72, 72 + line * 20), "Visible native text on the page. " * 4)
    doc.save(born)
    doc.close()
    assert has_visible_text_content(born) is True

    src = fitz.open()
    s = src.new_page(width=595, height=842)
    for line in range(20):
        s.insert_text((72, 72 + line * 20), "Baked-in scan content. " * 4)
    pix = s.get_pixmap(dpi=150)
    scan = tmp_path / "scan.pdf"
    out = fitz.open()
    scan_page = out.new_page(width=595, height=842)
    scan_page.insert_image(fitz.Rect(0, 0, 595, 842), pixmap=pix)
    out.save(scan)
    out.close()
    src.close()
    assert has_visible_text_content(scan) is False
