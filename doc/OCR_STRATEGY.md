# What paperless-ngx actually does on ingest (verified on this deployment)

> **Status update (2026-09-17): P1–P3 are implemented.**
>
> * **P1** — `_assemble_pdf()` drives the same call ingest drives (`plugins=["paperless_chandra.ocrmypdf_plugin"]` + `chandra_*` kwargs); the dead `hocr=` kwarg and the unused per-page hOCR plumbing are gone. Verified: archives 4221/5822/5823 re-run, `Creator: OCRmyPDF 17.11.0 / Chandra 0.2.0`, no Tesseract anywhere.
> * **P2/P3** — new `ocr/ingest_args.py` mirrors `construct_ocrmypdf_parameters` 1:1 (mode flags, clean/clean-final + redo remap, deskew suppression under redo, `pages` XOR `sidecar`, `user_args` merged last) plus the paperless text semantics (`post_process_text`, born-digital rule `tagged or normalised > 50`, sidecar placeholder → `pdftotext` fallback). Archive-mode runs now do **one** ocrmypdf pass producing both the PDF/A and the markdown sidecar content — the two-pass flow (per-page Chandra + assembly) is retired.
> * **D-decisions taken**: D1 = option 1 (full parity, single pass). D2 = `REARCHIVE_OCR_MODE` default **`redo`**; `auto` upgrades to `redo` on text-bearing PDFs (a re-OCR tool must re-OCR; `off` is available for PDF/A-conversion-only). D3 = `re-ocr-content` keeps the no-ocrmypdf per-page fast path (content-only runs never produce an archive). D4 = `re-ocr-page-errors` now applies to **content-only runs only**: with ocrmypdf driving archive runs, a page failure aborts the document → ingest-style safe fallback (`force_ocr` retry, clean/deskew per settings) → document failure tag.
> * New knobs: `REARCHIVE_OCR_CLEAN`, `REARCHIVE_OCR_ROTATE_PAGES`, `REARCHIVE_OCR_ROTATE_PAGES_THRESHOLD` (defaults mirror paperless); `REARCHIVE_OCR_MODE` default changed `auto` → `redo`.


**1. Ingest uses the Chandra parser, via a parser-plugin entry point**

```
$ docker exec paperless python3 -c "import importlib.metadata as m; print(list(m.distribution('paperless-chandra').entry_points))"
[('paperless_ngx.parsers', 'chandra', 'paperless_chandra.parser:PaperlessChandraParser')]
```

`PaperlessChandraParser.score()` returns 15 vs Tesseract's 10 (`PAPERLESS_CHANDRA_SCORE` default) → **Chandra wins for all supported MIME types**. The celery logs confirm it runs in the consume task (`documents.parsers.ParseError: MissingDependencyError: The Chandra server at http://ai:8777/v1 rejected the API key…`). So the parity target is **`paperless_chandra/parser.py`** (a near-copy of paperless's `parsers/tesseract.py`), not `paperless/parsers/tesseract.py`.

**2. The exact ocrmypdf parameter set** (`construct_ocrmypdf_parameters`, `paperless_chandra/parser.py:340-464`) — with this deployment's effective values:

| key | source / effective value |
| --- | --- |
| `input_file_or_options`, `output_file` | doc path → `archive.pdf` |
| `use_threads=True`, `jobs=PAPERLESS_THREADS_PER_WORKER` | thread-per-worker |
| `language` | `PAPERLESS_OCR_LANGUAGE` = `eng+deu` |
| `output_type` | `PAPERLESS_OCR_OUTPUT_TYPE` = `pdfa` (default) |
| `color_conversion_strategy` | `RGB` (default), only when `pdfa` |
| mode flags | `PAPERLESS_OCR_MODE` = **`auto`** (unset → default; legacy `skip`/`skip_noarchive` map to `auto`) |
| `clean` | `PAPERLESS_OCR_CLEAN` = `clean` (default) |
| `deskew` | `PAPERLESS_OCR_DESKEW` = `true` (default), **skipped when mode=redo** |
| `rotate_pages`, `rotate_pages_threshold` | `PAPERLESS_OCR_ROTATE_PAGES` = `true`, threshold `12.0` |
| `sidecar` **or** `pages="1-N"` | mutually exclusive (`PAPERLESS_OCR_PAGES` unset here) |
| `image_dpi` | only for image inputs (`PAPERLESS_OCR_IMAGE_DPI`, else detected, else A4 estimate, else ParseError) |
| `max_image_mpixels` | `PAPERLESS_OCR_MAX_IMAGE_PIXELS` (unset here) |
| `user_args` merged **last** (can override anything) | `PAPERLESS_OCR_USER_ARGS` = `{"invalidate_digital_signatures": true, "continue_on_soft_render_error": true}` |
| `plugins=["paperless_chandra.ocrmypdf_plugin"]` | ← **the Chandra wiring** |
| `chandra_server_url/model_name/api_key/max_output_tokens/content_format` | `PAPERLESS_CHANDRA_*`, content format = `markdown` |

**Mode semantics** (`ModeChoices` = `auto|force|redo|off`, note the `off` value and the normalised `skip_text` kwarg):

| mode | ocrmypdf flag | ingest behaviour |
| --- | --- | --- |
| `auto` (default) | *(none)* when no text layer; `skip_text` when text present | born-digital + no archive → **skips ocrmypdf entirely** (content = `pdftotext`); born-digital + archive → **PDF/A conversion only**, no re-OCR |
| `force` | `force_ocr` | rasterise + OCR |
| `redo` | `redo_ocr` | strip text layer, re-OCR; deskew suppressed |
| `off` | `skip_text` / no engine | PDF/A conversion only |

**Born-digital rule**: `is_tagged_pdf(path) or len(post_process_text(pdftotext)) > 50` (`PDF_TEXT_MIN_LENGTH = 50`).
**Content**: sidecar text, unless mode=`redo` → text extracted from the produced PDF; both via `post_process_text()` (collapse runs of spaces, strip line padding, `\0`→space). A sidecar containing `[OCR skipped on page` is **discarded** and `pdftotext` of the output PDF is used instead.
**Safe fallback**: on `PriorOcrFoundError` / `InputFileError` / no-text → rebuild args with `safe_fallback=True` → `force_ocr` retry (clean/deskew still applied per settings).

---

# Where re-OCR currently diverges — including a real bug

**⚠️ The re-OCR'd archive text layer is Tesseract, not Chandra.** Proof:

```
Creator:  OCRmyPDF 17.11.0 / OCRmyPDF fpdf2 + Tesseract OCR 5.5.0     ← doc 4221's replaced archive
2026-09-16 07:28:24 WARNING ocrmypdf._exec.tesseract: [tesseract] lots of diacritics - possibly poor OCR
```

Root cause: `_assemble_pdf()` calls

```python
ocrmypdf.ocr(str(original_pdf), output_file=..., output_type="pdfa", language=..., redo_ocr=True, hocr=str(hocr_path), jobs=1)
```

- **`hocr=` is not an ocrmypdf option and is silently ignored.** Verified two ways: `inspect.signature(ocrmypdf.ocr)` has no `hocr` (only `**kwargs`), and a probe passing `hocr='/tmp/does-not-exist.hocr'` with `redo_ocr=True` *succeeded* — a control probe with `bogus_xyz_option=1` was ignored identically. So every Chandra hOCR page we render is thrown away.
- **`plugins=[...]` is not passed**, so the Chandra engine isn't loaded either → `redo_ocr=True` strips the old layer and re-OCRs with **Tesseract**.

Net effect: `content` = Chandra markdown, archive text layer = Tesseract — i.e. a re-OCR'd document is *not* what ingest would have produced (and the GPU work for hOCR is wasted).

Other divergences:

| aspect | paperless ingest | current re-OCR (active engine) |
| --- | --- | --- |
| mode/clean/deskew/rotate/color/image_dpi/pixel limit | full semantics | **all dead** — only in legacy `runner.py`; no deskew/clean/rotate applied at all |
| page rasterisation | ocrmypdf (`redo_ocr` keeps original images) | our own PyMuPDF render at a **hardcoded 300 dpi** (`getattr(settings,'ocr_dpi',300)` — no such setting exists) |
| born-digital detection | tagged-PDF **or** normalised text > 50 | raw `pdftotext` ≥ 25 |
| sidecar placeholder handling | discard sidecar → use `pdftotext` of output | hard failure (`ocr_skipped_all`) |
| content post-processing | `post_process_text()` | none |
| text source | sidecar (single ocrmypdf pass) | our own markdown assembly |
| safe fallback | `force_ocr`, keeps clean/deskew | strips clean/deskew/rotate |
| page parallelism | `jobs`/`use_threads` | our `REARCHIVE_OCR_CONCURRENCY` threads |

---

# Fine-tuned plan

## Target architecture — "one ingest-identical ocrmypdf pass"

The principled way to make re-OCR behave exactly like ingest is to **drive the same call ingest drives**: one `ocrmypdf.ocr(**args)` per document, with `plugins=["paperless_chandra.ocrmypdf_plugin"]` + the `chandra_*` kwargs, using a parameter builder that mirrors `construct_ocrmypdf_parameters` 1:1. That yields, from a single Chandra pass: the PDF/A (with a **Chandra** text layer) *and* the markdown sidecar (content).

- `re-ocr-all` → use the produced PDF/A + sidecar text.
- `re-ocr-content` → same call; discard the PDF/A (this is exactly what ingest does when `produce_archive=False`) — or keep an opt-in cheaper fast path.
- Content = sidecar → `post_process_text()`, i.e. identical to ingest.
- REARCHIVE keeps its own layer: trigger tags/outcome tags, page cap (`pages="1-N"`), document-level batching + docs/min, provenance + audit note, `re-ocr-page-errors`, archive replacement + checksum UPDATE.
- Retire the dead knobs / miswired ones (`ocr_dpi` getattr, page-thread concurrency → superseded by `jobs`/`use_threads`), and mirror PAPERLESS_* names for the new knobs so the mapping is obvious.

## Phases

**P1 — Fix the archive text layer (highest value, small).** Add `plugins=["paperless_chandra.ocrmypdf_plugin"]` + `chandra_server_url/model_name/api_key/max_output_tokens/content_format` to the assembly call, **remove the dead `hocr=` kwarg**, and decide what drives the text layer (plugin OCR — the parity choice). Verify the produced archive shows `Creator: …Chandra…` instead of Tesseract.

**P2 — Ingest-parity parameter builder.** New module (`ocr/ingest_args.py`, or refactor `runner.py`) reproducing paperless's `construct_ocrmypdf_parameters` exactly, incl. the clean/clean-final + redo interactions, deskew suppression under redo, `pages` XOR `sidecar`, `user_args` merged last, `max_image_mpixels`, image alpha/DPI handling, and the `safe_fallback` path. New REARCHIVE knobs mirroring PAPERLESS_* with **paperless's defaults**.

**P3 — Semantics parity.** Born-digital rule (`tagged or normalised > 50`), sidecar-placeholder handling (discard → `pdftotext` of output), `post_process_text()` on content, mode behaviour incl. the "auto + text + no archive ⇒ skip ocrmypdf" and "auto + text + archive ⇒ PDF/A only" shortcuts.

**P4 — Docs & cleanup.** README OCR-strategy section rewritten against the real engine; PLANNING config table, Risks, diagrams; retire `runner.py`'s legacy-only knobs (or mark them used solely by the integration harness).

## Verification
- Re-OCR doc 4221 with the new path: assert archive `Creator` contains `Chandra`, `pdftotext(archive)` matches the sidecar/content, and logs show `deskew`/`rotate_pages`/`clean` as configured.
- A/B: consume the same source PDF fresh (ingest) vs. re-OCR it, then diff text layers + effective params.
- Unit tests for the builder mirroring paperless's own test cases (mode combos, deskew+redo, clean-final+redo, pages vs sidecar, user_args override, image DPI).

## Decisions needed

Open items I'd rather not guess:
- **D1 architecture** (the question below).
- **D2 mode default for re-OCR**: literal parity = `auto` — but on a text-bearing doc that means *PDF/A conversion only, no re-OCR*; the purpose of this tool suggests default `redo` (or `auto` that detects a text layer and switches to `redo`). Needs your call.
- **D3 `re-ocr-content`**: accept the ingest-like full ocrmypdf run (PDF/A conversion cost) or keep the no-ocrmypdf fast path (documented deviation)?
- **D4 `re-ocr-page-errors`**: with ocrmypdf driving, per-page Chandra failures abort the document (→ safe fallback), so the partial-failure tag semantics need redefining.

### D1 options ("How should we pursue ingest parity for re-OCR?")

1. **Full parity (recommended)**: one ingest-identical ocrmypdf pass per document for both modes — content from the sidebar sidecar, archive from the same pass, Chandra text layer, all PAPERLESS_OCR_* semantics re-introduced.
2. **Archive-only parity**: keep our per-page Chandra engine for content, but fix the archive assembly to use the plugin + ingest parameters (accepts two Chandra passes per document).
3. **Docs-first**: document the divergence and the Tesseract-text-layer bug now, implement the parity work as a separate follow-up phase.
4. **Full parity but keep the current cheap content-only fast path** (no ocrmypdf for `re-ocr-content`) as an opt-in, with ingest-parity as the default.

---

## Reproducing this provenance check

The engine that produced an archive's text layer is recorded in the PDF `Creator`
metadata, so no host tooling is needed - everything runs inside the containers:

```bash
# one archive, or several (paths relative to /archives)
tests/integration/check_archive_provenance.sh \
    "Rental Properties/2007/Sue Foley/2007-01-15_Solicitors Trust Account Statement and Titles.pdf"

# the whole archive mount (slow: one pdfinfo per file), optionally capped
tests/integration/check_archive_provenance.sh --all --limit 20
```

Exit status: `0` = every checked archive shows Chandra, `1` = at least one
Tesseract or unreadable file, `2` = usage error.

The raw one-liners used during the investigation:

```bash
# the PDF's own metadata
docker exec paperless-rearchive sh -c 'pdfinfo "/archives/<rel>/<file>.pdf" | grep -iE "^(creator|producer)"'
docker exec paperless-rearchive python3 -c "import pymupdf; print(pymupdf.open('/archives/<rel>/<file>.pdf').metadata['creator'])"

# the fallback engine announcing itself in the sidecar's logs
docker logs paperless-rearchive 2>&1 | grep -iE 'ocrmypdf\._exec\.tesseract'
```

**Measured impact (2026-09-17): every archive the sidecar has rewritten so far is
affected - 3 of 3 `re-ocr-all-success` documents:**

| doc | archive (relative) | Creator |
| --- | --- | --- |
| 4221 | `Rental Properties/2007/Sue Foley/2007-01-15_Solicitors Trust Account Statement and Titles.pdf` | `OCRmyPDF 17.11.0 / OCRmyPDF fpdf2 + Tesseract OCR 5.5.0` |
| 5822 | `Passports_Visas_IDs/DE/1971/1971-10-25_re-archive-test-no-ocr-original.pdf` | `OCRmyPDF 17.11.0 / OCRmyPDF fpdf2 + Tesseract OCR 5.5.0` |
| 5823 | `Passports_Visas_IDs/DE/2026/2026-01-07_re-archive-test-ocr-d-original.pdf` | `OCRmyPDF 17.11.0 / OCRmyPDF fpdf2 + Tesseract OCR 5.5.0` |

There are no `re-ocr-content-success` documents yet, and content-only runs never
touch the archive, so they are unaffected. Older Tesseract-era ingest archives
(ocrmypdf 15.x/16.x/17.4) are expected to show Tesseract and are outside the
re-OCR scope.

---

## ocrmypdf mode restrictions (why deskew is unavailable under `redo`)

`--mode redo` is **incompatible with `--deskew`, `--clean-final` and
`--remove-background`** (hard error - `ocrmypdf/_options.py:392`,
`_validation_coordinator.py:101`), because `redo` keeps the original page images and
will not rasterise them. `--clean` and `--rotate-pages` *are* allowed under `redo`.

So whichever options re-OCR re-introduces, the effective set is mode-dependent:

| mode | clean | deskew | rotate_pages | archive text layer |
| --- | --- | --- | --- | --- |
| `auto` (ingest default, page without text) | yes | yes | yes | Chandra |
| `redo` (re-OCR's natural mode) | yes (never `clean-final`) | **no - forbidden by ocrmypdf** | yes | Chandra |
| `force` | yes / `clean-final` | yes | yes | Chandra (archive grows: 616 KiB -> 4.6 MiB measured) |

paperless/paperless-chandra apply exactly this rule: `deskew` only when
`mode != redo`, and `clean` (never `clean-final`) under `redo`
(`paperless_chandra/parser.py:387-400`).

Consequences for the D1 decision:

* "Ingest parity" **restores** deskew and rotate relative to today's engine (which
  applies neither); the only case where deskew stays unavailable is `redo`, and that
  is ocrmypdf's constraint - paperless has the same limitation.
* `rotate_pages` (90/180/270 orientation, via the plugin's
  `ChandraEngine.get_orientation` -> `osd.detect_orientation`) is what protects
  badly-oriented pages from useless OCR, and it is available in **every** mode.
* Fine-skew correction under `redo` would require either `force` mode (accepting the
  archive-size growth) or deskewing the pages in our own render path before the
  Chandra call.

### Recommendations from glm-5.3-flash

1. __Adopt D1 option 1 (full parity)__ — confirmed as the right call by my independent reading; one ingest-identical pass, plugin + chandra kwargs, remove the dead `hocr` plumbing (and `_combine_hocr_pages` becomes obsolete — the whole per-page hOCR assembly can go).
2. __D2: default `redo`, not `auto`__ — a re-OCR tool's purpose is to re-OCR; `auto` would silently no-op on text-bearing docs. Keep `auto` available as an explicit mode.
3. __D3: keep the no-ocrmypdf fast path for `re-ocr-content`__ as a documented deviation (option 4). Full parity buys the PDF/A as a by-product, which is pure waste when `produce_archive=False`; note Chandra markdown *is* the content paperless-chandra produces anyway (content_format=markdown), so parity of content is preserved without the PDF/A cost.
4. __D4__: define `re-ocr-page-errors` against ocrmypdf's actual failure model (document-level exit → safe fallback → `force_ocr`), and drop the per-page partial tag unless you keep the render-before-OCR path.
5. __P1 first__ (small, fixes real data corruption risk), then __P2__ with unit tests mirroring paperless's mode-combo cases. Add an explicit remediation step: re-run docs 4221/5822/5823 after P1 and assert `Creator` contains Chandra + `pdftotext(archive)` ≈ content.
6. Also remove the `getattr(settings, 'ocr_dpi', 300)` in favour of a real `REARCHIVE_OCR_DPI`-style knob (or drop it entirely under ocrmypdf parity, where rasterisation is ocrmypdf's job).

This was all read-only (plan mode) — no files changed. If you're happy, toggle to __Act mode__ and I can start with P1 (the archive-text-layer fix) plus the remediation re-run of the three affected documents.
