"""Django-free ocrmypdf argument builder (integration-harness only).

**Superseded for the active pipeline** by :mod:`paperless_rearchive.ocr.ingest_args`,
which mirrors ``paperless_chandra.parser.construct_ocrmypdf_parameters`` 1:1 and
drives the single ingest-parity ocrmypdf pass (see doc/OCR_STRATEGY.md).

This module is retained because the integration harness still uses pieces of
it: ``tests/integration/diag_original.py`` and ``check_download.py`` use
``guess_mime_type``/``has_text_layer``; ``size_compare.py`` uses ``run_ocr``;
``tests/test_runner.py`` is its test suite. Do not wire it into the pipeline.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from paperless_rearchive.config import Settings
    from paperless_rearchive.ocr.base import OcrProviderPlugin

log = logging.getLogger(__name__)

_IMAGE_MIME_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tif",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
}


def guess_mime_type(path: Path) -> str:
    """MIME type from the filename, falling back to magic-byte sniffing.

    The filename alone is not trustworthy here: the download filename comes from
    the API's ``Content-Disposition`` header, and a parsing slip (a stray quote
    in the header) used to type a plain PDF as ``application/octet-stream``.
    That in turn made :func:`select_ocr_strategy` treat it as a non-PDF and pick
    ``skip_text``, which OCR'd nothing at all. Sniffing the actual bytes keeps the
    OCR mode decision independent of the name.
    """
    mime, _ = mimetypes.guess_type(path.name)
    if mime is None:
        suffix = path.suffix.lower()
        if suffix in (".tif", ".tiff"):
            return "image/tiff"
        if suffix in (".jpg", ".jpeg"):
            return "image/jpeg"
        return sniff_mime_type(path)
    return mime


def sniff_mime_type(path: Path) -> str:
    """Identify common document/image formats from their magic bytes."""
    try:
        with path.open("rb") as fh:
            head = fh.read(8)
    except OSError:
        return "application/octet-stream"
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head.startswith(b"BM"):
        return "image/bmp"
    return "application/octet-stream"


def is_image(mime_type: str) -> bool:
    return mime_type in _IMAGE_MIME_TYPES


def remove_alpha(input_file: Path, dest_dir: Path) -> Path:
    """Composite an RGBA image onto white; img2pdf cannot handle alpha."""
    from PIL import Image

    dest = dest_dir / f"{input_file.stem}-noalpha.png"
    with Image.open(input_file) as img:
        if img.mode in ("RGBA", "LA", "PA"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            background.save(dest, dpi=img.info.get("dpi", (300, 300)))
        else:
            return input_file
    return dest


def calculate_a4_dpi(input_file: Path) -> int:
    """DPI that fits the image onto A4, mirroring paperless's fallback."""
    from PIL import Image

    a4_width_inch = 8.27
    a4_height_inch = 11.69
    with Image.open(input_file) as img:
        width_dpi = img.width / a4_width_inch
        height_dpi = img.height / a4_height_inch
    return max(1, int(round(max(width_dpi, height_dpi))))


def get_dpi(input_file: Path, fallback_dpi: int = 0) -> int:
    from PIL import Image

    try:
        with Image.open(input_file) as img:
            dpi = img.info.get("dpi", (fallback_dpi, fallback_dpi))[0]
        return int(round(dpi)) if dpi else 0
    except Exception:  # noqa: BLE001 - DPI is best-effort metadata
        return 0


def has_text_layer(input_file: Path) -> bool:
    """True when the PDF already carries an extractable text layer.

    Used to pick the ocrmypdf mode: ``redo_ocr`` replaces just the text
    layer and keeps the original page images (crucial for size: old CCITT
    bilevel scans stay tiny), while ``force_ocr`` rasterises every page.
    """
    import subprocess

    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no user input
            ["pdftotext", str(input_file), "-"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return len(result.stdout.strip()) >= 25


#: ocrmypdf writes one of these into the sidecar for every page it did *not*
#: OCR (``skip_text`` mode on a page that already had text). A sidecar that
#: contains nothing else means no recognition happened at all - the run must
#: not be allowed to overwrite the stored content with the placeholder.
OCR_SKIP_MARKER = "[OCR skipped on page"


def ocr_skipped_all(text: str) -> bool:
    """True when ``text`` consists only of ocrmypdf's skip placeholders.

    Mirrors paperless's own handling in ``parsers/tesseract.py``: a sidecar
    containing ``"[OCR skipped on page"`` is treated as incomplete and
    discarded. Here it is escalated to a hard stop, because the whole point of
    this tool is to *replace* old OCR - a run that recognised nothing is a
    configuration error, not a valid result.
    """
    stripped = "\n".join(
        line for line in text.splitlines() if OCR_SKIP_MARKER not in line
    ).strip()
    return not stripped


def _env_ocr_mode(settings: Settings) -> str:
    mode = settings.ocr_mode
    if mode not in ("auto", "force", "redo"):
        log.warning("Invalid REARCHIVE_OCR_MODE=%r; using 'auto'.", mode)
        return "auto"
    return mode


#: ocrmypdf accepts exactly one of these mode flags.
_MODE_FLAGS = {"force": "force_ocr", "redo": "redo_ocr", "skip_text": "skip_text"}


def select_ocr_strategy(ocr_mode: str, input_file: Path, mime_type: str) -> str:
    """Pick the ocrmypdf mode flag: ``force``, ``redo`` or ``skip_text``.

    ``redo`` swaps the invisible text layer for the new one and leaves the page
    images untouched, so the archive keeps its original size. ``force``
    rasterises every page - needed for text baked into the page content, but it
    re-renders the scan (measured: 616 KiB -> 4.6 MiB on a 72 dpi test scan).
    ``skip_text`` OCRs only the pages that carry no text layer at all.

    ``auto`` therefore means "replace the OCR text layer in place when the PDF
    has one, otherwise OCR the bare pages" - it never rasterises. Note that
    ocrmypdf rejects ``--redo-ocr`` combined with ``--deskew``; the caller
    drops deskew in that case (see :func:`build_ocrmypdf_args`).
    """
    if ocr_mode == "force":
        return "force"
    if ocr_mode == "redo":
        return "redo"
    if mime_type.startswith("application/pdf") and has_text_layer(input_file):
        return "redo"
    return "skip_text"


def build_ocrmypdf_args(
    input_file: Path,
    output_file: Path,
    sidecar_file: Path,
    provider: OcrProviderPlugin,
    settings: Settings,
    *,
    jobs: int = 2,
) -> dict[str, Any]:
    """Build the ocrmypdf.ocr() kwargs for one re-OCR run.

    Mode resolution (``REARCHIVE_OCR_MODE``):
    * ``auto`` (default): ``redo_ocr`` when the input already has a text layer
      (replaces it, keeps the page images -> archive size stays put),
      ``skip_text`` otherwise.
    * ``force``: always ``force_ocr`` - rasterises every page, so expect a much
      larger archive. Only worth it when the text cannot be redone.
    * ``redo``: always ``redo_ocr``.
    """
    mime_type = guess_mime_type(input_file)
    ocr_mode = _env_ocr_mode(settings)
    strategy = select_ocr_strategy(ocr_mode, input_file, mime_type)
    args: dict[str, Any] = {
        "input_file_or_options": input_file,
        "output_file": output_file,
        "use_threads": True,
        "jobs": max(1, jobs),
        "language": settings.ocr_language or "eng",
        "output_type": settings.ocr_output_type,
        "progress_bar": False,
        _MODE_FLAGS[strategy]: True,
        "plugins": [provider.ocrmypdf_plugin_module],
        **provider.ocrmypdf_kwargs(),
        **settings.ocr_user_args,  # e.g. invalidate_digital_signatures
    }

    if "pdfa" in settings.ocr_output_type:
        # Mirror paperless's default colour strategy for PDF/A.
        args.setdefault("color_conversion_strategy", "RGB")

    if settings.ocr_deskew:
        if strategy == "redo":
            # ocrmypdf rejects ``--redo-ocr`` together with ``--deskew``
            # ("not currently compatible with --deskew, --clean-final, and
            # --remove-background"). Redo keeps the original page images, so a
            # deskew pass would have to rasterise them anyway - dropping deskew
            # is what paperless itself does (parser.py: ``if deskew and mode !=
            # REDO``). Warn rather than fail: without this the run raises and the
            # safe-fallback below re-runs the whole document with force_ocr,
            # doubling GPU time and inflating the archive.
            args.pop("deskew", None)
            log.warning(
                "REARCHIVE_OCR_DESKEW ignored: ocrmypdf does not support deskew with redo_ocr "
                "(set REARCHIVE_OCR_MODE=force to deskew at the cost of a larger archive)."
            )
        else:
            args["deskew"] = True

    if settings.max_pages > 0:
        args["pages"] = f"1-{settings.max_pages}"
    else:
        args["sidecar"] = sidecar_file

    if is_image(mime_type):
        dpi = get_dpi(input_file)
        a4_dpi = calculate_a4_dpi(input_file)
        if dpi:
            args["image_dpi"] = dpi
        else:
            args["image_dpi"] = a4_dpi

    return args


def run_ocr(
    input_file: Path,
    output_file: Path,
    sidecar_file: Path,
    provider: OcrProviderPlugin,
    settings: Settings,
) -> None:
    """Run ocrmypdf once, with paperless-style safe fallback on failure."""
    import ocrmypdf

    args = build_ocrmypdf_args(
        input_file, output_file, sidecar_file, provider, settings, jobs=settings.concurrency
    )
    log_args = {
        k: ("***" if "api_key" in k else v) for k, v in args.items() if k != "plugins"
    }
    strategy = next(flag for flag in _MODE_FLAGS.values() if args.get(flag))
    log.info(
        "OCR strategy=%s (deskew=%s, output_type=%s)",
        strategy,
        bool(args.get("deskew")),
        args.get("output_type"),
    )
    log.debug("Calling OCRmyPDF with args: %s", log_args)
    try:
        ocrmypdf.ocr(**args)
        return
    except Exception as exc:  # noqa: BLE001 - mirror paperless's safe fallback
        log.warning("OCR failed (%s: %s); retrying with safe fallback args", type(exc).__name__, exc)

    fallback_args = build_ocrmypdf_args(
        input_file, output_file, sidecar_file, provider, settings, jobs=settings.concurrency
    )
    # Strip options known to break odd inputs. If redo_ocr was attempted
    # (e.g. the text layer turned out to be non-editable), fall back to
    # force_ocr, which handles any input.
    if fallback_args.pop("redo_ocr", False):
        fallback_args["force_ocr"] = True
        log.warning(
            "falling back to force_ocr: every page is re-rasterised, so the new archive can be "
            "several times larger than the old one."
        )
    for key in ("clean", "clean_final", "deskew", "rotate_pages", "rotate_pages_threshold"):
        fallback_args.pop(key, None)
    log.debug("Retrying OCRmyPDF with args: %s", fallback_args)
    try:
        ocrmypdf.ocr(**fallback_args)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"OCRmyPDF failed: {type(exc).__name__}: {exc}") from exc
