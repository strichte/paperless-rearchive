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
- **Archive filename**: *not* always `<id>.pdf`. `file_handling.generate_unique_filename(doc,
  archive_filename=True)` derives it from storage-path / `PAPERLESS_FILENAME_FORMAT` templates
  (or `<pk:07>.pdf` when no template). The sidecar must take the archive path from the API
  serializer field `archived_file_name` (relative to `ARCHIVE_DIR`) — never reconstruct it.
- **Born-digital PDFs**: in `auto` mode paperless skips OCR for PDFs with an existing text layer
  (`skip_text`, `pdf_born_digital_text`); `PAPERLESS_ARCHIVE_FILE_GENERATION`
  (`never`/`only`/`always`) decides whether an archive file is produced at all. Documents
  without an archive file ⇒ content-only mode in this sidecar.
- **Non-PDF originals** (jpg/png/tiff/…): paperless converts them to a PDF during consumption
  (`PAPERLESS_OCR_IMAGE_DPI`, A4 DPI fallback). The sidecar runs the same conversion via
  ocrmypdf but cannot regenerate an *existing* archive file (none exists) ⇒ content-only by
  default; `REARCHIVE_ARCHIVE_FOR_IMAGES=true` later opts into creating one (experimental).
- **API limits**: the paperless REST API cannot upload/replace an archive version of an existing

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
    CK -- "yes" --> OCR["ocrmypdf: force_ocr, output_type=pdfa,<br/>deskew/clean per config, plugins=provider"]
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
│   ├── paperless_api.py       # REST client
│   ├── pipeline.py            # per-document orchestration
│   ├── poller.py              # main loop (entry point)
│   ├── ocr/
│   │   ├── __init__.py
│   │   ├── base.py            # OcrProviderPlugin ABC + registry
│   │   ├── chandra.py         # Chandra provider
│   │   └── runner.py          # Django-free ocrmypdf argument builder + image helpers
│   └── archive/
│       ├── __init__.py
│       ├── db.py              # psycopg checksum update
│       └── replacer.py        # backup + atomic replace + sha256
├── docker/Dockerfile
├── pyproject.toml
└── tests/
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
- ✅ `ocr/runner.py` (force_ocr, pdfa, deskew/clean, image DPI/alpha handling, fallback retry)
- ✅ `pipeline.py` orchestration incl. dry-run + tag lifecycle

### Phase 4 — Archive replacement ✅
- ✅ `archive/db.py` (psycopg; fetch + update archive_checksum)
- ✅ `archive/replacer.py` (verify checksum, backup, atomic replace, sha256)
- ✅ DB password from secret file `paperless_db_paperless_passwd`

### Phase 5 — Deployment & tests 🔧
- ✅ Dockerfile (python3.12-slim + ghostscript/tesseract/qpdf/poppler + ocrmypdf + chandra-ocr)
- ✅ compose snippet + `.env.example`
- ✅ unit tests (12 passing: replacer, runner args, config)
- ✅ live smoke test (2026-09-15): dry-run cycle against the running instance
  (`http://localhost:8001`) — document 5488 tagged `re-ocr-content` + `re-ocr-all` was OCR'd
  end-to-end via the live Chandra server (30,706 chars markdown sidecar produced, nothing
  written, trigger tag kept). `re-ocr-all` archive path correctly halts when DB credentials are
  absent (dry-run never touches tags — enforced in `pipeline._finish` call sites).
- ⬜ full e2e in the container: build image, deploy via compose snippet, tag one
  `re-ocr-content` document and one throwaway `re-ocr-all` document; verify content + tags +
  archive replace + DB checksum update. Requires ghostscript/qpdf in the container (Dockerfile
  installs them; the host shell lacked them, so local dry runs need
  `REARCHIVE_OCR_OUTPUT_TYPE=pdf`).

  document (no archive upload endpoint; document upload creates *new* documents). Hence the
  bind-mount + direct DB update approach.
- **Content update**: `PATCH /api/documents/{id}/ { "content": "..." }` works (documents
  serializer exposes `content` as writable).
- **Live probe (2026-09-15, instance with 5352 docs)**: the documents serializer *does* expose
  `archived_file_name` (confirmed template-derived, e.g.
  `2026-09-15 Wisconsin ... .pdf`) but does **NOT** expose `archive_checksum` — the checksum for
  the verify-before-replace step is read directly from Postgres
  (`archive/db.py: fetch_archive_checksum`).



## 6. Configuration

Environment variables (prefix `REARCHIVE_` where generic; `PAPERLESS_CHANDRA_*` shared with the
paperless container for provider settings):

| Variable | Default | Description |
| --- | --- | --- |
| `PAPERLESS_BASE_URL` | `http://paperless:8000` | paperless-ngx base URL |
| `PAPERLESS_API_TOKEN` | *(required)* | API token (from `.env.paperless-gpt`) |
| `REARCHIVE_TRIGGER_TAG_CONTENT` | `re-ocr-content` | trigger tag, content-only mode |
| `REARCHIVE_TRIGGER_TAG_ALL` | `re-ocr-all` | trigger tag, archive + content mode |
| `REARCHIVE_SUCCESS_SUFFIX` | `-success` | success tag suffix |
| `REARCHIVE_FAILURE_SUFFIX` | `-failure` | failure tag suffix |
| `REARCHIVE_ARCHIVE_DIR` | `/archives` | bind-mounted `media/documents/archive` |
| `REARCHIVE_PROVIDER` | `chandra` | OCR provider plugin |
| `PAPERLESS_CHANDRA_SERVER_URL` | *(required)* | e.g. `http://ai:8110/v1` |
| `PAPERLESS_CHANDRA_MODEL_NAME` | `chandra` | e.g. `chandra-ocr-2-q8` |
| `PAPERLESS_CHANDRA_API_KEY_FILE` | *(optional)* | secret file with API key |
| `PAPERLESS_CHANDRA_CONTENT_FORMAT` | `markdown` | `markdown` or `text` |
| `PAPERLESS_CHANDRA_MAX_OUTPUT_TOKENS` | `12384` | per-page token budget |
| `REARCHIVE_OCR_LANGUAGE` | `eng` | passed through to ocrmypdf (labels hOCR) |
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
| `PAPERLESS_DBHOST` / `PAPERLESS_DBPORT` / `PAPERLESS_DBNAME` / `PAPERLESS_DBUSER` | `postgres` / `5432` / `paperless` / `paperless` | DB connection |
| `PAPERLESS_DBPASS_FILE` | *(required for re-ocr-all)* | secret file with DB password |

Secrets (already defined in `paperless-lxc/docker-compose.yml`): `chandra_api_key`,
`paperless_db_paperless_passwd`.

## 7. Risks / open questions

- ⬜ Verify `archived_file_name` serializer field name against the running instance's API
  (`/api/documents/{id}/`) during Phase 5 e2e.
- ⬜ Full-text index / search index is updated by paperless on content PATCH (serializers post
  save) — confirm search reflects new content after e2e.
- ⬜ Thumbnails are not regenerated (archive pixel content is unchanged, so the existing
  thumbnail stays valid).
- ⬜ Concurrency with paperless workers: the checksum-verify-before-replace step is the only
  guard against concurrent modification — acceptable for a single-operator instance.
- ⬜ `invalidate_digital_signatures: true` (paperless `PAPERLESS_OCR_USER_ARGS`) must be
  mirrored in sidecar runs for signed PDFs.
