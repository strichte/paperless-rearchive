"""Unified Chandra OCR engine - produces markdown and optionally PDF/A.

This module provides a unified OCR pipeline that:
1. Renders PDF pages to images using PyMuPDF
2. Calls Chandra for OCR on each page
3. Optionally produces a searchable PDF/A via a single ocrmypdf pass driven by
   the paperless_chandra.ocrmypdf_plugin - the same engine paperless ingest
   uses - so the archive's text layer (and Creator metadata) is Chandra's,
   not Tesseract's
4. Tracks page-level errors for partial failure reporting

The key insight: OCR work is unified (same Chandra calls regardless of mode),
but PDF/A assembly only happens for re-ocr-all to avoid wasting CPU.
"""

from __future__ import annotations

import builtins
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from paperless_chandra.engine import client as chandra_client

try:
    from chandra.model.util import detect_repeat_token as _detect_repeat_token
except Exception:  # chandra extra not installed; post-hoc repeat check is skipped
    _detect_repeat_token = None

try:
    from chandra.settings import settings as _chandra_settings
except Exception:
    _chandra_settings = None
from paperless_chandra.engine.blocks import markdown_sidecar, page_from_chunks
from paperless_chandra.engine.hocr import sidecar_text

log = logging.getLogger(__name__)

# The ocrmypdf plugin that replaces ocrmypdf's default Tesseract engine with
# Chandra. Passing this (plus the chandra_* kwargs it registers via its
# add_options hook) is what makes the archive's text layer Chandra's.
# Mirrors paperless_chandra.parser._OCRMYPDF_PLUGIN_MODULE.
_OCRMYPDF_PLUGIN_MODULE = "paperless_chandra.ocrmypdf_plugin"


# Mirror of the upstream retry policy in chandra/model/vllm.py::generate_vllm.
# generate_vllm defaults: temperature=0.0, top_p=0.1, max_retries from
# chandra.settings.MAX_VLLM_RETRIES (default 6). Each retry N (1-based) uses
# temperature=min(base + 0.2*N, 0.8) and top_p=0.95, i.e. with base 0.0:
# attempt 1 -> 0.2, attempt 2 -> 0.4, attempt 3 -> 0.6, attempts 4-6 -> 0.8.
# Our engine does not override these, so the upstream defaults apply.
_BASE_TEMPERATURE = 0.0
_BASE_TOP_P = 0.1
_RETRY_TOP_P = 0.95
_RETRY_TEMP_STEP = 0.2
_RETRY_TEMP_CAP = 0.8
_FALLBACK_MAX_RETRIES = 6

# Upstream retry notices go to stdout via print(), not logging. Match them so
# we can re-log with the sampling params used for that attempt.
_RETRY_PRINT_RE = re.compile(
    r"Detected (repeat token|vllm error), retrying generation \(attempt (\d+)\)"
)
_interceptor_installed = False
_policy_logged = False


def _upstream_max_retries() -> int:
    """Effective max_retries used by generate_vllm when not passed explicitly."""
    try:
        if _chandra_settings is not None:
            return int(_chandra_settings.MAX_VLLM_RETRIES)
    except Exception:
        pass
    return _FALLBACK_MAX_RETRIES


def _retry_temperature(base: float, attempt: int) -> float:
    """Temperature upstream uses for 1-based retry attempt N."""
    return min(base + _RETRY_TEMP_STEP * attempt, _RETRY_TEMP_CAP)


def _install_retry_log_interceptor() -> None:
    """Route upstream's print() retry notices through logging with params.

    Idempotent; preserves the original stdout output. The attempt number in
    the message maps to retry_temperature(N) / top_p=0.95, since upstream
    computes retry_temperature the same way for both repeat-token and error
    retries.
    """
    global _interceptor_installed
    if _interceptor_installed:
        return
    _orig_print = builtins.print

    def _print(*args: Any, sep: str = " ", end: str = "\n", **kwargs: Any) -> None:
        try:
            msg = sep.join(str(a) for a in args)
        except Exception:
            _orig_print(*args, sep=sep, end=end, **kwargs)
            return
        m = _RETRY_PRINT_RE.search(msg)
        if m:
            kind, attempt = m.group(1), int(m.group(2))
            temp = _retry_temperature(_BASE_TEMPERATURE, attempt)
            log.warning(
                "Chandra detected %s - retry attempt %d/%d "
                "(temperature=%.1f, top_p=%.2f; base was temperature=%.1f, top_p=%.2f)",
                kind,
                attempt,
                _upstream_max_retries(),
                temp,
                _RETRY_TOP_P,
                _BASE_TEMPERATURE,
                _BASE_TOP_P,
            )
        _orig_print(*args, sep=sep, end=end, **kwargs)

    builtins.print = _print  # type: ignore[assignment]
    _interceptor_installed = True


def _final_output_has_repeat(raw: str) -> bool:
    """Same repeat test upstream uses to decide on a retry (vllm.py::_should_retry)."""
    if not raw or _detect_repeat_token is None:
        return False
    try:
        return bool(
            _detect_repeat_token(raw)
            or (len(raw) > 50 and _detect_repeat_token(raw, cut_from_end=50))
        )
    except Exception:
        return False


class OcrResult:
    """Result of OCR processing a document.

    Attributes:
        markdown: The OCR'd text content (markdown or plain text).
        pdf_path: Path to the generated PDF/A if produced, else None.
        page_count: Total number of pages processed.
        error_pages: List of page numbers (1-based) that failed OCR.
        errors: List of error messages for failed pages.
    """

    def __init__(
        self,
        markdown: str,
        pdf_path: Path | None = None,
        page_count: int = 0,
        error_pages: list[int] | None = None,
        errors: list[str] | None = None,
    ) -> None:
        self.markdown = markdown
        self.pdf_path = pdf_path
        self.page_count = page_count
        self.error_pages = error_pages or []
        self.errors = errors or []

    @property
    def has_errors(self) -> bool:
        """True if any pages failed OCR."""
        return len(self.error_pages) > 0

    @property
    def success_count(self) -> int:
        """Number of pages that succeeded."""
        return self.page_count - len(self.error_pages)


class ChandraOcrEngine:
    """Unified Chandra OCR engine.

    Renders PDF pages to images, calls Chandra for OCR, and produces:
    - Markdown content (always)
    - Optional searchable PDF/A with a Chandra text layer (when
      produce_pdf=True), via one ocrmypdf pass with the
      paperless_chandra.ocrmypdf_plugin

    This avoids wasting CPU cycles on PDF/A generation for content-only
    mode, where only the markdown is needed for the content field.
    """

    def __init__(
        self,
        server_url: str,
        model_name: str,
        api_key: str,
        content_format: str = "markdown",
        max_output_tokens: int = 12384,
        language: str = "eng",
        dpi: int = 300,
        concurrency: int = 1,
        max_pages: int = 0,
    ) -> None:
        """Initialize the Chandra OCR engine.

        Args:
            server_url: URL of the Chandra inference server.
            model_name: Name of the Chandra model to use.
            api_key: API key for the Chandra server.
            content_format: Either "markdown" or "text".
            max_output_tokens: Maximum tokens for OCR output.
            language: Language code for OCR.
            dpi: Resolution for rendering PDF pages to images.
            concurrency: Max pages OCR'd concurrently (ThreadPoolExecutor
                workers around the blocking Chandra HTTP call). Default 1
                (sequential). WARNING: this is designed for a local vision
                LLM where the GPU is the bottleneck - raising concurrency
                does not create more GPU, it only piles competing requests
                onto the same inference server. Expect higher latency per
                page, more VRAM pressure, and risk of timeouts/OOM under
                load. Raise gradually (2, then 4) and watch GPU
                utilisation, queue depth and error rate.
            max_pages: Cap on pages OCR'd per document. 0 (default) means
                all pages; otherwise only the first N pages are processed
                (page_count still reports the document's total pages).
        """
        self.server_url = server_url
        self.model_name = model_name
        self.api_key = api_key
        self.content_format = content_format
        self.max_output_tokens = max_output_tokens
        self.language = language
        self.dpi = dpi
        self.concurrency = max(1, int(concurrency or 1))
        self.max_pages = max(0, int(max_pages or 0))
        if self.concurrency > 1:
            log.warning(
                "Chandra concurrency=%d: pages are OCR'd in parallel against "
                "the same inference server. This is designed for a local "
                "vision LLM where the GPU is the bottleneck - it only piles "
                "competing requests onto the same GPU, so per-page latency "
                "may rise and timeouts/OOM become more likely under load.",
                self.concurrency,
            )
        self._max_retries = _upstream_max_retries()
        _install_retry_log_interceptor()
        global _policy_logged
        if not _policy_logged:
            log.info(
                "Chandra retry policy: max_retries=%d "
                "(chandra.settings.MAX_VLLM_RETRIES; override via MAX_VLLM_RETRIES env), "
                "base temperature=%.1f top_p=%.2f; retry attempt N uses "
                "temperature=min(base+0.2*N, 0.8) top_p=%.2f",
                self._max_retries,
                _BASE_TEMPERATURE,
                _BASE_TOP_P,
                _RETRY_TOP_P,
            )
            _policy_logged = True
        else:
            log.debug(
                "Chandra retry policy: max_retries=%d, base temperature=%.1f top_p=%.2f",
                self._max_retries,
                _BASE_TEMPERATURE,
                _BASE_TOP_P,
            )

    def ocr_document(
        self,
        pdf_path: Path,
        *,
        produce_pdf: bool = False,
        output_pdf_path: Path | None = None,
    ) -> OcrResult:
        """OCR a document using Chandra.

        Args:
            pdf_path: Path to the input PDF.
            produce_pdf: If True, also produce a searchable PDF/A.
            output_pdf_path: Where to write the PDF/A. Required if produce_pdf=True.

        Returns:
            OcrResult containing markdown, optionally PDF/A path, and error info.
        """
        if produce_pdf and output_pdf_path is None:
            raise ValueError("output_pdf_path is required when produce_pdf=True")

        # Render PDF pages to images
        images = self._render_pdf_pages(pdf_path)
        if not images:
            return OcrResult(
                markdown="",
                page_count=0,
                error_pages=[0],
                errors=["No pages could be rendered from PDF"],
            )

        total_pages = len(images)
        if self.max_pages > 0 and total_pages > self.max_pages:
            log.warning(
                "Document has %d pages, capping OCR at first %d "
                "(REARCHIVE_MAX_PAGES=%d); remaining %d page(s) skipped",
                total_pages,
                self.max_pages,
                self.max_pages,
                total_pages - self.max_pages,
            )
            images = images[: self.max_pages]

        log.info(
            "Unified Chandra OCR engine: %d page(s) at %d DPI%s",
            len(images),
            self.dpi,
            f" (concurrency={self.concurrency})" if self.concurrency > 1 else "",
        )

        # OCR each page with Chandra (sequential by default; workers are only
        # the HTTP wait around the blocking generate_vllm call - the GPU on
        # the inference server still processes them one at a time)
        all_markdown_parts: list[str] = [""] * len(images)
        error_pages: list[int] = []
        errors: list[str] = []
        doc_started = time.monotonic()

        def _run(page_num: int, image: Any) -> str:
            return self._ocr_page(image, page_num, len(images))

        indexed = list(enumerate(images, start=1))
        if self.concurrency > 1 and len(indexed) > 1:
            with ThreadPoolExecutor(
                max_workers=min(self.concurrency, len(indexed)),
                thread_name_prefix="chandra-page",
            ) as pool:
                futures = {pool.submit(_run, n, img): n for n, img in indexed}
                ordered: dict[int, str] = {}
                for future in as_completed(futures):
                    n = futures[future]
                    try:
                        ordered[n] = future.result()
                    except Exception as e:
                        log.warning("OCR failed on page %d: %s", n, e)
                        ordered[n] = ("", None)
            results = [ordered[n] for n, _ in indexed]
        else:
            results = []
            for n, img in indexed:
                try:
                    results.append(self._ocr_page(img, n, len(indexed)))
                except Exception as e:
                    log.warning("OCR failed on page %d: %s", n, e)
                    results.append(("", None))

        for page_num, markdown in enumerate(results, start=1):
            all_markdown_parts[page_num - 1] = markdown
            if not (markdown or "").strip():
                error_pages.append(page_num)
                errors.append(f"Page {page_num}: no OCR result")

        elapsed = time.monotonic() - doc_started
        ok = len(images) - len(error_pages)
        log.info(
            "Unified Chandra OCR done: %d/%d pages in %.1fs (%.1f pages/min)%s",
            ok,
            len(images),
            elapsed,
            (len(images) / elapsed * 60) if elapsed > 0 else 0.0,
            f" ({len(error_pages)} error(s))" if error_pages else "",
        )

        # Combine all page markdown
        markdown = "\n\n".join(all_markdown_parts)

        # Optionally produce the PDF/A: one ingest-identical ocrmypdf pass
        # with the paperless_chandra plugin gives the archive its Chandra
        # text layer. Skipped when every page failed (the pipeline refuses
        # to write a result whose pages all errored, so don't pay for a
        # second Chandra pass we would throw away).
        pdf_path_result: Path | None = None
        if produce_pdf and len(images) > len(error_pages):
            pdf_path_result = self._assemble_pdf(pdf_path, output_pdf_path)

        skipped = total_pages - len(images)
        skipped_pages = list(range(len(images) + 1, total_pages + 1))
        if skipped_pages:
            error_pages = sorted(set(error_pages) | set(skipped_pages))
            errors.append(
                f"Skipped {skipped} page(s) beyond REARCHIVE_MAX_PAGES={self.max_pages}"
            )

        return OcrResult(
            markdown=markdown,
            pdf_path=pdf_path_result,
            page_count=total_pages,
            error_pages=error_pages,
            errors=errors,
        )

    def _render_pdf_pages(self, pdf_path: Path) -> list[Any]:
        """Render PDF pages to PIL Images using PyMuPDF.

        Args:
            pdf_path: Path to the PDF file.

        Returns:
            List of PIL Images, one per page.
        """
        import fitz  # PyMuPDF

        images: list[Any] = []
        try:
            doc = fitz.open(pdf_path)
            for page_num in range(len(doc)):
                page = doc[page_num]
                # Check if page has content
                if not page.get_text().strip() and not page.get_images():
                    log.debug("Skipping blank page %d", page_num + 1)
                    continue

                # Render page to image at the configured DPI
                zoom = self.dpi / 72.0
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat)
                from PIL import Image

                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                images.append(img)
            doc.close()
        except Exception as e:
            log.error("Failed to render PDF pages: %s", e)
            raise

        return images

    def _ocr_page(self, image: Any, page_num: int, page_count: int = 0) -> str:
        """OCR a single page image with Chandra.

        Args:
            image: PIL Image of the page.
            page_num: 1-based page number (for logging).
            page_count: Total pages in this document (for logging context).

        Returns:
            The markdown sidecar text for the page.
        """
        from chandra.output import parse_chunks

        # Prepare options for Chandra client using SimpleNamespace
        options = SimpleNamespace(
            chandra_server_url=self.server_url,
            chandra_model_name=self.model_name,
            chandra_api_key=self.api_key,
            chandra_max_output_tokens=self.max_output_tokens,
            chandra_content_format=self.content_format,
            languages=[self.language],
        )

        log.debug(
            "Page %d: Chandra request (temperature=%.1f, top_p=%.2f, "
            "max_tokens=%d, max_retries=%d)",
            page_num,
            _BASE_TEMPERATURE,
            _BASE_TOP_P,
            self.max_output_tokens,
            self._max_retries,
        )
        started = time.monotonic()
        # Call Chandra for raw OCR output
        raw = chandra_client.ocr_image(image, options)
        elapsed = time.monotonic() - started
        log.debug(
            "Page %d: Chandra returned %d chars in %.1fs",
            page_num,
            len(raw or ""),
            elapsed,
        )
        if raw and _final_output_has_repeat(raw):
            log.warning(
                "Page %d: final OCR output (%d chars) still contains a "
                "repeat loop after all %d retries - likely failed scan",
                page_num,
                len(raw),
                self._max_retries,
            )
        if not raw:
            log.warning("Chandra returned empty result for page %d", page_num)
            return ""

        # Parse chunks and build page structure
        chunks = parse_chunks(raw, image)
        page = page_from_chunks(
            chunks,
            image.width,
            image.height,
            self.language,
        )

        # Get markdown sidecar
        if self.content_format == "markdown" and raw:
            markdown = markdown_sidecar(raw)
        else:
            markdown = sidecar_text(page)

        return markdown

    def _assemble_pdf(self, original_pdf: Path, output_pdf: Path) -> Path:
        """Produce a searchable PDF/A with a Chandra text layer.

        Drives the same ocrmypdf invocation paperless ingest drives
        (paperless_chandra.parser.construct_ocrmypdf_parameters): the
        paperless_chandra.ocrmypdf_plugin replaces ocrmypdf's default
        Tesseract engine, so the PDF's invisible text layer - and the
        ``Creator`` metadata ocrmypdf records - are Chandra's.

        ocrmypdf 17.x has no hOCR renderer: an ``hocr=`` kwarg would be
        silently swallowed by **kwargs and the text layer would fall back
        to Tesseract (the pre-P1 bug; see doc/OCR_STRATEGY.md). The Chandra
        pass therefore happens *inside* ocrmypdf, via the plugin - the
        per-page hOCR this module used to render is gone entirely.

        ``redo_ocr`` keeps the original page images (no re-rasterising, so
        the archive does not balloon) and strips any existing text layer -
        the natural mode for a re-OCR tool. ``use_threads=True`` mirrors
        paperless (required for daemonised callers) and keeps everything
        in-process.

        Args:
            original_pdf: The original PDF (for page images).
            output_pdf: Where to write the output PDF/A.

        Returns:
            Path to the generated PDF/A.
        """
        import ocrmypdf

        log.info(
            "Assembling searchable PDF/A via ocrmypdf + Chandra plugin: %s",
            output_pdf,
        )
        ocrmypdf.ocr(
            str(original_pdf),  # input_file as positional arg
            output_file=str(output_pdf),
            output_type="pdfa",
            # Mirror paperless's default colour strategy for PDF/A.
            color_conversion_strategy="RGB",
            language=self.language,
            redo_ocr=True,
            use_threads=True,
            jobs=1,
            progress_bar=False,
            plugins=[_OCRMYPDF_PLUGIN_MODULE],
            # Custom kwargs registered by the plugin's add_options hookimpl.
            chandra_server_url=self.server_url,
            chandra_model_name=self.model_name,
            chandra_api_key=self.api_key,
            chandra_max_output_tokens=self.max_output_tokens,
            chandra_content_format=self.content_format,
        )
        log.info("PDF/A assembled: %s", output_pdf)
        return output_pdf
