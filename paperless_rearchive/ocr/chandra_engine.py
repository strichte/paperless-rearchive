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
import contextlib
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from paperless_chandra.engine import client as chandra_client
from paperless_chandra.engine.client import ChandraClientError

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

from paperless_rearchive.ocr.ingest_args import post_process_text
from paperless_rearchive.ocr.model_check import cached_models_for, ensure_model_served
from paperless_rearchive.ocr.server_check import ensure_server_reachable

log = logging.getLogger(__name__)

# Token capture: the upstream client returns only the raw string, dropping the
# per-page ``GenerationResult.token_count`` (completion tokens). Wrapping
# ``generate_vllm`` at the ``paperless_chandra.engine.client`` reference lets
# this engine recover the count without forking the client: every result list
# produced inside a page window contributes its token sum to that page. The
# stack is thread-local, so concurrent pages (each on its own worker thread,
# retries included) accumulate independently; FIFO/LIFO popping is avoided
# entirely - each thread only ever touches its own window.
_TOKEN_WINDOW = threading.local()


def _token_stack() -> list[list[int]]:
    stack = getattr(_TOKEN_WINDOW, "stack", None)
    if stack is None:
        stack = []
        _TOKEN_WINDOW.stack = stack
    return stack


_wrapped_generate_vllm = False


def _install_token_tap() -> None:
    """Wrap upstream generate_vllm once so token counts reach page windows.

    Patches the ``generate_vllm`` reference held by
    ``paperless_chandra.engine.client`` (bound at its import time, so patching
    ``chandra.model.vllm`` alone would miss). Idempotent; the wrapper delegates
    fully and only records ``sum(r.token_count)`` into the current thread's
    innermost window, if any.
    """

    global _wrapped_generate_vllm
    if _wrapped_generate_vllm:
        return
    try:
        original = chandra_client.generate_vllm
    except AttributeError:
        return

    def _recording(*args: Any, **kwargs: Any) -> Any:
        results = original(*args, **kwargs)
        try:
            total = sum(int(getattr(r, "token_count", 0) or 0) for r in results or [])
        except Exception:
            total = 0
        stack = getattr(_TOKEN_WINDOW, "stack", None)
        if stack and stack[-1] is not None:
            with contextlib.suppress(IndexError):
                stack[-1].append(total)
        return results

    try:
        chandra_client.generate_vllm = _recording  # type: ignore[attr-defined]
    except Exception:
        return
    _wrapped_generate_vllm = True

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

# Upstream reports each failed request with a bare print() too (see
# chandra/model/vllm.py::generate_vllm). The message omits the model name, so
# match it here and re-log with the model - plus the server's advertised
# models when the /models preflight already fetched them.
_VLLM_ERROR_PRINT_RE = re.compile(r"Error during VLLM generation: (?P<detail>.*)", re.DOTALL)
_interceptor_installed = False
_policy_logged = False

# The ocrmypdf plugin's preflight (paperless_chandra.ocrmypdf_plugin::
# _probe_server) raises MissingDependencyError for exactly two conditions:
# an unreachable server (URLError underneath) and a rejected API key.
# A rejected key is a configuration error (the safe-fallback retry with
# different ocrmypdf args cannot help either, but it is not an outage);
# only the unreachable-server variant is matched here.
_MISSING_DEPENDENCY_RE = re.compile(r"Chandra server at (?P<url>\S+) is not reachable \(")


def _is_missing_dependency(exc: BaseException) -> bool:
    """True when the ocrmypdf plugin probe failed on an unreachable server.

    The plugin raises :class:`ocrmypdf.exceptions.MissingDependencyError`
    (an ``OcrmypdfError``, deliberately *not* a ``ChandraClientError``) from
    its ``check_options`` hook before any page runs, so the message is the
    only stable discriminator.
    """
    return _MISSING_DEPENDENCY_RE.search(str(exc)) is not None


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


def _current_model_name() -> str:
    """Model upstream applied to the last request (read from its settings)."""
    try:
        if _chandra_settings is not None:
            return str(_chandra_settings.VLLM_MODEL_NAME or "")
    except Exception:
        pass
    return ""


def _install_retry_log_interceptor() -> None:
    """Route upstream's print() retry/error notices through logging.

    Idempotent; preserves the original stdout output. The attempt number in
    the retry message maps to retry_temperature(N) / top_p=0.95, since upstream
    computes retry_temperature the same way for both repeat-token and error
    retries. Generation failures are re-logged with the configured model name
    (and the server's advertised models when known), which upstream's bare
    ``Error during VLLM generation: ...`` print omits.
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
        error = _VLLM_ERROR_PRINT_RE.search(msg)
        if error:
            model = _current_model_name()
            available = cached_models_for(model) if model else None
            hint = f" Server advertises: {', '.join(sorted(available))}." if available else ""
            log.error(
                "Chandra generation failed for model %r: %s%s",
                model or "<unset>",
                error.group("detail").strip(),
                hint,
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


class PageStat:
    """Per-page OCR timing + token usage for one page sent to Chandra."""

    def __init__(self, page_num: int, seconds: float, tokens: int | None = None) -> None:
        self.page_num = page_num
        self.seconds = seconds
        self.tokens = tokens


class OcrResult:
    """Result of OCR processing a document.

    Attributes:
        markdown: The OCR'd text content (markdown or plain text).
        pdf_path: Path to the generated PDF/A if produced, else None.
        page_count: Total number of pages processed.
        error_pages: List of page numbers (1-based) that failed OCR.
        errors: List of error messages for failed pages.
        page_stats: Per-page inference stats (one entry per page actually
            sent to Chandra; native/passthrough/skipped pages have none).
        inference_seconds: Sum of per-page Chandra call time (pure
            inference + HTTP wait + upstream retries, no render/assembly).
        elapsed_seconds: Wall time of the whole engine call (render +
            inference + optional ocrmypdf assembly).
    """

    def __init__(
        self,
        markdown: str,
        pdf_path: Path | None = None,
        page_count: int = 0,
        error_pages: list[int] | None = None,
        errors: list[str] | None = None,
        page_actions: dict[int, str] | None = None,
        page_stats: list[PageStat] | None = None,
        inference_seconds: float = 0.0,
        elapsed_seconds: float = 0.0,
    ) -> None:
        self.markdown = markdown
        self.pdf_path = pdf_path
        self.page_count = page_count
        self.error_pages = error_pages or []
        self.errors = errors or []
        #: Per-page outcome, 1-based page -> action: ``ocr`` (sent to the OCR
        #: engine), ``native`` (kept its original text layer / born-digital
        #: text), ``passthrough`` (copied through the archive pass untouched),
        #: ``skipped`` (beyond the page cap), or ``error``. Pages absent from
        #: the map have no distinct action.
        self.page_actions: dict[int, str] = page_actions or {}
        #: One PageStat per page actually sent to Chandra (error pages with an
        #: empty upstream result included when the call itself completed).
        self.page_stats: list[PageStat] = list(page_stats or [])
        #: Sum of page_stats seconds (concurrency>1 overlaps, so this may
        #: exceed wall time).
        self.inference_seconds: float = inference_seconds
        #: Whole engine-call wall time (render + inference + assembly).
        self.elapsed_seconds: float = elapsed_seconds

    @property
    def total_tokens(self) -> int:
        """Sum of completion tokens over pages that reported a count."""
        return sum(s.tokens for s in self.page_stats if s.tokens is not None)

    @property
    def has_token_counts(self) -> bool:
        """True when at least one OCR'd page reported a token count."""
        return any(s.tokens is not None for s in self.page_stats)

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
        _install_token_tap()
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
        settings: Any = None,
        provenance: Any = None,
        force: bool = False,
    ) -> OcrResult:
        """OCR a document using Chandra.

        Args:
            pdf_path: Path to the input PDF.
            produce_pdf: If True, also produce a searchable PDF/A.
            output_pdf_path: Where to write the PDF/A. Required if produce_pdf=True.
            settings: Optional rearchive Settings. When given (and
                produce_pdf=True), a single ingest-identical ocrmypdf pass
                produces both the PDF/A and the markdown sidecar content
                (paperless parity; see doc/OCR_STRATEGY.md P2/P3). Without
                it, the legacy two-pass flow (per-page Chandra for content,
                plugin assembly for the archive) is used.
            provenance: Optional :class:`~paperless_rearchive.ocr.provenance.PdfProvenance`
                verdict for the original. When given, only the pages flagged
                ``pages_needing_ocr`` are sent to Chandra and native pages
                keep pdf-inspector's own markdown (content-only path), and an
                archive run derives its effective ocrmypdf mode from the
                verdict instead of the legacy document-level heuristics.
            force: Bypass the provenance verdict and OCR every page (the
                ``re-ocr-force`` modifier tag). Explicit operator override.

        Returns:
            OcrResult containing markdown, optionally PDF/A path, and error info.
        """
        if produce_pdf and output_pdf_path is None:
            raise ValueError("output_pdf_path is required when produce_pdf=True")

        # Fail fast on a model the server does not serve. The upstream retry
        # ladder would otherwise re-send the same 404 once per page
        # (MAX_VLLM_RETRIES attempts, ~40 s at the default six) before the
        # error surfaces - and without naming the offending model. Cached once
        # per (url, model) per process, so this is one probe, not one per page.
        ensure_model_served(self.server_url, self.model_name, self.api_key)

        # Fail fast when the inference server cannot be reached at all. The
        # plugin's own check_options probe reports the outage one ocrmypdf
        # validation step later; this preflight makes the failure a
        # ChandraClientError subclass the poller aborts on without recording
        # per-document failures - the outage equivalent of the model preflight.
        ensure_server_reachable(self.server_url, self.api_key)

        if produce_pdf and settings is not None:
            return self._ocr_document_ingest_pass(
                pdf_path, output_pdf_path, settings, provenance=provenance, force=force
            )

        if provenance is not None and not produce_pdf:
            return self._ocr_document_pages(pdf_path, provenance, force=force)

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
        # One stats bucket per page sent to Chandra. Concurrency>1 overlaps,
        # so a shared counter would race - index by page under a lock.
        stats: dict[int, PageStat] = {}
        stats_lock = threading.Lock()
        doc_started = time.monotonic()

        def _run(page_num: int, image: Any) -> str:
            started = time.monotonic()
            _token_stack().append([])
            try:
                return self._ocr_page(image, page_num, len(images))
            finally:
                elapsed_s = time.monotonic() - started
                bucket = _token_stack().pop()
                tokens = sum(bucket) if bucket else None
                with stats_lock:
                    stats[page_num] = PageStat(page_num, elapsed_s, tokens)

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
                    except ChandraClientError:
                        # Server-side/transient failure (the client already
                        # exhausted its retries): abort the whole document so
                        # the poller keeps the trigger tag and retries - the
                        # same semantics the archive path gets from ocrmypdf.
                        raise
                    except Exception as e:
                        log.warning("OCR failed on page %d: %s", n, e)
                        ordered[n] = ""
            results = [ordered[n] for n, _ in indexed]
        else:
            results = []
            for n, img in indexed:
                try:
                    results.append(_run(n, img))
                except ChandraClientError:
                    # See the concurrent branch above: transient server
                    # failures abort the document instead of becoming
                    # per-page errors.
                    raise
                except Exception as e:
                    log.warning("OCR failed on page %d: %s", n, e)
                    results.append("")

        for page_num, markdown in enumerate(results, start=1):
            all_markdown_parts[page_num - 1] = markdown
            if not (markdown or "").strip():
                error_pages.append(page_num)
                errors.append(f"Page {page_num}: no OCR result")

        elapsed = time.monotonic() - doc_started
        ok = len(images) - len(error_pages)
        page_stats = [stats[n] for n in sorted(stats)]
        inference_s = sum(s.seconds for s in page_stats)
        total_tokens = sum(s.tokens for s in page_stats if s.tokens is not None)
        known_tokens = sum(1 for s in page_stats if s.tokens is not None)
        for s in page_stats:
            log.debug(
                "Page %d/%d: %.1fs inference, %s tokens",
                s.page_num,
                len(images),
                s.seconds,
                f"{s.tokens}" if s.tokens is not None else "n/a",
            )
        log.info(
            "Unified Chandra OCR done: %d/%d pages in %.1fs (%.1f pages/min)%s",
            ok,
            len(images),
            elapsed,
            (len(images) / elapsed * 60) if elapsed > 0 else 0.0,
            f" ({len(error_pages)} error(s))" if error_pages else "",
        )
        log.info(
            "OCR stats: %d token(s)%s over %d OCR'd page(s), "
            "%.1fs inference (%.1fs/page), %.1fs total (%.1fs overhead)",
            total_tokens,
            "" if known_tokens == len(page_stats) else f" ({known_tokens}/{len(page_stats)} pages reported)",
            len(page_stats),
            inference_s,
            (inference_s / len(page_stats)) if page_stats else 0.0,
            elapsed,
            max(elapsed - inference_s, 0.0),
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
            page_stats=page_stats,
            inference_seconds=inference_s,
            elapsed_seconds=elapsed,
            page_actions={
                **{p: "ocr" for p in range(1, len(images) + 1)},
                **{p: "error" for p in error_pages if p <= len(images)},
                **{p: "skipped" for p in skipped_pages},
            },
        )

    def _stamp_model_provenance(self, pdf_path: Path) -> None:
        """Append the served model name to the PDF's Creator metadata.

        ocrmypdf records the engine/library versions ('OCRmyPDF … / … +
        Chandra 0.2.0' via ``ChandraEngine.creator_tag``), but the *served
        model* is a deployment choice (PAPERLESS_CHANDRA_MODEL_NAME) — so it
        is appended here, making the archive itself carry the full provenance
        of the text layer. docinfo ``/Creator`` and XMP ``xmp:CreatorTool``
        are kept in sync through pikepdf's metadata API (PDF/A-safe).
        """
        import pikepdf

        model = (self.model_name or "").strip()
        if not model:
            return
        with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
            creator = str(pdf.docinfo.get("/Creator", "") or "").strip()
            stamped = f"{creator} [model: {model}]".strip()
            with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=True) as meta:
                meta["xmp:CreatorTool"] = stamped
            pdf.docinfo["/Creator"] = stamped
            # pikepdf's context manager does NOT auto-save; without an
            # explicit save the metadata changes are discarded on close.
            pdf.save(pdf_path)
        log.info("Stamped model provenance into %s: %s", pdf_path.name, stamped)

    def _resolve_ingest_mode(
        self,
        settings: Any,
        pdf_path: Path,
        provenance: Any,
        force: bool,
    ) -> str:
        """Choose the ocrmypdf mode for an archive run (layer 2).

        With a provenance verdict the mode is derived from the per-page
        classification; without one (detection disabled, ``unknown``, or
        non-PDF input) the legacy document-level heuristics apply, so
        behaviour is unchanged.
        """
        from paperless_rearchive.ocr.ingest_args import (
            effective_mode,
            has_visible_text_content,
            pdf_born_digital_text,
            resolve_mode,
        )

        if force:
            log.warning("forced OCR requested; ignoring the provenance verdict")
            return "force"
        kind = getattr(provenance, "kind", None)
        if kind in ("text_based", "scanned", "mixed"):
            return effective_mode(
                settings.ocr_mode,
                kind,
                mixed_mode=settings.ocr_mixed_mode,
            )
        return resolve_mode(
            settings.ocr_mode,
            pdf_has_text=pdf_born_digital_text(pdf_path),
            pdf_is_digital_born=has_visible_text_content(pdf_path),
        )

    def _ocr_document_ingest_pass(
        self,
        pdf_path: Path,
        output_pdf_path: Path,
        settings: Any,
        *,
        provenance: Any = None,
        force: bool = False,
    ) -> OcrResult:
        """Single ingest-identical ocrmypdf pass: PDF/A + markdown sidecar.

        Replaces the previous two-pass flow (per-page Chandra for content,
        then ocrmypdf for the archive). One ocrmypdf pass with the
        paperless_chandra plugin produces the PDF/A (Chandra text layer)
        *and* the markdown sidecar the content field is taken from -
        exactly what ingest produces, so archive and content can no longer
        disagree.

        Consequence (D4 redefinition, doc/OCR_STRATEGY.md): page-level
        partial-failure tracking does not apply to archive-mode runs. A
        Chandra page failure aborts the pass; the document-level safe
        fallback (force_ocr retry, clean/deskew kept per settings) mirrors
        ingest. ``re-ocr-page-errors`` remains meaningful only for
        content-only runs.
        """
        import fitz  # PyMuPDF
        import ocrmypdf

        from paperless_rearchive.ocr.ingest_args import (
            build_ocrmypdf_args,
            extract_pdf_text,
            post_process_text,
            sidecar_content,
        )

        started = time.monotonic()
        doc = fitz.open(pdf_path)
        try:
            total_pages = len(doc)
        finally:
            doc.close()

        effective_pages = self.max_pages
        if effective_pages > 0 and effective_pages > total_pages:
            effective_pages = total_pages

        resolved_mode = self._resolve_ingest_mode(settings, pdf_path, provenance, force)

        # Mixed provenance: ocrmypdf's skip_text cannot express "OCR only the
        # textless pages" - it skips *every* page carrying a text layer,
        # including scans with a stale OCR layer, so the pass becomes a no-op
        # (observed on document 5830: all 3 pages skipped, no OCR at all).
        # Instead, restrict OCR to exactly the pages the provenance verdict
        # marked as needing OCR (--pages); native pages pass through
        # unmodified. Pages in the range run in redo mode so a stale text
        # layer is replaced rather than causing PriorOcrFoundError.
        mixed_pages: str | None = None
        mixed_needed: list[int] = []
        if (
            resolved_mode == "skip"
            and getattr(provenance, "kind", None) == "mixed"
        ):
            cap = self.max_pages
            mixed_needed = [
                p
                for p in sorted(provenance.pages_needing_ocr)
                if cap <= 0 or p <= cap
            ]
            if mixed_needed:
                mixed_pages = ",".join(str(p) for p in mixed_needed)
                log.info(
                    "mixed provenance: OCR limited to page(s) %s (--pages, "
                    "redo); native pages pass through unmodified",
                    mixed_pages,
                )

        sidecar = output_pdf_path.parent / "archive-sidecar.txt"

        def _build(*, safe_fallback: bool) -> dict[str, Any]:
            return build_ocrmypdf_args(
                input_file=pdf_path,
                output_file=output_pdf_path,
                sidecar_file=sidecar,
                language=self.language,
                mode="redo" if mixed_pages else resolved_mode,
                clean=settings.ocr_clean,
                deskew=settings.ocr_deskew,
                rotate=settings.ocr_rotate,
                rotate_threshold=settings.ocr_rotate_threshold,
                output_type=settings.ocr_output_type,
                jobs=self.concurrency,
                max_pages=effective_pages,
                pages=mixed_pages,
                user_args=settings.ocr_user_args,
                chandra_server_url=self.server_url,
                chandra_model_name=self.model_name,
                chandra_api_key=self.api_key,
                chandra_max_output_tokens=self.max_output_tokens,
                chandra_content_format=self.content_format,
                safe_fallback=safe_fallback,
            )

        args = _build(safe_fallback=False)
        flag = next(
            (f for f in ("force_ocr", "redo_ocr", "skip_text") if args.get(f)),
            "default",
        )
        log.info(
            "Ingest-parity OCR pass: %d page(s), mode=%s (%s), clean=%s, deskew=%s, "
            "rotate=%s, output_type=%s, jobs=%d%s",
            total_pages,
            "redo" if mixed_pages else resolved_mode,
            flag,
            settings.ocr_clean,
            bool(args.get("deskew")),
            bool(args.get("rotate_pages")),
            args.get("output_type"),
            args.get("jobs"),
            f", pages={mixed_pages}" if mixed_pages else "",
        )
        log.debug(
            "ocrmypdf args: %s",
            {k: ("***" if "api_key" in k else v) for k, v in args.items() if k != "plugins"},
        )
        try:
            ocrmypdf.ocr(**args)
        except Exception as exc:  # noqa: BLE001 - mirror paperless's safe fallback
            # The safe fallback changes OCRmyPDF *arguments* (force_ocr,
            # clean/deskew). An unreachable inference server fails both passes
            # identically - retrying only duplicates the same
            # ConnectionRefusedError (observed 2026-09-22, doc 3697).
            if _is_missing_dependency(exc):
                raise
            log.warning(
                "OCR failed (%s: %s); retrying with safe fallback (force_ocr, "
                "clean/deskew per settings)",
                type(exc).__name__,
                exc,
            )
            args = _build(safe_fallback=True)
            log.debug("Retrying OCRmyPDF with args: %s", args)
            try:
                ocrmypdf.ocr(**args)
            except Exception as exc2:  # noqa: BLE001
                raise RuntimeError(
                    f"OCRmyPDF failed: {type(exc2).__name__}: {exc2}"
                ) from exc2

        # Bake the served model name into the archive itself (Creator/XMP),
        # next to the engine + library versions ocrmypdf already recorded.
        self._stamp_model_provenance(output_pdf_path)

        # Per-page action map for the final pipeline log: in a mixed --pages
        # run the listed pages get fresh Chandra layers and the rest pass
        # through untouched; otherwise the resolved mode applies per page.
        if mixed_pages:
            page_actions: dict[int, str] = {p: "ocr" for p in mixed_needed}
            for p in range(1, total_pages + 1):
                page_actions.setdefault(p, "passthrough")
        elif resolved_mode == "off":
            page_actions = {p: "passthrough" for p in range(1, total_pages + 1)}
        else:
            page_actions = {p: "ocr" for p in range(1, total_pages + 1)}

        if mixed_pages:
            # Mixed provenance, --pages run: no sidecar (mutually exclusive
            # with ``pages``). Content = Chandra markdown for OCR'd pages +
            # pdftotext for native pages - the same per-page composition the
            # provenance-driven content pass produces.
            content = self._mixed_content(pdf_path, output_pdf_path, mixed_needed)
        elif sidecar.exists():
            content = sidecar_content(sidecar, output_pdf_path)
        else:
            # pages cap set: ocrmypdf got ``pages=1-N`` instead of a sidecar
            # (they are mutually exclusive, as at ingest). Content comes from
            # the produced PDF's text layer instead of markdown.
            log.warning(
                "pages cap active (%d); no sidecar - content taken from pdftotext "
                "of the produced PDF (plain text, not markdown)",
                effective_pages,
            )
            content = post_process_text(extract_pdf_text(output_pdf_path)) or ""

        # Guard against the silent no-op seen on document 5830: ocrmypdf
        # skipping *every* page (e.g. skip_text over a fully-text-bearing
        # PDF) would otherwise surface as a successful run.
        skipped_all = (
            sidecar.exists()
            and not mixed_pages
            and sidecar.read_text(encoding="utf-8", errors="replace").count(
                "[OCR skipped on page"
            )
            >= total_pages
        )
        if skipped_all:
            log.warning(
                "OCR skipped on all %d page(s); the pass made no changes - "
                "check the resolved mode vs the document's text layers",
                total_pages,
            )

        elapsed = time.monotonic() - started
        log.info(
            "Ingest-parity OCR done: %d page(s) in %.1fs (%.1f pages/min), "
            "content %d chars%s",
            total_pages,
            elapsed,
            (total_pages / elapsed * 60) if elapsed > 0 else 0.0,
            len(content),
            f", pages={mixed_pages}" if mixed_pages else "",
        )
        # The ocrmypdf pass runs Chandra inside worker processes (jobs>1),
        # so per-page token counts are not visible here: report n/a rather
        # than a misleading zero.
        log.info(
            "OCR stats: n/a token(s) over %d OCR'd page(s), "
            "%.1fs inference (n/a/page), %.1fs total",
            sum(1 for a in page_actions.values() if a == "ocr"),
            elapsed,
        )
        return OcrResult(
            markdown=content,
            pdf_path=output_pdf_path,
            page_count=total_pages,
            page_actions=page_actions,
            inference_seconds=0.0,
            elapsed_seconds=elapsed,
        )

    def _ocr_document_pages(
        self,
        pdf_path: Path,
        provenance: Any,
        *,
        force: bool = False,
    ) -> OcrResult:
        """Content-only OCR driven by a per-page provenance verdict.

        Only pages in ``provenance.pages_needing_ocr`` (or every page under
        ``force``) are sent to Chandra; native pages get their text via
        ``pdftotext`` (same as paperless-ngx ingest), falling back to
        pdf-inspector's markdown only when pdftotext finds nothing, so
        born-digital pages are never handed to the LLM.
        """
        import fitz  # PyMuPDF

        doc = fitz.open(pdf_path)
        parts: list[str] = [""] * len(doc)
        error_pages: list[int] = []
        errors: list[str] = []
        page_stats: list[PageStat] = []
        pages_started = time.monotonic()
        try:
            total_pages = len(doc)
            cap = self.max_pages
            needed = (
                set(range(1, total_pages + 1))
                if force
                else set(provenance.pages_needing_ocr)
            )
            skipped = sorted(p for p in needed if cap > 0 and p > cap)
            needed -= set(skipped)

            log.info(
                "Provenance-driven content OCR: %d page(s) total, %d to OCR, "
                "%d native%s, dpi=%d",
                total_pages,
                len(needed),
                total_pages - len(needed),
                " (forced)" if force else "",
                self.dpi,
            )

            native_count = 0
            for page_num in range(1, total_pages + 1):
                page = doc[page_num - 1]
                if page_num not in needed:
                    # Born-digital page: extract with pdftotext (same as
                    # paperless-ngx ingest), split by form feed for per-page
                    # text, so native pages keep text close to the original
                    # content rather than PyMuPDF's layout extraction.
                    text = self._pdftotext_page_text(pdf_path, page_num)
                    if not text.strip():
                        # Fall back to pdf-inspector's markdown if pdftotext found nothing.
                        text = provenance.native_markdown.get(page_num, "")
                    if text.strip():
                        native_count += 1
                    parts[page_num - 1] = text
                    continue

                image = self._render_page_image(page)
                if image is None:
                    log.debug("Page %d: blank, nothing to OCR", page_num)
                    continue
                page_started = time.monotonic()
                _token_stack().append([])
                try:
                    markdown = self._ocr_page(image, page_num, total_pages)
                except ChandraClientError:
                    # Transient server failure: abort so the poller retries.
                    raise
                except Exception as e:  # noqa: BLE001 - page-level failure
                    log.warning("OCR failed on page %d: %s", page_num, e)
                    markdown = ""
                finally:
                    bucket = _token_stack().pop()
                    page_stats.append(
                        PageStat(
                            page_num,
                            time.monotonic() - page_started,
                            sum(bucket) if bucket else None,
                        )
                    )
                if (markdown or "").strip():
                    parts[page_num - 1] = markdown
                else:
                    error_pages.append(page_num)
                    errors.append(f"Page {page_num}: no OCR result")

            if skipped:
                error_pages = sorted(set(error_pages) | set(skipped))
                errors.append(
                    f"Skipped {len(skipped)} page(s) beyond REARCHIVE_MAX_PAGES={cap}"
                )

            log.info(
                "Provenance-driven content OCR done: %d page(s) sent to Chandra, "
                "%d kept as native text (pdftotext), %d error(s)",
                len(needed),
                native_count,
                len(error_pages),
            )
            pages_elapsed = time.monotonic() - pages_started
            inference_s = sum(s.seconds for s in page_stats)
            total_tokens = sum(s.tokens for s in page_stats if s.tokens is not None)
            known = sum(1 for s in page_stats if s.tokens is not None)
            for s in sorted(page_stats, key=lambda s: s.page_num):
                log.debug(
                    "Page %d/%d: %.1fs inference, %s tokens",
                    s.page_num,
                    total_pages,
                    s.seconds,
                    f"{s.tokens}" if s.tokens is not None else "n/a",
                )
            log.info(
                "OCR stats: %d token(s)%s over %d OCR'd page(s), "
                "%.1fs inference (%.1fs/page), %.1fs total (%.1fs overhead)",
                total_tokens,
                "" if known == len(page_stats) else f" ({known}/{len(page_stats)} pages reported)",
                len(page_stats),
                inference_s,
                (inference_s / len(page_stats)) if page_stats else 0.0,
                pages_elapsed,
                max(pages_elapsed - inference_s, 0.0),
            )
        finally:
            doc.close()

        # Per-page action map for the final pipeline log.
        page_actions = {p: "ocr" for p in needed}
        for p in range(1, total_pages + 1):
            if p not in needed:
                page_actions[p] = "native"
        for p in error_pages:
            page_actions[p] = "error" if p in needed else "skipped"

        return OcrResult(
            markdown="\n\n".join(parts),
            pdf_path=None,
            page_count=len(parts),
            error_pages=error_pages,
            errors=errors,
            page_actions=page_actions,
            page_stats=sorted(page_stats, key=lambda s: s.page_num),
            inference_seconds=inference_s,
            elapsed_seconds=pages_elapsed,
        )


    @staticmethod
    def _pdftotext_page_text(pdf_path: Path, page_num: int) -> str:
        """Extract text for a single page with pdftotext (same as paperless-ngx).

        Runs ``pdftotext -q -layout -enc UTF-8`` once on the whole PDF, splits
        on form feeds, strips NUL bytes (as ``read_file_handle_unicode_errors``
        does for ingest), post-processes each page with the same whitespace
        normalization paperless-ngx applies, and returns the slice for
        *page_num* (1-based). Returns empty string on failure, so callers can
        fall back to pdf-inspector markdown.
        """
        import subprocess

        try:
            result = subprocess.run(
                [
                    "pdftotext",
                    "-q",
                    "-layout",
                    "-enc",
                    "UTF-8",
                    str(pdf_path),
                    "-",
                ],
                capture_output=True,
                timeout=120,
            )
            if result.returncode != 0:
                return ""
            full_text = result.stdout.decode("utf-8", errors="replace") or ""
        except Exception:
            return ""

        if not full_text:
            return ""

        # Strip NUL bytes the way paperless's read_file_handle_unicode_errors
        # does, so post_process_text sees the same bytes ingest does.
        full_text = full_text.replace("\x00", "")

        # pdftotext joins pages with a form feed (\f). Split, then
        # post-process each page individually (form feeds are whitespace
        # and would be collapsed by post_process_text on the full text).
        pages = full_text.split("\f")
        if page_num < 1 or page_num > len(pages):
            return ""
        return post_process_text(pages[page_num - 1]) or ""

    @classmethod
    def _pdftotext_all_pages(cls, pdf_path: Path) -> str:
        """Whole-PDF pdftotext text (layout mode), NUL-stripped, unsplit.

        Same extraction as :meth:`_pdftotext_page_text`, but returns the raw
        form-feed-joined text for callers that need several pages at once.
        """
        import subprocess

        try:
            result = subprocess.run(
                [
                    "pdftotext",
                    "-q",
                    "-layout",
                    "-enc",
                    "UTF-8",
                    str(pdf_path),
                    "-",
                ],
                capture_output=True,
                timeout=120,
            )
            if result.returncode != 0:
                return ""
            full_text = result.stdout.decode("utf-8", errors="replace") or ""
        except Exception:
            return ""
        return full_text.replace("\x00", "")

    def _pdftotext_page_map(self, pdf_path: Path) -> dict[int, str]:
        """pdftotext text keyed by 1-based page number, post-processed per page."""
        pages: dict[int, str] = {}
        full = self._pdftotext_all_pages(pdf_path)
        if not full:
            return pages
        for idx, chunk in enumerate(full.split("\f"), start=1):
            pages[idx] = post_process_text(chunk) or ""
        return pages

    def _mixed_content(
        self,
        original_pdf: Path,
        ocr_pdf: Path,
        ocr_pages: list[int],
    ) -> str:
        """Per-page content for a mixed-provenance ``--pages`` run.

        OCR'd pages come from the produced PDF's per-page text (the fresh
        Chandra layer via pdftotext, split on form feeds); native pages come
        from the *original* PDF's pdftotext text - the same composition the
        provenance-driven content pass produces (page order preserved, pages
        joined with blank lines).
        """
        ocr_map = self._pdftotext_page_map(ocr_pdf)
        orig_map = self._pdftotext_page_map(original_pdf)
        parts: list[str] = []
        for page_num in range(1, max(len(ocr_map), len(orig_map)) + 1):
            if page_num in ocr_pages:
                parts.append(ocr_map.get(page_num, ""))
            else:
                parts.append(orig_map.get(page_num, ""))
        return "\n\n".join(parts)

    def _render_page_image(self, page: Any, skip_blank: bool = True) -> Any:
        """Render one PyMuPDF page to a PIL Image, or None when blank."""
        import fitz  # PyMuPDF
        from PIL import Image

        if skip_blank and not page.get_text().strip() and not page.get_images():
            return None
        zoom = self.dpi / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

    def _render_pdf_pages(self, pdf_path: Path) -> list[Any]:
        """Render PDF pages to PIL Images using PyMuPDF.

        Args:
            pdf_path: Path to the PDF file.

        Returns:
            List of PIL Images, one per page (blank pages skipped).
        """
        import fitz  # PyMuPDF

        images: list[Any] = []
        try:
            try:
                doc = fitz.open(pdf_path)
            except fitz.FileDataError as e:
                # Unrenderable input (Office document, corrupt file, ...).
                # The pipeline skips non-OCR-able originals before this
                # point; reaching here means an unforeseen type slipped
                # through - fail with a clear message, not a mupdf dump.
                raise RuntimeError(
                    f"original is not a renderable document "
                    f"({pdf_path.suffix.lower() or 'unknown type'}): {e}"
                ) from e
            # Raster-image originals (JPG/PNG/...): the image IS the page, so
            # get_text()/get_images() are empty and the PDF blank-page
            # heuristic would skip every page. Render image documents
            # unconditionally; blank detection only applies to PDFs.
            allow_blank_skip = bool(doc.is_pdf)
            for page_num in range(len(doc)):
                image = self._render_page_image(doc[page_num], skip_blank=allow_blank_skip)
                if image is None:
                    log.debug("Skipping blank page %d", page_num + 1)
                    continue
                images.append(image)
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
