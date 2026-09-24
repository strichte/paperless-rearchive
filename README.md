# paperless-rearchive <!-- omit from toc -->

A tag-driven sidecar for [paperless-ngx](https://docs.paperless-ngx.com) that **re-OCRs documents
already in your library** with [Chandra](https://github.com/datalab-to/chandra) (an LLM vision
OCR model) and optionally regenerates the *archive* (searchable PDF/A) with the new OCR layer the same way
paperless-ngx does at ingest.

- Tag a document `re-ocr-content` → its `content` field is replaced with fresh OCR markdown.
- Tag it `re-ocr-all` → content **plus** the archive file is regenerated.
- The sidecar polls paperless for these tags and processes tagged documents in the background.

## Why not just paperless-ngx?<!-- omit from toc -->

paperless-ngx *can* re-run OCR on existing documents — the UI **Reprocess** action,
`POST /api/documents/reprocess/`, and `document_archiver --overwrite --document <id>` all
exist. For a real re-OCR campaign they fall short:

- **Invisible text layers are skipped by default.** Reprocess honors the global
  `PAPERLESS_OCR_MODE`; under the default `auto`, any PDF with ≥50 characters of extractable
  text is treated as "already has text" and OCR is skipped (`--skip-text`). Scans carrying
  an old, invisible OCR overlay — typical for libraries imported from earlier tools — are
  silently left alone. Getting past that means flipping the *global* mode to `redo`,
  restarting, reprocessing, then flipping it back — which also changes how every future
  ingest is handled ([#6289](https://github.com/paperless-ngx/paperless-ngx/issues/6289)).
- **The born-digital test is document-level.** A scanned page with an invisible overlay, or
  a mixed native/scan document, is judged all-or-nothing — no per-page routing (see
  [OCR strategy](#ocr-strategy)).
- **Only paperless's configured OCR engine.** There is no way to say "re-OCR *these*
  documents with Chandra, via a plain OpenAI-compatible endpoint".
- **No safety net for bulk replacement.** No dry-run, no unconditional backups, no one-shot
  restore — exactly what you want before anything rewrites archive files at scale.

This sidecar is the tag-driven, per-page-provenance, dry-run-and-backup way to do it.

## Is this tool for you?<!-- omit from toc -->

Re-OCR rewrites `content` and *replaces archive files*. Check every item:

- **Docker compose paperless.** The sidecar is a compose service next to `paperless` + `postgres`.
- **Write access to the archive dir.** Mount `media/documents/archive/` at `/archive`
  read-write; run as the UID:GID that owns the files. Originals are never touched (API
  download only).
- **Paperless API token.** Paperless *Admin → Documents → Tokens*.
- **PostgreSQL** (`re-ocr-all` only). SQLite/MariaDB: `re-ocr-content` works, `re-ocr-all`
  cannot. Reuse paperless's `PAPERLESS_DB*` credentials. Content-only runs never open a DB
  connection.
- **Local Chandra LLM server.** Same server as [paperless-chandra](https://github.com/flobernd/paperless-chandra) ingest — see [`doc/LLM-SERVER.md`](doc/LLM-SERVER.md) for setup.
- **Backup mount.** Every replaced archive is copied to `/archive-backups` first. Mount it
  outside the archive tree (separate bind mount, may be another disk).
- **No undo button.** Changes are recorded (audit note, provenance fields, `.bak` copies) — still,
  start with `REARCHIVE_DRY_RUN=true` and one test doc. Mistakes are reversible (with caveats) via
  [`restore_backup`](#restoring-from-backups-restore_backup).

Only need Chandra for **new** documents? Run
[paperless-chandra](https://github.com/flobernd/paperless-chandra) directly — this project is for
what's already in your library.

## Table of Content<!-- omit from toc -->
* [Quickstart](#quickstart)
* [Trigger tags](#trigger-tags)
* [Safety](#safety)
* [OCR strategy](#ocr-strategy)
  * [Why a separate provenance test?](#why-a-separate-provenance-test)
* [Full setup (docker compose)](#full-setup-docker-compose)
  * [Chandra LLM server setup](#chandra-llm-server-setup)
  * [Get the code next to your compose file](#get-the-code-next-to-your-compose-file)
  * [Add the service to `docker-compose.yml`](#add-the-service-to-docker-composeyml)
    * [Secrets](#secrets)
      * [Way 1: plain values](#way-1-plain-values)
      * [Way 2: `.env` file](#way-2-env-file)
      * [Way 3: compose `secrets:`](#way-3-compose-secrets)
  * [Build and start](#build-and-start)
  * [First test](#first-test)
* [Configuration reference](#configuration-reference)
  * [Paperless connection](#paperless-connection)
  * [Chandra inference server](#chandra-inference-server)
  * [OCR behaviour (mirrors paperless's `PAPERLESS_OCR_*`)](#ocr-behaviour-mirrors-paperlesss-paperless_ocr_)
  * [Tags, loop and safety](#tags-loop-and-safety)
  * [Database (re-ocr-all only)](#database-re-ocr-all-only)
* [OCR provenance](#ocr-provenance)
* [Manual trigger](#manual-trigger)
* [Backups](#backups)
* [Restoring from backups (`restore_backup`)](#restoring-from-backups-restore_backup)
* [License](#license)

## Quickstart

A minimal service `docker-compose.yml`. Only required settings — everything else runs on defaults (see [Configuration reference](#configuration-reference)).

```yaml
services:
  paperless-rearchive:
    build:
      context: ./paperless-rearchive
      dockerfile: docker/Dockerfile
    image: paperless-rearchive:local
    user: "1000:1000"   # UID:GID that owns the archive files (same as paperless USERMAP_UID/GID)
    environment:
      PAPERLESS_API_TOKEN: "<paperless-api-token>"          # Admin -> Documents -> Tokens
      PAPERLESS_CHANDRA_SERVER_URL: "http://my-ai.local:8000/v1"  # see Chandra server below
      PAPERLESS_CHANDRA_MODEL_NAME: "chandra-ocr-2-q8"      # must match server's --served-model-name
      PAPERLESS_DBPASS: "<postgres-password>"               # re-ocr-all only; omit for content-only
    volumes:
      - /data/paperless/media/documents/archive:/archive         # read-write
      - /data/paperless/archive-backups:/archive-backups         # required, outside the archive tree
```

Truly required: `PAPERLESS_API_TOKEN` (poller exits without it) and
`PAPERLESS_CHANDRA_SERVER_URL` (provider `validate()` fails without it). `PAPERLESS_DBPASS`
only for `re-ocr-all` — content-only never opens a DB connection. `PAPERLESS_CHANDRA_MODEL_NAME`
defaults to `chandra`, so you can omit it when the server is started with
`--served-model-name=chandra` — but the value **must** match a name the server advertises. A
mismatch is caught once per process against the server's `GET /v1/models` list and aborts the
poll cycle immediately — naming the requested model and listing the models the server actually
serves — instead of retrying the same `model not found` on every page (and then escalating each
document after three strikes). Fix the env var and redeploy; the poller resumes on the next cycle.

Steps:

- Clone the repo next to your compose file: `git clone https://github.com/strichte/paperless-rearchive.git`
- Adjust the placeholders above (token, server URL, model name, DB password) and the two host paths.
- `docker compose build paperless-rearchive && docker compose up -d paperless-rearchive`
- Tag one disposable test document `re-ocr-content`, wait a cycle, check for
  `re-ocr-content-success`. For an immediate cycle: `docker kill -s HUP paperless-rearchive`.
- Start real runs with `REARCHIVE_DRY_RUN: "true"` first for a no-write rehearsal.


## Trigger tags

| Tag | Effect |
| --- | --- |
| `re-ocr-content` | Replace `content` with fresh OCR markdown. No archive, no DB. |
| `re-ocr-all` | As above, **plus** regenerate the archive (same ocrmypdf pipeline as ingest) and update `archive_checksum` in the DB. |
| `re-ocr-force` | Modifier, not a trigger: add next to one of the above to force OCR of every page. Never auto-created. |

- Tag a doc and wait (or `docker kill -s HUP
  paperless-rearchive`).
- After processing the trigger is swapped for `<trigger>-success` or `<trigger>-failure`.
- Extra tags: `re-ocr-preserved` (born-digital, left untouched), `re-ocr-detection-unknown`
  (could not classify, processed anyway), `re-ocr-page-errors` (content mode, some pages failed).
- Transient errors (server down, network hiccup) keep the trigger tag — documents self-heal next
  cycle. After **3 consecutive failures** a doc is escalated to `<trigger>-failure` with an audit
  note. An inference-server **outage is not counted**: with documents queued, the sidecar probes
  the server once per cycle and aborts immediately when it cannot be reached — no document is
  attempted, no failure is recorded, nothing escalates — and the next cycle waits the full poll
  interval, so an outage neither hammers the network nor mis-tags healthy documents.

## Safety

- **Originals immutable.** Downloaded via `?original=true` into a scratch dir, used as OCR source.
- **Born-digital PDFs preserved.** Classified page by page (pdf-inspector) before any OCR run;
  all-native docs are left completely untouched (`-success` + `re-ocr-preserved` + note).
- **Atomic archive replace.** Temp file → `os.replace`, checksum verified before (never races a
  concurrent write), checksum updated after. Non-PDF originals and docs without an archive fall
  through to content-only.
- **Backups unconditional.** Every replacement copies the old archive to `/archive-backups`
  (layout mirrored). Startup creates the dir, verifies writability, and refuses to run when the
  mounts overlap. Undo via [`restore_backup`](#restoring-from-backups-restore_backup).
- **Dry run.** `REARCHIVE_DRY_RUN=true` does the full OCR run, writes nothing (no PATCH, no file,
  no DB, no tag swaps).

## OCR strategy

`ocrmypdf` decides what happens to a PDF that already has a text layer. The sidecar adds a
provenance gate in front:

| provenance | `re-ocr-content` | `re-ocr-all` |
| --- | --- | --- |
| `text_based` (all pages native) | content left untouched — no OCR run | archive left untouched — no ocrmypdf pass |
| `scanned` (all pages OCR candidates) | Chandra OCRs every page | `--redo-ocr` (or `force`/`off` per mode); archive replaced |
| `mixed` (some of each) | Chandra OCRs only the scan pages; native pages keep their own text | `--skip-text`: native pages keep their text, textless pages get OCR |

- `REARCHIVE_OCR_MODE` (default `redo`) applies only to pages routed to OCR. Mixed docs always
  use `--skip-text` (ocrmypdf applies one mode per file; `--redo-ocr` would strip native pages).
  `force` re-rasterises everything (much larger archives) and bypasses the gate — prefer the
  per-doc `re-ocr-force` tag.
- Quality knobs mirror paperless: `ROTATE_PAGES` (local Tesseract OSD, no GPU), `DESKEW`
  (skipped under `redo`, ocrmypdf constraint), `CLEAN`, `OUTPUT_TYPE`, `OCR_USER_ARGS` (JSON,
  merged last).
- Archive mode fails the whole doc on an un-OCRable page; `re-ocr-page-errors` only appears on
  content runs.

### Why a separate provenance test?

Paperless-ngx's own born-digital check (`pdf_born_digital_text` + `has_visible_text_content`)
is document-level and binary: it answers *"does this PDF have a text layer?"* but cannot tell a
scanned page with an invisible OCR overlay from a genuinely born-digital page, and it cannot split
a mixed document page by page. The result: a scanned document carrying an OCR overlay is mistakenly
treated as born-digital and skipped; a document mixing native and scanned pages is treated as all
one thing.

`REARCHIVE_PDF_PROVENANCE=on` (default) replaces that with pdf-inspector's per-page
`extract_pages_markdown`: each page is classified individually with a correct `needs_ocr` flag,
and pages that are native keep their own markdown (better-than-Chandra content, free). This is the
more robust test the re-OCR tool exists to provide — it is what distinguishes a re-OCR run from
paperless-ngx's ingestion pipeline.

`REARCHIVE_PDF_PROVENANCE=off` falls back to paperless-ngx's own document-level heuristics (the
same check ingest uses). It behaves like a paperless-ngx ingestion pipeline: the
`REARCHIVE_OCR_MODE` knob is the sole policy, no per-page routing, no native markdown extraction.
Use it when you want behaviour parity with ingest or when pdf-inspector is unavailable.

## Full setup (docker compose)

### Chandra LLM server setup

All inference happens on a self-hosted server with an OpenAI-compatible endpoint; the sidecar itself is CPU-only. Any server exposing `/v1/chat/completions` works — [vLLM](https://github.com/vllm-project/vllm) is the reference, and the same server you would use for [paperless-chandra](https://github.com/flobernd/paperless-chandra) ingest works as-is.

Setting one up (model downloads, `llama-swap` or vLLM compose files, benchmarks, wiring the sidecar to it) is documented in **[`doc/LLM-SERVER.md`](doc/LLM-SERVER.md)**. In short:

- Serve a Chandra model under a name of your choice, e.g. `chandra-ocr-2-q8`.
- Set `PAPERLESS_CHANDRA_SERVER_URL` (e.g. `http://my-ai.local:8000/v1`) and `PAPERLESS_CHANDRA_MODEL_NAME` to that name on the sidecar — see [Chandra inference server](#chandra-inference-server).
- Set `PAPERLESS_CHANDRA_API_KEY` when the server requires a key (both documented setups do).

**Note:** Datalab's [Chandra OCR 2 model](https://github.com/datalab-to/chandra) uses a dual licensing structure: the source code is licensed under Apache-2.0, while the model weights are governed by a modified OpenRAIL-M license.

* **License Breakdown:**
  * Code License: Apache-2.0 for the repository's codebase.
  * Model Weights License: Modified OpenRAIL-M.
* **Usage Terms & Free Tier:**
  * Free Use: Free for research, personal use, and startups with under $2 million in funding or revenue.
  * Restrictions: Cannot be used to compete directly with Datalab's API services.
  * Commercial License: Required for larger organizations, companies with over $2M in revenue/funding, or high-volume/on-prem enterprise needs. You can obtain a commercial agreement through the Datalab Pricing page.

Same compose file as paperless-ngx (shared network, Postgres, archive dir). Adjust `paperless` /
`postgres` / server URL to your setup.

### Get the code next to your compose file

```bash
cd /opt/paperless            # wherever your paperless-ngx docker-compose.yml lives
git clone https://github.com/strichte/paperless-rearchive.git
```

### Add the service to `docker-compose.yml`

Abbreviated but complete: postgres, valkey/redis, tika, gotenberg, paperless (with Chandra plugin
for ingest) plus `paperless-rearchive`. Host paths `/data/paperless/...`, UID/GID `1000` are
placeholders. Chandra server runs elsewhere (`http://my-ai.local:8000/v1` here) — or add one to
the same file, see [`doc/LLM-SERVER.md`](doc/LLM-SERVER.md).

```yaml
networks:
  frontend:
  backend:

secrets:
  paperless_db_paperless_passwd:
    file: ./secrets/paperless_db_paperless_passwd   # same file paperless uses
  paperless_secret_key:
    file: ./secrets/paperless_secret_key
  chandra_api_key:
    file: ./secrets/chandra_api_key                 # omit if server needs no auth
  paperless_api_token:
    file: ./secrets/paperless_api_token

services:
  # ─── PostgreSQL ────────────────────────────────────────────────────────────
  postgres:
    image: postgres:17
    container_name: postgres
    restart: unless-stopped
    networks: [backend]
    volumes:
      - /data/paperless/pgdata:/var/lib/postgresql/data
    environment:
      POSTGRES_DB: paperless
      POSTGRES_USER: paperless
      POSTGRES_PASSWORD_FILE: /run/secrets/paperless_db_paperless_passwd
    secrets: [paperless_db_paperless_passwd]

  # ─── Valkey (or redis) — paperless task queue / broker ─────────────────────
  valkey:
    image: valkey/valkey:8
    container_name: valkey
    restart: unless-stopped
    networks: [backend]

  # ─── Gotenberg — PDF/A conversion, office→PDF for paperless ────────────────
  gotenberg:
    image: gotenberg/gotenberg:8
    container_name: gotenberg
    restart: unless-stopped
    networks: [backend]
    command: ["gotenberg", "--chromium-disable-javascript=true"]

  # ─── Tika — text/metadata extraction for non-PDF documents ─────────────────
  tika:
    image: apache/tika:latest
    container_name: tika
    restart: unless-stopped
    networks: [backend]

  # ─── paperless-ngx (abbreviated — keep your existing settings) ─────────────
  paperless:
    # paperless-ngx with the Chandra OCR plugin wired in at ingest, built from
    # paperless-chandra's example Dockerfile. (With the stock paperless-ngx
    # image the PAPERLESS_CHANDRA_* options below would be ignored.)
    build:
      context: ./paperless-chandra/examples
      dockerfile: Dockerfile
      args:
        PLUGIN_REF: ${PLUGIN_REF:-master}     # paperless-chandra plugin ref
        PAPERLESS_TAG: ${PAPERLESS_TAG:-3.1.3}
    image: paperless
    container_name: paperless
    restart: unless-stopped
    networks: [frontend, backend]
    ports: ["8000:8000"]
    depends_on: [postgres, valkey, gotenberg, tika]
    volumes:
      - /data/paperless/data:/usr/src/paperless/data
      - /data/paperless/media:/usr/src/paperless/media
      - /data/paperless/consume:/usr/src/paperless/consume
    environment:
      USERMAP_UID: "1000"                # must match paperless-rearchive's user:
      USERMAP_GID: "1000"
      PAPERLESS_REDIS: redis://valkey:6379
      PAPERLESS_DBHOST: postgres
      PAPERLESS_DBNAME: paperless
      PAPERLESS_DBUSER: paperless
      PAPERLESS_DBPASS_FILE: /run/secrets/paperless_db_paperless_passwd
      PAPERLESS_SECRET_KEY_FILE: /run/secrets/paperless_secret_key
      PAPERLESS_TIKA_ENABLED: "1"
      PAPERLESS_TIKA_ENDPOINT: http://tika:9998
      PAPERLESS_TIKA_GOTENBERG_ENDPOINT: http://gotenberg:3000
      # Chandra at ingest (requires the paperless-chandra build above):
      PAPERLESS_CHANDRA_SERVER_URL: http://my-ai.local:8000/v1
      PAPERLESS_CHANDRA_MODEL_NAME: chandra-ocr-2-q8
      PAPERLESS_CHANDRA_API_KEY_FILE: /run/secrets/chandra_api_key
    secrets:
      - paperless_db_paperless_passwd
      - paperless_secret_key
      - chandra_api_key

  # ─── paperless-rearchive — tag-driven re-OCR sidecar ───────────────────────
  paperless-rearchive:
    # Built from the checked-out source (the repo this compose file lives in).
    # The version-pinned image tag makes rollback to a previous release a
    # re-tag + `up -d` away.
    build:
      context: ./paperless-rearchive
      dockerfile: docker/Dockerfile
      args:
        CHANDRA_REF: master   # pin a paperless-chandra SHA for reproducibility
        UID: "1000"           # create the image's rearchive user with this
        GID: "1000"           # UID/GID — same pair as user: below and
                              # paperless's USERMAP_UID/GID
    image: paperless-rearchive:v0.1.0
    container_name: paperless-rearchive
    restart: unless-stopped
    # MUST match the build args above and the UID:GID that owns the files in
    # paperless's archive directory — i.e. the same value as paperless's
    # USERMAP_UID/GID.
    user: "1000:1000"
    networks: [backend]
    depends_on:
      - paperless
    environment:
      # ---- required: where to find things -----------------------------------
      PAPERLESS_BASE_URL: "http://paperless:8000"     # paperless web UI/API
      PAPERLESS_API_TOKEN_FILE: /run/secrets/paperless_api_token
      # Chandra inference server (OpenAI-compatible /v1 API):
      PAPERLESS_CHANDRA_SERVER_URL: "http://my-ai.local:8000/v1"
      PAPERLESS_CHANDRA_MODEL_NAME: "chandra-ocr-2-q8"
      PAPERLESS_CHANDRA_API_KEY_FILE: /run/secrets/chandra_api_key
      # Database (re-ocr-all only) - copy from paperless's own environment:
      PAPERLESS_DBHOST: "postgres"
      PAPERLESS_DBPORT: "5432"
      PAPERLESS_DBNAME: "paperless"
      PAPERLESS_DBUSER: "paperless"
      PAPERLESS_DBPASS_FILE: /run/secrets/paperless_db_paperless_passwd
      # ---- optional, safe defaults shown; details in the reference below ----
      REARCHIVE_PROVIDER: "chandra"
      REARCHIVE_TRIGGER_TAG_CONTENT: "re-ocr-content"
      REARCHIVE_TRIGGER_TAG_ALL: "re-ocr-all"
      REARCHIVE_SUCCESS_SUFFIX: "-success"
      REARCHIVE_FAILURE_SUFFIX: "-failure"
      REARCHIVE_POLL_INTERVAL: "300"       # seconds between tag polls
      REARCHIVE_BATCH_LIMIT: "5"           # docs processed per cycle
      REARCHIVE_WRITE_PROVENANCE: "true"   # custom fields + audit notes
      REARCHIVE_DRY_RUN: "false"
      REARCHIVE_RUN_ONCE: "false"
      REARCHIVE_LOG_LEVEL: "INFO"
      REARCHIVE_MAX_PAGES: "0"             # 0 = all pages
      REARCHIVE_OCR_CONCURRENCY: "1"       # parallel pages (see reference)
      # --- OCR behaviour (mirrors PAPERLESS_OCR_*; see "OCR strategy") -------
      REARCHIVE_OCR_MODE: "redo"           # redo | auto | force | off | skip
      REARCHIVE_PDF_PROVENANCE: "on"     # born-digital gate: on (pdf-inspector) | off (paperless-ngx heuristics)
      REARCHIVE_SKIP_BORN_DIGITAL: "true"  # never re-OCR born-digital PDFs
      REARCHIVE_PRESERVED_TAG: "re-ocr-preserved"
      REARCHIVE_OCR_MIXED_MODE: "skip"     # archive mode for mixed provenance
      REARCHIVE_FORCE_TAG: "re-ocr-force"  # modifier tag: force OCR for one document
      REARCHIVE_OCR_CLEAN: "clean"         # clean | final | none
      REARCHIVE_OCR_DESKEW: "true"
      REARCHIVE_OCR_ROTATE_PAGES: "true"
      REARCHIVE_OCR_ROTATE_PAGES_THRESHOLD: "12.0"
      REARCHIVE_OCR_OUTPUT_TYPE: "pdfa"
      REARCHIVE_OCR_LANGUAGE: "eng"        # label only; Chandra is language-agnostic
      REARCHIVE_OCR_DPI: "300"             # content-only render DPI
      # REARCHIVE_OCR_USER_ARGS: '{"invalidate_digital_signatures": true, "continue_on_soft_render_error": true}'
      # REARCHIVE_ARCHIVE_FOR_IMAGES: "false"   # reserved; images are content-only today
      PAPERLESS_CHANDRA_CONTENT_FORMAT: "markdown"
      PAPERLESS_CHANDRA_MAX_OUTPUT_TOKENS: "12384"
    volumes:
      # Read-write: archive files are replaced here. Use the SAME host path
      # your paperless container mounts at media/documents/archive.
      - /data/paperless/media/documents/archive:/archive
      # Required: where .bak backups of replaced archives are kept. May be a
      # different disk than the archive mount (backups are copied).
      - /data/paperless/archive-backups:/archive-backups
    secrets:
      - chandra_api_key
      - paperless_db_paperless_passwd
      - paperless_api_token
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

> **Find your archive path:** in paperless's compose service it is the volume mounted at
> `/usr/src/paperless/media` — the archive directory is `…/media/documents/archive` on the host.
> Mount exactly that directory (not its parent) read-write.


#### Secrets

Three credentials: paperless API token, Chandra API key, DB password (optionally DB user).
Pick one way per credential — or mix:

##### Way 1: plain values

```yaml
services:
  paperless-rearchive:
    environment:
      PAPERLESS_API_TOKEN: "paste-token-here"
      PAPERLESS_CHANDRA_API_KEY: "paste-key-here"   # omit if no auth needed
      PAPERLESS_DBPASS: "postgres-password"
```

- Simplest for single-operator instances. Not recommended practice.
- Also works via `env_file:` (same `KEY=value` lines).

##### Way 2: `.env` file

See [`doc/deploy/env.example`](doc/deploy/env.example).

##### Way 3: compose `secrets:`

Works for all four: `PAPERLESS_API_TOKEN`, `PAPERLESS_CHANDRA_API_KEY`, `PAPERLESS_DBPASS`,
  `PAPERLESS_DBUSER`. Put the value only in the corresponding secret file, i.e. `./secrets/paperless_db_paperless_passwd` would only contain your password `super-secret-password`.

```yaml
secrets:
  paperless_db_paperless_passwd:
    file: ./secrets/paperless_db_paperless_passwd
  paperless_secret_key:
    file: ./secrets/paperless_secret_key
  chandra_api_key:
    file: ./secrets/chandra_api_key

services:
  paperless-rearchive:
    secrets:
      - paperless_api_token
      - chandra_api_key
      - paperless_db_paperless_passwd
    environment:
      PAPERLESS_API_TOKEN_FILE: /run/secrets/paperless_api_token
      PAPERLESS_CHANDRA_API_KEY_FILE: /run/secrets/chandra_api_key
      PAPERLESS_DBPASS_FILE: /run/secrets/paperless_db_paperless_passwd
```
Files mounted at `/run/secrets/<name>`. Keeps values out of
  `docker inspect`. This is what [Full setup](#full-setup-docker-compose) uses — see
  [`doc/deploy/compose-snippet.yml`](doc/deploy/compose-snippet.yml).

### Build and start

```bash
docker compose build paperless-rearchive
docker compose up -d paperless-rearchive
docker compose logs -f paperless-rearchive   # watch the first cycles
```

### First test

1. Set `REARCHIVE_DRY_RUN: "true"` first if you want a no-write rehearsal.
2. Tag one **disposable test document** `re-ocr-all` in the paperless UI.
3. Wait for a poll cycle (or `docker kill -s HUP paperless-rearchive`).
4. Check the outcome: the document should carry `re-ocr-all-success`, its `content` field fresh
   markdown, and an audit note; the archive file's `Creator` metadata should read
   `OCRmyPDF … / Chandra …`. The old archive is kept as `.bak-<timestamp>` in
   `/archive-backups`.

## Configuration reference

Booleans accept `true/false/yes/on/1`. Mounts are fixed: `/archive` (paperless
`media/documents/archive`, read-write) and `/archive-backups` (outside the archive tree).
`REARCHIVE_ARCHIVE_DIR` only exists to override `/archive` if the mount differs.

### Paperless connection

| Variable | Default | Purpose |
| --- | --- | --- |
| `PAPERLESS_BASE_URL` | `http://paperless:8000` | Base URL of the paperless web server (REST API is `<url>/api/`). Use the *service name* from your compose file, not `localhost`. |
| `PAPERLESS_API_TOKEN` / `PAPERLESS_API_TOKEN_FILE` | *(required)* | API token created in paperless (Admin → Documents → Tokens). Needs read access to documents, write access to content/tags/notes/custom fields. |

### Chandra inference server

| Variable | Default | Purpose |
| --- | --- | --- |
| `PAPERLESS_CHANDRA_SERVER_URL` | *(required)* | OpenAI-compatible server hosting Chandra, e.g. `http://chandra-server:8000` or `http://ai:8110/v1`. |
| `PAPERLESS_CHANDRA_MODEL_NAME` | `chandra` | The model name the server advertises. Checked against `GET /v1/models` once per process; a mismatch aborts the poll cycle (no document is modified or escalated). |
| `PAPERLESS_CHANDRA_API_KEY` / `…_FILE` | *(empty)* | Bearer token for the server; leave unset if the server needs no auth. |
| `PAPERLESS_CHANDRA_CONTENT_FORMAT` | `markdown` | Format of the OCR text stored in paperless: `markdown` (recommended) or `text`. |
| `PAPERLESS_CHANDRA_MAX_OUTPUT_TOKENS` | `12384` | Per-page output token budget for the model. |

### OCR behaviour (mirrors paperless's `PAPERLESS_OCR_*`)

| Variable | Default | Purpose |
| --- | --- | --- |
| `REARCHIVE_PROVIDER` | `chandra` | OCR provider plugin. Only `chandra` exists today. |
| `REARCHIVE_OCR_MODE` | `redo` | How existing text on OCR-routed pages is treated — see [OCR strategy](#ocr-strategy). `redo`/`auto`/`force`/`off`/`skip` (legacy `skip`/`skip_noarchive` = `auto`). |
| `REARCHIVE_PDF_PROVENANCE` | `on` | Per-page born-digital detection (pdf-inspector): `on` classifies each PDF page individually and routes only scan pages to OCR (`text_based` docs skipped entirely); `off` falls back to paperless-ngx's own document-level heuristics and behaves like the ingestion pipeline (mode-driven, no per-page routing). See [Why a separate provenance test?](#why-a-separate-provenance-test) and [OCR strategy](#ocr-strategy). |
| `REARCHIVE_SKIP_BORN_DIGITAL` | `true` | Works with `REARCHIVE_PDF_PROVENANCE` (both `on` and `off`): when a document is classified `text_based` (all native), `true` skips it entirely (no OCR, no PATCH, no archive write; tagged `re-ocr-preserved`); `false` runs OCR anyway using `REARCHIVE_OCR_MODE` (useful when you want Chandra markdown over native text, or suspect the classifier). |
| `REARCHIVE_PRESERVED_TAG` | `re-ocr-preserved` | Tag added alongside `-success` when native text was preserved. Empty disables. |
| `REARCHIVE_OCR_MIXED_MODE` | `skip` | ocrmypdf mode for mixed docs (`skip`/`redo`/`force`). `skip` = `--skip-text`, keeps native pages. |
| `REARCHIVE_PROVENANCE_MAX_PAGES` | `0` | Pages inspected by the provenance classifier; `0` = all. |
| `REARCHIVE_FORCE_TAG` | `re-ocr-force` | Modifier tag (never auto-created) to force OCR of every page. Empty disables. |
| `re-ocr-skipped` (auto tag) | — | Marker applied when the original is not OCR-able (Office documents etc.): digital-born, not PDF, text extracted at ingest - nothing to re-run. Trigger swaps to `<trigger>-success`. Raster-image originals (JPG/PNG/TIFF) are OCR-able and take the normal content path. |
| `REARCHIVE_OCR_CLEAN` | `clean` | Image cleaning: `clean` (pre-OCR), `final` (becomes pre-OCR under `redo`), `none`. |
| `REARCHIVE_OCR_DESKEW` | `true` | Fix small skew. Ineffective under `redo` - which includes the mixed-provenance `--pages` path (ocrmypdf constraint: redo never re-renders page images; the run logs when it drops deskew). Applies to textless pages under `auto`/`skip` and to all pages under `force`. |
| `REARCHIVE_OCR_ROTATE_PAGES` | `true` | Fix page orientation. Detection: local Tesseract OSD (no GPU). |
| `REARCHIVE_OCR_ROTATE_PAGES_THRESHOLD` | `12.0` | Rotation confidence threshold; lower = more aggressive. |
| `REARCHIVE_OCR_OUTPUT_TYPE` | `pdfa` | Archive flavour (`pdfa`, `pdfa-1/2/3`, `pdf`), like `PAPERLESS_OCR_OUTPUT_TYPE`. |
| `REARCHIVE_OCR_LANGUAGE` | `eng` | Label passed to ocrmypdf (e.g. `eng+deu`). Chandra is language-agnostic. |
| `REARCHIVE_OCR_USER_ARGS` | *(unset)* | JSON ocrmypdf kwargs, merged **last** (same escape hatch as `PAPERLESS_OCR_USER_ARGS`). |
| `REARCHIVE_OCR_DPI` | `300` | Render DPI for the content-only fast path. Archive runs rasterise inside ocrmypdf. |
| `REARCHIVE_MAX_PAGES` | `0` | `0` = all pages; `N` = first N pages only. |
| `REARCHIVE_OCR_CONCURRENCY` | `1` | Concurrent pages per doc. GPU-bound: raise gradually (2, 4), watch the GPU. |
| `REARCHIVE_ARCHIVE_FOR_IMAGES` | `false` | Reserved. Non-PDF originals are always handled content-only today. |

### Tags, loop and safety

| Variable | Default | Purpose |
| --- | --- | --- |
| `REARCHIVE_TRIGGER_TAG_CONTENT` | `re-ocr-content` | Trigger tag for content-only runs (auto-created). |
| `REARCHIVE_TRIGGER_TAG_ALL` | `re-ocr-all` | Trigger tag for content+archive runs (auto-created). |
| `REARCHIVE_SUCCESS_SUFFIX` | `-success` | Outcome tag suffix appended to the trigger tag name on success. |
| `REARCHIVE_FAILURE_SUFFIX` | `-failure` | Outcome tag suffix on failure. |
| `REARCHIVE_POLL_INTERVAL` | `300` | Seconds between polls when idle. While draining, ~10 s cycles; exponential backoff on no-progress. |
| `REARCHIVE_BATCH_LIMIT` | `5` | Docs per cycle (rest stay tagged for next cycle). |
| `REARCHIVE_ARCHIVE_DIR` | `/archive` | Override only if the archive mount differs from `/archive`. |
| `REARCHIVE_WRITE_PROVENANCE` | `true` | Custom fields + audit note per run. Skipped in dry-run. |
| `REARCHIVE_DRY_RUN` | `false` | Full OCR run, nothing written (no PATCH, file, DB, or tag changes). |
| `REARCHIVE_RUN_ONCE` | `false` | Exit after one cycle (cron/CI). |
| `REARCHIVE_LOG_LEVEL` | `INFO` | `DEBUG` shows ocrmypdf args (key masked). |

### Database (re-ocr-all only)

- **PostgreSQL only.** SQLite: content-only works, `re-ocr-all` cannot. Copy from paperless's env.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PAPERLESS_DBHOST` | `postgres` | Postgres host (compose service name). |
| `PAPERLESS_DBPORT` | `5432` | Postgres port. |
| `PAPERLESS_DBNAME` | `paperless` | DB name. |
| `PAPERLESS_DBUSER` / `…_FILE` | `paperless` | DB user. |
| `PAPERLESS_DBPASS` / `…_FILE` | *(required for re-ocr-all)* | DB password. |

## OCR provenance

Per success the sidecar writes [custom fields](https://docs.paperless-ngx.com/usage/#custom-fields)
(auto-created) plus an audit note:

| Field | Type | Example | Written when |
| --- | --- | --- | --- |
| `OCR engine` | string | `chandra-ocr-2-q8` | every success |
| `OCR date` | date | `2026-09-17` | every success |
| `OCR pages` | string | `4/4 ok` | every success |
| `OCR archive ratio` | float | `1.001` | `re-ocr-all` only |

Field definitions are auto-created. Custom fields keep latest state; the audit note is
append-only (engine, pages, archive ratio, duration; `Re-OCR failed (…)` on failure). Skipped in
dry-run and when `REARCHIVE_WRITE_PROVENANCE=false`; provenance never fails the doc.

- **Baked into the archive:** regenerated PDFs carry `Creator: OCRmyPDF … + Chandra … [model:
  <name>]` (visible via `pdfinfo`). Check with
  `tests/integration/check_archive_provenance.sh`.

## Manual trigger

- Poll interval `REARCHIVE_POLL_INTERVAL` (default 300 s). Immediate cycle:
  `docker kill -s HUP paperless-rearchive`. In-flight runs are not interrupted; a signal during a
  cycle triggers the next one immediately after.

## Backups

- Before replacement the old archive is copied to `/archive-backups` as
  `<name>.bak-<timestamp>` (sub-dir layout mirrored). Unconditional — no skip option.
- Mount it outside the archive tree (separate bind mount, may be another disk) or paperless
  reports backups as orphaned files:
  `[WARNING] [paperless.sanity_checker] Orphaned file in media dir: …`.
- Startup creates the dir, checks writability, and refuses to run when mounts overlap; the
  replacer has the same runtime backstop.

```yaml
services:
  paperless-rearchive:
    volumes:
      - /opt/paperless/media/documents/archive:/archive
      - /opt/paperless/archive-backups:/archive-backups
```

## Restoring from backups (`restore_backup`)

Every replacement leaves a `.bak-<timestamp>` copy in `/archive-backups`. Restore archive and/or
`content` from inside the container (has both mounts + credentials):

```bash
docker exec -it paperless-rearchive restore_backup [-a] [-c] [-f] [-v] DOC_ID|BACKUP [...]
```

- Flags: none = both; `-a` archive only; `-c` content only; `-f` no confirm; `-v` version.
- Operands: doc IDs or backup names/globs (matched recursively below `/archive-backups`).
  Multiple matches ask which version.
- Archive: copied over `/archive/<archive_filename>` (atomic), checksum updated in DB.
- Content: re-extracted from the backup via `pdftotext`, normalised like paperless ingest, PATCHed
  back (may differ from the pre-re-OCR text depending on ingest settings).
- Cleanup on every restore: OCR provenance custom fields (`OCR engine/date/pages/archive ratio`)
  are wiped (stale after restore), all re-OCR tags are removed, and an audit note records what was
  restored, which backup was used, and which tags were removed.

## License

MIT — see [`LICENSE`](LICENSE).

