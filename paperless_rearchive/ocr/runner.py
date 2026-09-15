"""Django-free ocrmypdf argument builder.

Mirrors ``paperless_chandra.parser.construct_ocrmypdf_parameters`` for the
re-OCR use case: ``force_ocr`` (old OCR results are replaced), PDF/A output,
optional deskew, image-input DPI/alpha handling, and a safe fallback retry.
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
    mime, _ = mimetypes.guess_type(path.name)
    if mime is None:
        suffix = path.suffix.lower()
        if suffix in (".tif", ".tiff"):
            return "image/tiff"
        if suffix == ".jpg" or suffix == ".jpeg":
            return "image/jpeg"
        return "application/octet-stream"
    return mime


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

    A4_WIDTH_INCH = 8.27
    A4_HEIGHT_INCH = 11.69
    with Image.open(input_file) as img:
        width_dpi = img.width / A4_WIDTH_INCH
        height_dpi = img.height / A4_HEIGHT_INCH
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


def _env_ocr_mode(settings: "Settings") -> str:
    mode = settings.ocr_mode
    if mode not in ("auto", "force", "redo"):
        log.warning("Invalid REARCHIVE_OCR_MODE=%r; using 'auto'.", mode)
        return "auto"
    return mode


def build_ocrmypdf_args(
    input_file: Path,
    output_file: Path,
    sidecar_file: Path,
    provider: "OcrProviderPlugin",
    settings: "Settings",
    *,
    jobs: int = 2,
) -> dict[str, Any]:
    """Build the ocrmypdf.ocr() kwargs for one re-OCR run.

    Mode resolution (``REARCHIVE_OCR_MODE``):
    * ``auto`` (default): ``redo_ocr`` when the PDF already has a text layer
      (keeps original page images -> archive size stays near the old archive),
      ``force_ocr`` otherwise.
    * ``force``: always force_ocr (rasterises all pages).
    * ``redo``: always redo_ocr.
    """
    mime_type = guess_mime_type(input_file)
    ocr_mode = _env_ocr_mode(settings)
    redo = ocr_mode == "redo" or (
        ocr_mode == "auto" and mime_type.startswith("application/pdf") and has_text_layer(input_file)
    )
    args: dict[str, Any] = {
        "input_file_or_options": input_file,
        "output_file": output_file,
        "use_threads": True,
        "jobs": max(1, jobs),
        "language": settings.ocr_language or "eng",
        "output_type": settings.ocr_output_type,
        "progress_bar": False,
        "redo_ocr" if redo else "force_ocr": True,
        "plugins": [provider.ocrmypdf_plugin_module],
        **provider.ocrmypdf_kwargs(),
        **settings.ocr_user_args,  # e.g. invalidate_digital_signatures
    }

    if "pdfa" in settings.ocr_output_type:
        # Mirror paperless's default colour strategy for PDF/A.
        args.setdefault("color_conversion_strategy", "RGB")

    if settings.ocr_deskew:
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
    provider: "OcrProviderPlugin",
    settings: "Settings",
) -> None:
    """Run ocrmypdf once, with paperless-style safe fallback on failure."""
    import ocrmypdf

    args = build_ocrmypdf_args(
        input_file, output_file, sidecar_file, provider, settings, jobs=settings.concurrency
    )
    log_args = {
        k: ("***" if "api_key" in k else v) for k, v in args.items() if k != "plugins"
    }
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
    for key in ("clean", "clean_final", "deskew", "rotate_pages", "rotate_pages_threshold"):
        fallback_args.pop(key, None)
    try:
        ocrmypdf.ocr(**fallback_args)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"OCRmyPDF failed: {type(exc).__name__}: {exc}") from exc
