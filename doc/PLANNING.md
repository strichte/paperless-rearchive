# paperless-rearchive — Planning

Working plan for the tag-driven re-OCR sidecar. This document is the source of truth for coding
agents; keep it up to date as phases complete. Status markers: ⬜ todo, 🔧 in progress, ✅ done.

## 1. Goal

Re-process the OCR of thousands of existing paperless-ngx documents using LLM vision OCR
(Chandra via the paperless-chandra ocrmypdf plugin), driven by tags:

- `re-ocr-content` — re-OCR, replace the document `content` field with the OCR output (markdown).
- `re-ocr-all` — as above, plus regenerate the archive version of the document and update the
  database checksum.

The original file is immutable; it is only downloaded via the API and used as OCR source.

## 2. Research notes (confirmed against paperless-ngx source / docs)

- **Archive checksum**: paperless sets `archive_checksum` with
  `documents.utils.compute_checksum` — **SHA-256** of the file bytes (verified live: the
  on-disk file's SHA-256 matches the DB value exactly; earlier paperless releases used MD5)
  when the archive is moved into `settings.ARCHIVE_DIR`. Correct update statement:
  `UPDATE documents_document SET archive_checksum = '<sha256>' WHERE id = <id>;`
- **Archive directory**: `media/documents/archive/` (singular) on this deployment, with
  template-derived subdirectories (e.g. `Retirement/Steffen Richter/2026/…pdf`);
  246 of 5352 documents have no archive file.
- **Archive filename / path**: *not* always `<id>.pdf`. `file_handling.generate_unique_filename(
  doc, archive_filename=True)` derives it from storage-path / `PAPERLESS_FILENAME_FORMAT`
  templates (or `<pk:07>.pdf` when no template). The API's `archived_file_name` is only a
  **flattened display name** (e.g. `2026-01-07 Immigration US re-archive-test.pdf`) and does
  **not** give the on-disk location. The real relative path lives only in the DB column
  `documents_document.archive_filename` (e.g.
  `Passports_Visas_IDs/DE/2026/2026-01-07_re-archive-test.pdf`); the sidecar resolves it via
  `archive/db.py: fetch_archive_filename` and never reconstructs it from the document id.
- **Born-digital PDFs**: in `auto` mode paperless skips OCR for PDFs with an existing text layer
  (`skip_text`, `pdf_born_digital_text`); `PAPERLESS_ARCHIVE_FILE_GENERATION`
  (`never`/`only`/`always`) decides whether an archive file is produced at all. Documents
  without an archive file ⇒ content-only mode in this sidecar.
- **Non-PDF originals** (jpg/png/tiff/…): paperless converts them to a PDF during consumption
  (`PAPERLESS_OCR_IMAGE_DPI`, A4 DPI fallback). The sidecar runs the same conversion via
  ocrmypdf but cannot regenerate an *existing* archive file (none exists) ⇒ content-only by
  default; `REARCHIVE_ARCHIVE_FOR_IMAGES=true` later opts into creating one (experimental).
- **API limits**: there is no endpoint to upload/replace the archive version of an existing
  document, and the document upload endpoint only creates *new* documents — hence the
  bind-mount + direct DB update approach used here.
- **Downloading the *original***: `/api/documents/{id}/download/` returns the **archive** version
  by default. The immutable original requires the query parameter `?original=true` (verified
  live — without it the sidecar re-OCR'd an archive it had itself just written). The sidecar
  always passes `original=true` and works in a scratch directory; nothing writes to
  `media/documents/originals/`.
- **Tag lookup**: `/api/tags/` silently ignores `?name=` and `?name__exact=`; `?name__iexact=`
  works. `?name__icontains=` is a trap — `re-ocr-all` also matches `re-ocr-all-success`. The
  sidecar uses `name__iexact` (regression-tested in `tests/test_paperless_api.py`).
- **Content update**: `PATCH /api/documents/{id}/ {"content": "..."}` works — `content` is
  writable in the documents serializer.
- **Live probe (2026-09-15, instance with 5352 docs)**: the serializer does **not** expose
  `archive_checksum`, so it is read directly from Postgres
  (`archive/db.py: fetch_archive_checksum`); 246 of 5352 documents have no archive file.
- **OCR mode vs. text layer**: `redo_ocr` replaces the invisible text layer in place and leaves
  the page images alone, so the archive keeps its size; `force_ocr` rasterises every page and
  inflates the file (measured 616 KiB → 4.6 MiB on one scan). `ocrmypdf` **rejects**
  `--redo-ocr` combined with `--deskew`/`--clean-final`/`--remove-background`, so the runner
  must drop deskew rather than fall through to `force_ocr` (a silent fallback here would have
  inflated every archive).
- **`skip_text` sidecar hazard**: with `skip_text`, ocrmypdf's sidecar contains only
  `[OCR skipped on page(s) …]`. Writing that to `content` destroys the existing text, so the
  runner treats an all-placeholder sidecar as a hard failure (`ocr_skipped_all`) instead of a
  result.
- **Chandra repeat-loop = failed scan**: when the vLLM/Chandra server logs
  `Detected repeat token, retrying generation (attempt N)` and the GPU spins up, the model is
  looping on a bad/empty page. Such runs are effectively failed scans — quality is garbage even
  though the pipeline completes. Detection/handling is deferred — see Risks.
- **Base image**: paperless-ngx now ships `python:3.14-slim` (Debian 13 *trixie*) and this
  sidecar uses the same base. `jbig2` (bilevel compression for ocrmypdf) is available as an apt
  package in trixie and is installed directly; together with `pngquant` it saved ~15-19% on a
  real re-OCR run.

## 3. Architecture

```mermaid
flowchart TB
    subgraph container["paperless-rearchive container"]
        MAIN["poller.py (main loop)<br/>POLL_INTERVAL / SIGHUP / RUN_ONCE"]
        PIPE["pipeline.py<br/>process_document()"]
        API["paperless_api.py<br/>(requests.Session, Token auth)"]
        RUN["ocr/runner.py<br/>build_ocrmypdf_args()"]
        PROV["ocr/base.py: OcrProviderPlugin ABC"]
        CHAN["ocr/chandra.py: ChandraProvider"]
        REP["archive/replacer.py<br/>backup + os.replace + sha256"]
        DBM["archive/db.py<br/>psycopg UPDATE archive_checksum"]
        CFG["config.py (Settings.from_env)"]
        MAIN --> CFG
        MAIN --> PIPE
        PIPE --> API
        PIPE --> RUN
        RUN --> PROV
        PROV -.implements.-> CHAN
        PIPE --> REP
        REP --> DBM
        PIPE --> CFG
    end
    API -- "HTTP :8000" --> PLX["paperless-ngx"]
    CHAN -- "chat/completions" --> LLM["Chandra server ai:8110/v1"]
    REP -- "bind mount rw" --> ARC["archives/*.pdf"]
    DBM -- "5432" --> PG[("postgres")]
```

### Plugin architecture

An `OcrProviderPlugin` encapsulates everything LLM-specific:

- `name` — provider id (`REARCHIVE_PROVIDER=chandra`).
- `ocrmypdf_plugin_module` — module passed as `plugins=[...]` to `ocrmypdf.ocr()`
  (paperless-chandra ships `paperless_chandra.ocrmypdf_plugin`, which registers `--chandra-*`
  kwargs and returns the Chandra `OcrEngine`).
- `ocrmypdf_kwargs()` — provider settings forwarded as ocrmypdf kwargs
  (server URL, model name, API key, max tokens, content format).
- `validate()` — fail-fast configuration probe (paperless-chandra already probes the server in
  its `check_options` hookimpl).

Because ocrmypdf renders the invisible text layer from the engine's hOCR and writes the
sidecar text (markdown when `content_format=markdown`), a future provider only needs its own
ocrmypdf `OcrEngine` + plugin module. Reusable paperless-chandra pieces for new providers:
`engine/hocr.py` (typed page model + hOCR serialisation), `engine/pdf.py` (text-only PDF
rendering), `engine/geometry.py`, `engine/blocks.py`.

### Per-document flow

```mermaid
flowchart TD
    START([document with trigger tag]) --> DL["download original via API<br/>(temp dir, never modified)"]
    DL --> ARCH{"re-ocr-all AND archived_file_name present AND original is PDF?"}
    ARCH -- "no" --> CO["content-only mode"]
    ARCH -- "yes" --> CK{"on-disk archive sha256 == DB archive_checksum?"}
    CK -- "no" --> FAIL["failure tag<br/>(archive changed underneath us)"]
    CK -- "yes" --> OCR["ocrmypdf: output_type=pdfa, plugins=provider<br/>auto → redo_ocr (text layer) / skip_text (none)<br/>force option only via REARCHIVE_OCR_MODE=force"]
    CO --> OCR
    OCR -- "error (transient)" --> KEEP["keep trigger tag<br/>(retry next cycle)"]
    OCR -- "error (permanent)" --> FAIL
    OCR -- "ok" --> DRY{"DRY_RUN?"}
    DRY -- "yes" --> REPORT["log what would be written<br/>+ keep trigger tag"]
    DRY -- "no" --> PATCH["PATCH content (markdown)"]
    PATCH --> MODE{"re-ocr-all?"}
    MODE -- "yes" --> REPL["backup old archive to .bak<br/>atomic os.replace(new, archive_path)<br/>sha256 then UPDATE documents_document"]
    MODE -- "no" --> TAGS
    REPL --> TAGS["remove trigger tag<br/>add ...-success"]
    FAIL --> TAGSF["remove trigger tag<br/>add ...-failure"]
    TAGS --> DONE([done])
    TAGSF --> DONE
    REPORT --> DONE
    KEEP --> DONE
```

## 4. Repository layout

```
paperless-rearchive/
├── README.md                  # living overview + status
├── doc/
│   ├── PLANNING.md            # this file
│   └── deploy/compose-snippet.yml
├── paperless_rearchive/
│   ├── __init__.py
│   ├── config.py              # env-driven Settings
│   ├── logging_setup.py       # shared logging config (poll + containers)
│   ├── secrets.py             # *_FILE secret resolution (paperless-ngx convention)
│   ├── paperless_api.py       # REST client (tag lookup, original download, PATCH, tag swap)
│   ├── pipeline.py            # per-document orchestration
│   ├── poller.py              # main loop (entry point)
│   ├── ocr/
│   │   ├── __init__.py
│   │   ├── base.py            # OcrProviderPlugin ABC + registry
│   │   ├── chandra.py         # Chandra provider
│   │   └── runner.py          # Django-free ocrmypdf argument builder + image helpers
│   └── archive/
│       ├── __init__.py
│       ├── db.py              # psycopg checksum/archive reads + UPDATE
│       └── replacer.py        # backup + atomic replace + sha256
├── docker/Dockerfile
├── pyproject.toml
└── tests/
    ├── test_*.py              # pytest unit tests (39 passing)
    └── integration/           # live-stack harness (see its README)
```

## 5. Phases

### Phase 1 — Documentation & scaffold ✅
- ✅ README.md, PLANNING.md, diagrams
- ✅ package scaffold, pyproject, Dockerfile, compose snippet

### Phase 2 — Core plumbing ✅
- ✅ `config.py` Settings
- ✅ `paperless_api.py` (ensure_tag, doc_ids_with_tag, download, patch content, tag swap)
- ✅ `ocr/base.py` ABC + provider registry (validated live against local paperless-chandra)

### Phase 3 — OCR pipeline ✅
- ✅ `ocr/chandra.py` provider (wraps paperless-chandra ocrmypdf plugin)
- ✅ `ocr/runner.py` (mode selection `redo_ocr`/`skip_text`/`force_ocr`, pdfa, deskew/clean with
  the redo-incompatibility guard, image DPI/alpha handling, `ocr_skipped_all` guard, fallback retry)
- ✅ `pipeline.py` orchestration incl. dry-run + tag lifecycle

### Phase 4 — Archive replacement ✅
- ✅ `archive/db.py` (psycopg; fetch + update archive_checksum)
- ✅ `archive/replacer.py` (verify checksum, backup, atomic replace, sha256)
- ✅ DB password from secret file `paperless_db_paperless_passwd`

### Phase 5 — Deployment & tests 🔧
- ✅ Dockerfile (python:3.14-slim/trixie + ghostscript/tesseract/qpdf/pngquant/jbig2/poppler +
  ocrmypdf + paperless-chandra from git master; image builds and all deps import on 3.14)
- ✅ compose snippet + `.env.example`
- ✅ unit tests (39 passing: replacer, runner args/mode selection, API tag lookup, config)
- ✅ live smoke test (2026-09-15): dry-run cycle against the running instance
  (`http://localhost:8001`) — document 5488 tagged `re-ocr-content` + `re-ocr-all` was OCR'd
  end-to-end via the live Chandra server (~30k chars markdown sidecar produced, nothing
  written, trigger tag kept). Dry-run never touches tags — enforced in `pipeline`.
- ✅ in-container e2e of the OCR stage (`tests/size_compare.py`, run on the `backend` network
  with real DB + API access): DB path resolution via `fetch_archive_filename`, checksum
  verification, full ocrmypdf+Chandra run.
- ✅ `REARCHIVE_OCR_MODE=auto` added: redo_ocr (text layer present) vs force_ocr.
- ✅ **full e2e incl. writes (2026-09-16, doc 5820, both trigger paths)**:
  - `re-ocr-all`: poller run → archive replaced (624 KiB, PDF/A-2b, sha256 `d9203b60…`) →
    `archive_checksum` UPDATE verified against `sha256sum` on disk → tag swapped to
    `re-ocr-all-success` → **original byte-identical** (`31feb5e0…`, mtime unchanged).
  - `re-ocr-content`: content PATCHed to freshly OCR'd markdown → archive file **untouched**
    (byte-identical) → tag swapped to `re-ocr-content-success`.
  - harness: `tests/run_poller_e2e.sh`, `tests/run_content_e2e.sh`, `tests/verify_state.sh`,
    `tests/reset_baseline.sh` (env-driven, run against the live stack).
- ✅ API correctness fixes found by e2e: `?original=true` on the download endpoint,
  `?name__iexact=` for tag lookup.
- ✅ archive size root cause identified and fixed: the silent `redo_ocr` → `force_ocr` fallback
  triggered by the (unsupported) `redo_ocr` + `deskew` combination. See Research notes / Risks.
- ⬜ repeat-loop detection: treat documents whose Chandra logs show repeated
  `Detected repeat token, retrying generation` as failed scans (tag `-failure` / investigate).
- ⬜ bulk-run procedural safeguards: confirm search index reflects PATCHed content on a real
  doc, and rehearse a small `re-ocr-all` batch before mass re-OCR.

## 6. Configuration

Environment variables (prefix `REARCHIVE_` where generic; `PAPERLESS_CHANDRA_*` shared with the
paperless container for provider settings).

**Secret convention:** every credential supports the paperless-ngx `_FILE` mechanism — for a
variable `NAME`, the environment variable `NAME_FILE` may point at a file (e.g. a Docker secret)
whose content is used instead of `NAME`. `_FILE` wins when both are set. This applies to the API
token, the Chandra key, and the database user/password.

| Variable | Default | Description |
| --- | --- | --- |
| `PAPERLESS_BASE_URL` | `http://paperless:8000` | paperless-ngx base URL |
| `PAPERLESS_API_TOKEN` *(or `PAPERLESS_API_TOKEN_FILE`)* | *(required)* | API token (from `.env.paperless-gpt`) |
| `REARCHIVE_TRIGGER_TAG_CONTENT` | `re-ocr-content` | trigger tag, content-only mode |
| `REARCHIVE_TRIGGER_TAG_ALL` | `re-ocr-all` | trigger tag, archive + content mode |
| `REARCHIVE_SUCCESS_SUFFIX` | `-success` | success tag suffix |
| `REARCHIVE_FAILURE_SUFFIX` | `-failure` | failure tag suffix |
| `REARCHIVE_ARCHIVE_DIR` | `/archives` | bind-mounted `media/documents/archive` |
| `REARCHIVE_PROVIDER` | `chandra` | OCR provider plugin |
| `PAPERLESS_CHANDRA_SERVER_URL` | *(required)* | e.g. `http://ai:8110/v1` |
| `PAPERLESS_CHANDRA_MODEL_NAME` | `chandra` | e.g. `chandra-ocr-2-q8` |
| `PAPERLESS_CHANDRA_API_KEY` *(or `PAPERLESS_CHANDRA_API_KEY_FILE`)* | *(optional)* | Chandra server API key / secret file |
| `PAPERLESS_CHANDRA_CONTENT_FORMAT` | `markdown` | `markdown` or `text` |
| `PAPERLESS_CHANDRA_MAX_OUTPUT_TOKENS` | `12384` | per-page token budget |
| `REARCHIVE_OCR_LANGUAGE` | `eng` | passed through to ocrmypdf (labels hOCR) |
| `REARCHIVE_OCR_MODE` | `auto` | `auto`: `redo_ocr` when the original has a text layer (swaps the invisible text layer, page images untouched), `skip_text` when it has none (OCRs the bare pages); `force`: always rasterise + re-OCR (much larger archive); `redo`: always redo |
| `REARCHIVE_OCR_DESKEW` | `true` | deskew pages before OCR |
| `REARCHIVE_OCR_OUTPUT_TYPE` | `pdfa` | archive PDF/A flavour |
| `REARCHIVE_OCR_USER_ARGS` | *(unset)* | extra ocrmypdf kwargs (JSON), e.g. paperless `PAPERLESS_OCR_USER_ARGS` |
| `REARCHIVE_ARCHIVE_FOR_IMAGES` | `false` | experimental: create archive for non-PDF originals |
| `REARCHIVE_POLL_INTERVAL` | `300` | seconds between polls |
| `REARCHIVE_BATCH_LIMIT` | `5` | max documents per cycle |
| `REARCHIVE_OCR_CONCURRENCY` | `2` | page-level concurrency inside ocrmypdf (jobs) |
| `REARCHIVE_MAX_PAGES` | `0` | abort documents with more pages (0 = unlimited) |
| `REARCHIVE_DRY_RUN` | `false` | OCR + report only, no writes |
| `REARCHIVE_RUN_ONCE` | `false` | single cycle then exit |
| `REARCHIVE_LOG_LEVEL` | `INFO` | log level |
| `PAPERLESS_DBHOST` / `PAPERLESS_DBPORT` / `PAPERLESS_DBNAME` | `postgres` / `5432` / `paperless` | DB connection |
| `PAPERLESS_DBUSER` *(or `PAPERLESS_DBUSER_FILE`)* | `paperless` | DB user |
| `PAPERLESS_DBPASS` *(or `PAPERLESS_DBPASS_FILE`)* | *(required for re-ocr-all)* | DB password / secret file |

Secrets (already defined in `paperless-lxc/docker-compose.yml`): `chandra_api_key`,
`paperless_db_paperless_passwd`.

## 7. Risks / open questions

- ✅ **Resolved** — `archived_file_name` is *not* a path; the on-disk archive path is read from
  the DB column `documents_document.archive_filename` (see Research notes).
- ⬜ Full-text index / search index is updated by paperless on content PATCH (serializers post
  save) — confirm search reflects new content after a real PATCH.
- ⬜ Thumbnails are not regenerated (archive pixel content is unchanged in `redo_ocr`, so the
  existing thumbnail stays valid). `force_ocr` re-rasterises pixels, so previews may drift.
- ⬜ Concurrency with paperless workers: the checksum-verify-before-replace step is the only
  guard against concurrent modification — acceptable for a single-operator instance.
- ✅ **Resolved** — `invalidate_digital_signatures: true` (paperless `PAPERLESS_OCR_USER_ARGS`)
  is mirrored via `REARCHIVE_OCR_USER_ARGS` in the sidecar runs.
- ✅ **Resolved** — archive size regression: the cause was a *silent fallback*, not
  `redo_ocr` itself. `ocrmypdf` rejects `--redo-ocr` together with `--deskew`; the runner's
  generic error-fallback then retried with `force_ocr`, which rasterises every page
  (measured 616 KiB → 4.6 MiB on a 72 dpi scan; doc 3723 112 KiB → 900 KiB). The runner now
  drops deskew when it selects `redo_ocr` (mirrors paperless-chandra `parser.py:395`) and logs
  a warning instead of silently inflating archives. `auto` never rasterises.
- ⬜ **Failed-scan detection**: Chandra `Detected repeat token, retrying generation` + GPU spin
  indicates the model looping on a bad/empty page — effectively a failed OCR. Pipeline should
  detect this (client callback / output repetition heuristic) and mark `-failure` instead of
  writing garbage content. Deferred until observed on more documents.
- ⬜ paperless-chandra is installed from git `master` (no release tags published upstream yet);
  pin a tag once available for reproducible builds.
- ⬜ `REARCHIVE_OCR_MODE=force` should only be used knowingly: it is the only mode that changes
  archive size dramatically.
