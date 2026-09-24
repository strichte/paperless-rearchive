# OCR Strategy

# Problem found:

Good question. Let me trace through the code for both paths and give you a precise answer — then update the docs to match reality, because I think there's a documentation gap here.

## Short answer

When `PDF_PROVENANCE=off`, **`SKIP_BORN_DIGITAL` is not consulted at all**. The document always proceeds to the OCR engine, and whether the born-digital content is preserved depends entirely on `OCR_MODE` (and the legacy heuristics inside `resolve_mode`), not on `SKIP_BORN_DIGITAL`.

The `SKIP_BORN_DIGITAL` setting only fires in the `PDF_PROVENANCE=on` path, at pipeline.py:148:
```python
elif provenance.kind == TEXT_BASED and settings.skip_born_digital:
    _preserve_born_digital(...)
```
When `PDF_PROVENANCE=off`, `provenance` is `None`, so this gate never executes.

---

## What actually happens with `PDF_PROVENANCE=off`

No `classify_pdf()` is called. The document goes straight to the OCR engine (`_ocr_document_ingest_pass` / `_ocr_document_pages`), passing `provenance=None`. Inside the engine's `_resolve_ingest_mode`, that triggers the legacy fallback:

```python
return resolve_mode(
    settings.ocr_mode,
    pdf_has_text=pdf_born_digital_text(pdf_path),
    pdf_is_digital_born=has_visible_text_content(pdf_path),
)
```

That's paperless-ngx's own document-level check (`pdf_born_digital_text` + `has_visible_text_content`). `SKIP_BORN_DIGITAL` is not part of that decision.

So the outcome for a born-digital document with `PDF_PROVENANCE=off` depends on `OCR_MODE`:

| `OCR_MODE` | What happens to born-digital content | Is it preserved? |
|---|---|---|
| `off` (skip_text) | ocrmypdf gets `--skip-text`; existing text layer kept; no Chandra OCR | **Yes**, but via ocrmypdf's skip_text, not via SKIP_BORN_DIGITAL |
| `auto` | No special flag; ocrmypdf default behaviour (preserve existing text unless it decides otherwise) | **Likely yes**, but not guaranteed — it's ocrmypdf's default, not an explicit skip |
| `redo` | ocrmypdf gets `--redo-ocr`; strips existing text layer and re-OCRs everything | **No** — native text destroyed |
| `force` | ocrmypdf gets `--force-ocr`; re-rasterises and re-OCRs everything | **No** — native text destroyed |

So `SKIP_BORN_DIGITAL=true` + `PDF_PROVENANCE=off` does **not** give you the same guarantee as `SKIP_BORN_DIGITAL=true` + `PDF_PROVENANCE=on`. With `on`, the doc is explicitly detected as `text_based` and skipped entirely (no OCR, no PATCH, no archive, tagged `re-ocr-preserved`). With `off`, the doc goes through ocrmypdf and the outcome is whatever `OCR_MODE` says.

### How paperless-ngx decides upstream

Reference for what the mode-driven fallback above mirrors (verified against upstream `main`):

- `pdf_born_digital_text` runs `pdftotext` and treats ≥ 50 extracted characters as "has text" (`PDF_TEXT_MIN_LENGTH` in `paperless/parsers/utils.py`). An invisible OCR overlay clears that easily — pre-paperless archive scans look born-digital to it.
- Mode → ocrmypdf flag (`paperless/parsers/tesseract.py`): `force` → `--force-ocr`, `redo` → `--redo-ocr`, `auto` → `--skip-text` when text was found (plain OCR otherwise), `off` → OCR never runs (pdftotext + format conversion only).
- paperless-ngx *can* re-run OCR on existing documents — the UI **Reprocess** action, `POST /api/documents/reprocess/`, or `document_archiver --overwrite` — but the action honors the **global** `PAPERLESS_OCR_MODE`; there is no per-call override ([#6289](https://github.com/paperless-ngx/paperless-ngx/issues/6289)). Under `auto`, reprocessing an overlay scan is a silent no-op: redoing it needs a global flip to `redo` + restart + flip back, which also changes how every future ingest is handled.

---

## The documentation currently overstates this

The README says (line 614):
> `REARCHIVE_SKIP_BORN_DIGITAL` | `true` | Works with `REARCHIVE_PDF_PROVENANCE` (both `on` and `off`): when a document is classified `text_based` (all native), `true` skips it entirely...

That's not accurate for the `off` case — with `PDF_PROVENANCE=off`, no classification happens, so `SKIP_BORN_DIGITAL` never gets a chance to act. It's effectively dead code in that path. The PLANNING doc has the same overstatement in the config table.

The one place that's accurate is `doc/OCR-STRATEGY.md:57`:
> `SKIP_BORN_DIGITAL` is irrelevant for mixed docs. It only gates all-native (`text_based`) documents.

That's true for both paths, but it should also note that it **only gates when `PDF_PROVENANCE=on`**.

---

## Should this be a design change?

Two options:

1. **Document the current behaviour accurately** — `SKIP_BORN_DIGITAL` is a provenance-gated setting; with `PDF_PROVENANCE=off` it has no effect, and `OCR_MODE` + legacy heuristics determine what happens to born-digital docs.

2. **Extend `SKIP_BORN_DIGITAL` to also gate the mode-driven path** — when `PDF_PROVENANCE=off` and the legacy heuristics detect a born-digital PDF, skip it if `SKIP_BORN_DIGITAL=true`. This would make the setting actually work the way the docs say it does for both paths.

Option 2 would be a behaviour change: today, `PDF_PROVENANCE=off` + `OCR_MODE=redo` + `SKIP_BORN_DIGITAL=true` on a born-digital doc would re-OCR it (destroying native text). With option 2, it would skip. That might be what people expect given the docs, but it's not what the code does today.

If you want, I can:
- First update the docs to accurately describe the current behaviour (no code change)
- Then, if you want, implement option 2 so `SKIP_BORN_DIGITAL` also gates the mode-driven path

Which do you want?
===================
## Two pathways through the pipeline

Before the matrix, it matters which pathway a document takes:

| Pathway | When | What drives the decision |
|---|---|---|
| **Provenance-driven** | `PDF_PROVENANCE=on` + PDF + verdict ∈ {`text_based`, `scanned`, `mixed`} | pdf-inspector per-page classification (`kind` + `pages_needing_ocr`) |
| **Mode-driven (legacy)** | `PDF_PROVENANCE=off`, or verdict `unknown`, or non-PDF, or `force` tag | `OCR_MODE` + legacy heuristics (`pdf_born_digital_text` + `has_visible_text_content`) |

For a **mixed** document (`some born-digital pages + some scanned pages`), the provenance-driven pathway is the interesting one. The mode-driven pathway treats it like any other PDF and lets `OCR_MODE` decide.

---

## Mixed document: `PDF_PROVENANCE=on` (default)

This is the default path. pdf-inspector classifies per page: some pages `needs_ocr=true` (scanned), some `needs_ocr=false` (native). Verdict = `mixed`, `pages_needing_ocr` = the scanned pages.

### re-ocr-content (content-only, no archive)

Content-only always goes through `_ocr_document_pages`: only `pages_needing_ocr` go to Chandra; native pages keep pdf-inspector's markdown (free, better than Chandra). The `OCR_MODE` knob **does not apply** — there is no ocrmypdf pass in content-only mode.

| `SKIP_BORN_DIGITAL` | `force` tag | What happens | Content field gets | Native pages |
|---|---|---|---|---|
| `true` (default) | no | Scanned pages → Chandra; native pages → pdf-inspector markdown | Merged markdown: native md + Chandra md for scanned pages | Kept as native text (pdf-inspector) |
| `false` | no | **Same as above.** `SKIP_BORN_DIGITAL` has no effect on mixed docs — it only gates all-native docs. | Same as above | Same as above |
| `true` or `false` | **yes** | **All pages → Chandra.** Provenance verdict ignored. Native pages are OCR'd by the LLM (their native text is replaced). | Chandra markdown for every page | **Lost** — replaced by Chandra output |

`OCR_MODE`, `OCR_MIXED_MODE`, `OCR_CLEAN`, `OCR_DESKEW`, `OCR_ROTATE` — **none of these apply** to content-only. They're ocrmypdf flags; there's no ocrmypdf pass.

### re-ocr-all (content + archive)

Archive mode runs a single ocrmypdf pass (`_ocr_document_ingest_pass`). The ocrmypdf mode is derived from `effective_mode(OCR_MODE, kind="mixed", mixed_mode=OCR_MIXED_MODE)`:

```python
# effective_mode for kind="mixed":
if mode == "redo":
    return mixed_mode   # defaults to "skip"
return "skip"           # for auto/force/off/skip
```

So for mixed docs, the archive **almost always** gets `--skip-text` (native pages keep their text, scanned pages get OCR'd). The only exception is `OCR_MODE=redo` + `OCR_MIXED_MODE=redo|force`.

| `OCR_MODE` | `OCR_MIXED_MODE` | `SKIP_BORN_DIGITAL` | `force` tag | ocrmypdf flag | Archive outcome | Content field | Native pages |
|---|---|---|---|---|---|---|---|
| `redo` (default) | `skip` (default) | `true` (default) | no | `--skip-text` | Scanned pages OCR'd; native pages keep text. PDF/A produced. | From ocrmypdf sidecar: native text + Chandra text for scanned pages | Preserved (native text kept) |
| `redo` | `skip` | `false` | no | `--skip-text` | **Same as above.** `SKIP_BORN_DIGITAL` has no effect on mixed docs. | Same | Preserved |
| `redo` | `redo` | `true`/`false` | no | `--redo-ocr` | **All pages re-OCR'd.** Native pages stripped and re-OCR'd by Chandra. Larger archive. | Chandra markdown for every page | **Lost** — replaced by Chandra |
| `redo` | `force` | `true`/`false` | no | `--force-ocr` + `--redo-ocr` | **All pages re-OCR'd with force.** Re-rasterises everything. Largest archives. | Chandra markdown for every page | **Lost** — replaced by Chandra |
| `auto` | *(ignored)* | `true`/`false` | no | `--skip-text` | Same as `redo`+ `skip`: scanned OCR'd, native kept. | From sidecar: native + Chandra | Preserved |
| `force` | *(ignored)* | `true`/`false` | no | `--force-ocr` | **All pages force-OCR'd.** Re-rasterises everything. | Chandra for every page | **Lost** |
| `off` | *(ignored)* | `true`/`false` | no | *(none — PDF/A conversion only)* | PDF/A conversion of the original. **No OCR at all.** Native + scanned text layers preserved as-is. | From pdftotext of produced PDF (plain text, not markdown) | Preserved (untouched) |
| `skip` | *(ignored)* | `true`/`false` | no | `--skip-text` | Same as `auto`: scanned OCR'd, native kept. | From sidecar: native + Chandra | Preserved |
| **any** | **any** | **any** | **yes** | `--force-ocr` | **All pages force-OCR'd.** Provenance verdict ignored. | Chandra for every page | **Lost** |

> **`SKIP_BORN_DIGITAL` is irrelevant for mixed docs.** It only gates all-native (`text_based`) documents. For mixed docs the verdict is `mixed`, never `text_based`, so the gate at pipeline.py:130 (`provenance.kind == TEXT_BASED and settings.skip_born_digital`) never triggers.

---

## All-native document (every page born-digital)

Verdict = `text_based`, `pages_needing_ocr = ∅`.

### re-ocr-content

| `SKIP_BORN_DIGITAL` | `force` tag | Outcome | Content field | Tags |
|---|---|---|---|---|
| `true` (default) | no | **Skipped entirely.** `_preserve_born_digital()` — no OCR, no PATCH, no archive, no DB. | **Untouched** (keeps original content) | Trigger → `<trigger>-success` + `re-ocr-preserved` + audit note |
| `false` | no | **OCR'd anyway.** Falls through to `_ocr_document_pages` with `pages_needing_ocr = ∅`. No pages sent to Chandra. Content from pdf-inspector markdown (native). | pdf-inspector native markdown (same or similar to what was there) | Trigger → `<trigger>-success` + audit note. No `re-ocr-preserved`. |
| `true` or `false` | **yes** | **All pages → Chandra.** Provenance verdict ignored. Native text replaced by LLM output. | Chandra markdown for every page | Trigger → `<trigger>-success`. No `re-ocr-preserved`. |

### re-ocr-all

| `OCR_MODE` | `SKIP_BORN_DIGITAL` | `force` tag | ocrmypdf flag | Outcome |
|---|---|---|---|---|
| `redo`/`auto`/`skip` | `true` (default) | no | `--skip-text` | **No OCR.** PDF/A conversion only. Native text preserved. Archive byte-identical (modulo PDF/A normalization). | 
| `force` | `true` | no | `--force-ocr` | **All pages OCR'd.** Native text stripped and replaced by Chandra. | 
| `off` | `true` | no | *(none)* | PDF/A conversion only, no OCR. Archive unchanged (modulo PDF/A normalization). |
| **any** | **any** | **yes** | `--force-ocr` | **All pages force-OCR'd.** Provenance + SKIP_BORN_DIGITAL both bypassed. |
| **any** | `false` | no | per OCR_MODE | `SKIP_BORN_DIGITAL=false` means "don't skip even if text_based". For `redo`: `--redo-ocr` strips native text and re-OCR's everything. For `off`: PDF/A only. For `auto`/`skip`: `--skip-text`, native kept. |

---

## All-scanned document (every page needs OCR)

Verdict = `scanned`, `pages_needing_ocr = {all pages}`.

### re-ocr-content
All pages → Chandra. No native pages to preserve. `SKIP_BORN_DIGITAL` has no effect (verdict is `scanned`, not `text_based`). Content = Chandra markdown for every page.

### re-ocr-all
| `OCR_MODE` | ocrmypdf flag | Outcome |
|---|---|---|
| `redo` (default) | `--redo-ocr` | All pages re-OCR'd. Original page images kept (no re-rasterising). |
| `auto` | *(none — ocrmypdf default)* | ocrmypdf decides. Since all pages are scanned (no text layer), ocrmypdf OCRs them. |
| `force` | `--force-ocr` | All pages re-OCR'd with force. Re-rasterises everything. |
| `off` | *(none)* | PDF/A conversion only. Since there's no text layer, the result is an image-only PDF/A (no searchable text). |
| `skip` | `--skip-text` | No-op effectively — no text to skip. ocrmypdf still produces PDF/A. |

`SKIP_BORN_DIGITAL` has no effect on all-scanned docs.

---

## The `force` tag (re-ocr-force) is the universal override

The `force` tag (configurable via `REARCHIVE_FORCE_TAG`, default `re-ocr-force`) is placed **next to** a trigger tag on a specific document. It:

1. Bypasses the provenance gate entirely (pipeline.py:123-129)
2. In the engine, bypasses the provenance verdict (chandra_engine.py:517-519) → mode = `"force"` → `--force-ocr`

**Effect: every page is OCR'd by Chandra, regardless of provenance, OCR_MODE, or SKIP_BORN_DIGITAL.** Native text is always replaced. This is per-document — you add it to one doc's tags, not a global setting.

---

## Quick reference: what each knob actually controls

| Knob | Controls | Doesn't control |
|---|---|---|
| `PDF_PROVENANCE=on/off` | Whether pdf-inspector per-page classification runs | `OCR_MODE` behaviour, force tag |
| `SKIP_BORN_DIGITAL=true/false` | Whether `text_based` verdict stops the run early | Mixed or scanned docs (no effect), force tag |
| `OCR_MODE=redo/auto/force/off/skip` | ocrmypdf mode flag for **archive runs only**; also drives legacy-mode fallback when no provenance verdict | Content-only runs (no ocrmypdf pass), force tag |
| `OCR_MIXED_MODE=skip/redo/force` | ocrmypdf mode for **mixed-provenance archive runs** when `OCR_MODE=redo` | Content-only, all-native, all-scanned, force tag |
| `force` tag | Universal override: OCR every page, ignore provenance | Global — per-document only |

The key thing to remember: **content-only and archive runs are different code paths.** Content-only is per-page Chandra with provenance routing (`_ocr_document_pages`); archive is a single ocrmypdf pass (`_ocr_document_ingest_pass`). `OCR_MODE` and `OCR_MIXED_MODE` only affect the archive path.