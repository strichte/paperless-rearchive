"""Ingest-parity ocrmypdf argument builder and text semantics.

Mirrors ``paperless_chandra.parser.construct_ocrmypdf_parameters`` (which is
itself a near-copy of paperless's ``parsers/tesseract.py``) so that a re-OCR
run drives exactly the ocrmypdf invocation ingest drives - same mode flags,
clean/deskew/rotate semantics, ``pages`` XOR ``sidecar``, ``user_args``
merged last, and the Chandra plugin as the OCR engine.

Also mirrors the paperless text helpers used around the OCR call:

* :func:`post_process_text` - whitespace normalisation (verbatim port)
* :func:`extract_pdf_text` - ``pdftotext`` wrapper
* :func:`is_tagged_pdf` / :func:`is_born_digital_text` - the born-digital
  rule (``tagged or normalised text > PDF_TEXT_MIN_LENGTH``)
* :func:`sidecar_content` - sidecar reading incl. the ``[OCR skipped on
  page`` placeholder handling (discard sidecar, fall back to ``pdftotext``
  of the produced PDF)

Deliberate deviation (D2, doc/OCR_STRATEGY.md): ``REARCHIVE_OCR_MODE=auto``
upgrades to ``redo`` when the PDF already has a text layer. Literal parity
would make ``auto`` a no-op (PDF/A conversion only) for exactly the
documents a re-OCR tool exists to process; use ``off`` explicitly when a
PDF/A-conversion-only run is wanted.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Same value as paperless.parsers.utils.PDF_TEXT_MIN_LENGTH.
PDF_TEXT_MIN_LENGTH = 50

#: ocrmypdf plugin that replaces the default Tesseract engine with Chandra.
_OCRMYPDF_PLUGIN_MODULE = "paperless_chandra.ocrmypdf_plugin"

_LEGACY_MODE_MAP = {"skip": "auto", "skip_noarchive": "auto"}
_VALID_MODES = {"auto", "force", "redo", "off"}


def post_process_text(text: str | None) -> str | None:
    """Normalise extracted PDF/OCR text (verbatim port of paperless).

    Returns ``None`` for ``None`` or whitespace-only input, so callers can
    treat "no text" and "only layout padding" the same way.
    """
    if not text:
        return None

    collapsed_spaces = re.sub(r"([^\S\r\n]+)", " ", text)
    no_leading_whitespace = re.sub(r"([\n\r]+)([^\S\n\r]+)", r"\1", collapsed_spaces)
    no_trailing_whitespace = re.sub(r"([^\S\n\r]+)$", "", no_leading_whitespace)

    # replace \0 prevents issues with saving to postgres.
    # text may contain \0 when this character is present in PDF files.
    result = no_trailing_whitespace.strip().replace("\0", " ")
    return result or None


def extract_pdf_text(path: Path) -> str | None:
    """Run ``pdftotext`` on *path* and return the extracted text, or None."""
    try:
        result = subprocess.run(
            ["pdftotext", str(path), "-"],
            capture_output=True,
            check=True,
            timeout=120,
        )
        return result.stdout.decode("utf-8", errors="replace")
    except Exception:
        log.warning("Error while getting text from PDF document with pdftotext", exc_info=True)
        return None


def is_tagged_pdf(path: Path) -> bool:
    """True when the PDF declares itself tagged (``/MarkInfo /Marked true``).

    Port of ``paperless.parsers.utils.is_tagged_pdf``.
    """
    import pikepdf

    try:
        with pikepdf.open(path) as pdf:
            mark_info = pdf.Root.get("/MarkInfo")
            if mark_info is None:
                return False
            return bool(mark_info.get("/Marked", False))
    except Exception:
        log.warning("Error while checking for tagged PDF: %s", path, exc_info=True)
        return False


def is_born_digital_text(text: str | None, path: Path) -> bool:
    """Born-digital rule: tagged PDF **or** normalised text > threshold."""
    if not text:
        return False
    return is_tagged_pdf(path) or len(text) > PDF_TEXT_MIN_LENGTH


def pdf_born_digital_text(path: Path) -> bool:
    """Extract + normalise text from *path*, then apply the born-digital rule."""
    return is_born_digital_text(post_process_text(extract_pdf_text(path)), path)


def resolve_mode(mode: str, *, pdf_has_text: bool) -> str:
    """Map REARCHIVE_OCR_MODE onto an effective ocrmypdf mode.

    Legacy ``skip``/``skip_noarchive`` map to ``auto`` (as paperless does).
    ``auto`` upgrades to ``redo`` on text-bearing PDFs (see module docstring).
    """
    mode = _LEGACY_MODE_MAP.get(mode, mode)
    if mode not in _VALID_MODES:
        raise ValueError(f"Invalid OCR mode: {mode!r} (expected one of {sorted(_VALID_MODES)})")
    if mode == "auto" and pdf_has_text:
        log.info(
            "mode=auto but the PDF already has a text layer; upgrading to "
            "redo (a re-OCR run must re-OCR; use mode=off for PDF/A-conversion-only)"
        )
        return "redo"
    return mode


def build_ocrmypdf_args(
    *,
    input_file: Path,
    output_file: Path,
    sidecar_file: Path,
    language: str,
    mode: str,
    clean: str = "clean",
    deskew: bool = True,
    rotate: bool = True,
    rotate_threshold: float = 12.0,
    output_type: str = "pdfa",
    color_conversion_strategy: str = "RGB",
    jobs: int = 1,
    max_pages: int = 0,
    user_args: dict[str, Any] | None = None,
    chandra_server_url: str = "",
    chandra_model_name: str = "",
    chandra_api_key: str = "",
    chandra_max_output_tokens: int = 0,
    chandra_content_format: str = "markdown",
    safe_fallback: bool = False,
) -> dict[str, Any]:
    """Build the ocrmypdf kwargs, mirroring paperless 1:1.

    ``mode`` here is the *configured* mode (not the resolved one) so the
    safe-fallback/clean/deskew interactions behave exactly as ingest does.
    Input is always a PDF here: the pipeline routes non-PDFs to content-only
    mode before OCR.
    """
    ocrmypdf_args: dict[str, Any] = {
        "input_file_or_options": input_file,
        "output_file": output_file,
        # need to use threads, since this runs inside a threaded service
        "use_threads": True,
        "jobs": max(1, int(jobs or 1)),
        "language": language or "eng",
        "output_type": output_type,
        "progress_bar": False,
        # ─ Chandra engine wiring ────────────────────────────────────────
        "plugins": [_OCRMYPDF_PLUGIN_MODULE],
        # Custom kwargs registered by the plugin's add_options hookimpl.
        "chandra_server_url": chandra_server_url,
        "chandra_model_name": chandra_model_name,
        "chandra_api_key": chandra_api_key,
        "chandra_max_output_tokens": chandra_max_output_tokens,
        "chandra_content_format": chandra_content_format,
    }

    if "pdfa" in ocrmypdf_args["output_type"]:
        ocrmypdf_args["color_conversion_strategy"] = color_conversion_strategy

    # OCR-mode flags (mutually exclusive). See tesseract.py:293-302.
    if safe_fallback or mode == "force":
        ocrmypdf_args["force_ocr"] = True
    elif mode == "redo":
        ocrmypdf_args["redo_ocr"] = True
    elif mode == "off":
        ocrmypdf_args["skip_text"] = True
    elif mode == "auto":
        pass
    else:  # pragma: no cover - resolve_mode() validates earlier
        raise ValueError(f"Invalid ocr mode: {mode}")

    if clean == "clean":
        ocrmypdf_args["clean"] = True
    elif clean == "final":
        if mode == "redo":
            ocrmypdf_args["clean"] = True
        else:
            ocrmypdf_args["clean_final"] = True

    if deskew and mode != "redo":
        # --deskew is not compatible with --redo-ocr; dropping deskew is
        # what paperless itself does.
        ocrmypdf_args["deskew"] = True

    if rotate:
        ocrmypdf_args["rotate_pages"] = True
        ocrmypdf_args["rotate_pages_threshold"] = rotate_threshold

    if max_pages is not None and max_pages > 0:
        ocrmypdf_args["pages"] = f"1-{max_pages}"
    else:
        ocrmypdf_args["sidecar"] = sidecar_file

    if user_args:
        # Merged last: can override anything above (same as ingest).
        try:
            ocrmypdf_args = {**ocrmypdf_args, **user_args}
        except Exception as e:
            log.warning("Invalid user_args; they will not be used. %s: %s", type(e).__name__, e)

    return ocrmypdf_args


def sidecar_content(sidecar_file: Path, output_pdf: Path) -> str:
    """Content from the ocrmypdf sidecar, with ingest's placeholder rule.

    A sidecar containing ``[OCR skipped on page`` means pages were skipped
    (text already present, or mode=off); ingest discards it and uses
    ``pdftotext`` of the produced PDF instead.
    """
    try:
        text = sidecar_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.warning("Could not read sidecar %s; falling back to pdftotext", sidecar_file)
        text = ""

    if "[OCR skipped on page" in text or not text:
        # Incomplete sidecar (pages skipped / mode=off) or nothing written:
        # ingest discards it and uses pdftotext of the produced PDF instead.
        log.info("Sidecar unusable; using pdftotext of the output PDF")
        text = extract_pdf_text(output_pdf) or ""

    return post_process_text(text) or ""
