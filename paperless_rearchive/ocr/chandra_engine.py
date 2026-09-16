"""Unified Chandra OCR engine - produces markdown and optionally PDF/A.

This module provides a unified OCR pipeline that:
1. Renders PDF pages to images using PyMuPDF
2. Calls Chandra for OCR on each page
3. Optionally assembles a searchable PDF/A using ocrmypdf's sandwich pipeline
4. Tracks page-level errors for partial failure reporting

The key insight: OCR work is unified (same Chandra calls regardless of mode),
but PDF/A assembly only happens for re-ocr-all to avoid wasting CPU.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from paperless_chandra.engine import client as chandra_client
from paperless_chandra.engine.blocks import page_from_chunks, markdown_sidecar
from paperless_chandra.engine.hocr import render_hocr, sidecar_text

log = logging.getLogger(__name__)


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
    - hOCR structures (always, for potential PDF/A assembly)
    - Optional searchable PDF/A (when produce_pdf=True)

    The OCR work is the same regardless of produce_pdf; only the
    final PDF assembly step differs.

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
        """
        self.server_url = server_url
        self.model_name = model_name
        self.api_key = api_key
        self.content_format = content_format
        self.max_output_tokens = max_output_tokens
        self.language = language
        self.dpi = dpi

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

        log.info(
            "Unified Chandra OCR engine: %d pages at %d DPI",
            len(images),
            self.dpi,
        )

        # OCR each page with Chandra
        all_markdown_parts: list[str] = []
        all_hocr_pages: list[tuple[int, int, Any]] = []  # (width, height, hocr_data)
        error_pages: list[int] = []
        errors: list[str] = []

        for page_num, image in enumerate(images, start=1):
            try:
                markdown, hocr_data = self._ocr_page(image, page_num)
                if markdown.strip():
                    all_markdown_parts.append(markdown)
                if hocr_data:
                    all_hocr_pages.append((image.width, image.height, hocr_data))
            except Exception as e:
                log.warning("OCR failed on page %d: %s", page_num, e)
                error_pages.append(page_num)
                errors.append(f"Page {page_num}: {e}")

        # Combine all page markdown
        markdown = "\n\n".join(all_markdown_parts)

        # Optionally assemble PDF/A
        pdf_path_result: Path | None = None
        if produce_pdf and all_hocr_pages:
            pdf_path_result = self._assemble_pdf(
                pdf_path,
                output_pdf_path,
                all_hocr_pages,
            )

        return OcrResult(
            markdown=markdown,
            pdf_path=pdf_path_result,
            page_count=len(images),
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

    def _ocr_page(self, image: Any, page_num: int) -> tuple[str, Any]:
        """OCR a single page image with Chandra.

        Args:
            image: PIL Image of the page.
            page_num: 1-based page number (for logging).

        Returns:
            Tuple of (markdown, hocr_data).
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

        # Call Chandra for raw OCR output
        raw = chandra_client.ocr_image(image, options)
        if not raw:
            log.warning("Chandra returned empty result for page %d", page_num)
            return "", None

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

        # Get hOCR for potential PDF assembly
        hocr_data = render_hocr(page)

        return markdown, hocr_data

    def _assemble_pdf(
        self,
        original_pdf: Path,
        output_pdf: Path,
        hocr_pages: list[tuple[int, int, Any]],
    ) -> Path:
        """Assemble a searchable PDF/A using ocrmypdf's sandwich pipeline.

        Uses ocrmypdf to combine the original scan images with the
        hOCR text layers to produce a searchable PDF/A.

        Args:
            original_pdf: The original PDF (for page images).
            output_pdf: Where to write the output PDF/A.
            hocr_pages: List of (width, height, hocr_data) per page.

        Returns:
            Path to the generated PDF/A.
        """
        import ocrmypdf

        log.info(
            "Assembling searchable PDF/A via ocrmypdf sandwich: %s",
            output_pdf,
        )

        # We need to create a combined hOCR document
        # ocrmypdf expects a single hOCR file with all pages
        combined_hocr = self._combine_hocr_pages(hocr_pages)

        # Write combined hOCR to temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".hocr", delete=False) as f:
            f.write(combined_hocr)
            hocr_path = Path(f.name)

        try:
            # Use ocrmypdf to do the sandwich
            # ocrmypdf.ocr() with redo_ocr=True will use the hOCR sidecar
            ocrmypdf.ocr(
                str(original_pdf),  # input_file as positional arg
                output_file=str(output_pdf),
                output_type="pdfa",
                language=self.language,
                redo_ocr=True,
                hocr=str(hocr_path),
                jobs=1,
            )
        finally:
            hocr_path.unlink()

        log.info("PDF/A assembled: %s", output_pdf)
        return output_pdf

    def _combine_hocr_pages(
        self, hocr_pages: list[tuple[int, int, Any]]
    ) -> str:
        """Combine per-page hOCR into a single multi-page hOCR document.

        Args:
            hocr_pages: List of (width, height, hocr_data) per page.

        Returns:
            Combined hOCR XML string.
        """
        # Start the combined hOCR document
        combined = (
            "<?xml version='1.0' encoding='UTF-8'?>\n"
            "<!DOCTYPE html PUBLIC '-//W3C//DTD XHTML 1.0 Transitional//EN' "
            "'http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd'>\n"
            "<html xmlns='http://www.w3.org/1999/xhtml' xml:lang='en' lang='en'>\n"
            "<head><title></title></head>\n"
            "<body>\n"
        )

        # Add each page's hOCR content
        for i, (_, _, hocr_data) in enumerate(hocr_pages):
            # hocr_data is the hOCR XML string for this page
            combined += hocr_data + "\n"

        # Close the document
        combined += "</body></html>"

        return combined
