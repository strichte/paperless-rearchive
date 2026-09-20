# paperless-rearchive <!-- omit from toc -->
- [1. Chandra server setup](#1-chandra-server-setup)
- [2. Is this tool for you?](#2-is-this-tool-for-you)
- [3. Trigger tags](#3-trigger-tags)
- [4. Safety](#4-safety)
- [5. OCR strategy](#5-ocr-strategy)
- [6. Full setup (docker compose)](#6-full-setup-docker-compose)
  - [6.1. Get the code next to your compose file](#61-get-the-code-next-to-your-compose-file)
  - [6.2. Add the service to `docker-compose.yml`](#62-add-the-service-to-docker-composeyml)
  - [6.3. Build and start](#63-build-and-start)
  - [6.4. First test](#64-first-test)
- [7. Secrets](#7-secrets)
  - [7.1. Way 1: plain values](#71-way-1-plain-values)
  - [7.2. Way 2: `_FILE` variables](#72-way-2-_file-variables)
  - [7.3. Way 3: compose `secrets:` (paperless-ngx convention)](#73-way-3-compose-secrets-paperless-ngx-convention)
- [8. Configuration reference](#8-configuration-reference)
  - [8.1. Paperless connection](#81-paperless-connection)
  - [8.2. Chandra inference server](#82-chandra-inference-server)
  - [8.3. OCR behaviour (mirrors paperless's `PAPERLESS_OCR_*`)](#83-ocr-behaviour-mirrors-paperlesss-paperless_ocr_)
  - [8.4. Tags, loop and safety](#84-tags-loop-and-safety)
  - [8.5. Database (re-ocr-all only)](#85-database-re-ocr-all-only)
- [9. OCR provenance](#9-ocr-provenance)
- [10. Manual trigger](#10-manual-trigger)
- [11. Backups](#11-backups)
- [12. Restoring from backups (`restore_backup`)](#12-restoring-from-backups-restore_backup)
- [13. Status](#13-status)
- [14. Development \& releases](#14-development--releases)
- [15. License](#15-license)

A tag-driven sidecar for [paperless-ngx](https://docs.paperless-ngx.com) that **re-OCRs documents
already in your library** with [Chandra](https://github.com/datalab-to/chandra), an LLM vision
OCR model — and optionally regenerates the *archive* (searchable PDF/A) the same way
paperless-ngx does at ingest. Something the paperless-ngx API deliberately does not let you do.

- Tag a document `re-ocr-content` → its `content` field is replaced with fresh OCR markdown.
- Tag it `re-ocr-all` → content **plus** the archive file is regenerated.
- The sidecar polls paperless for these tags and processes tagged documents in the background.

> Design notes and the engineering log live in [`doc/PLANNING.md`](doc/PLANNING.md).

## Quickstart <!-- omit from toc -->

Minimal service for the paperless-ngx compose wizards. Only required settings — everything else
runs on defaults (see [Configuration reference](#configuration-reference)).

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
`--served-model-name=chandra` — but the value **must** match the server's served name or every
request fails with `model not found`, so listing it explicitly is safer.

Steps:

- Clone the repo next to your compose file: `git clone https://github.com/<you>/paperless-rearchive.git`
- Adjust the placeholders above (token, server URL, model name, DB password) and the two host paths.
- `docker compose build paperless-rearchive && docker compose up -d paperless-rearchive`
- Tag one disposable test document `re-ocr-content`, wait a cycle, check for
  `re-ocr-content-success`. For an immediate cycle: `docker kill -s HUP paperless-rearchive`.
- Start real runs with `REARCHIVE_DRY_RUN: "true"` first for a no-write rehearsal.

## 1. Chandra server setup

All inference happens on a self-hosted server with an OpenAI-compatible endpoint. The sidecar itself is CPU-only.
Any server exposing `/v1/chat/completions` works; [vLLM](https://github.com/vllm-project/vllm) is
the reference. Same server you would use for
[paperless-chandra](https://github.com/flobernd/paperless-chandra) ingest works as-is. If you want to host with vLLM please follow [paperless-chandra'  recommended setup](https://github.com/flobernd/paperless-chandra/tree/master#docker-compose-example).

Alternatively, you can host it with `llama-swap`:

```yaml
services:
    image: ghcr.io/mostlygeek/llama-swap:v255-cuda13-b10902
    labels:
    container_name: llama-swap
    ports:
      - "8080:8080"
    volumes:
      - ~/docker/llama.cpp/models:/models
      - ~/docker/llama.cpp/cache:/root/.cache/huggingface
      - ~/docker/llama-swap/config.yaml:/app/config.yaml
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
      - CHANDRA_API_KEY=${CHANDRA_API_KEY:?missing CHANDRA_API_KEY in .env}
    restart: unless-stopped
    command: --config /app/config.yaml --listen 0.0.0.0:8080
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```
with `config.yaml`:
```yaml
models:
  "chandra-ocr-2-q8":
    description: "Datalab Chandra OCR 2 (5B vision model) - document/image OCR to markdown"
    ttl: 600
    cmd: |
      llama-server
      -m /models/chandra-ocr-2/chandra-ocr-2.Q8_0.gguf
      --mmproj /models/chandra-ocr-2/chandra-ocr-2.mmproj-f16.gguf
      --port ${PORT}
      --api-key ${env.CHANDRA_API_KEY}
      -ngl 999
      --parallel 1
      --flash-attn on
      --ctx-size 18000
      --temp 0.0
      --jinja
      --chat-template-kwargs '{"enable_thinking":false}'
  "chandra-ocr-2-bf16":
    description: "Datalab Chandra OCR 2 (5B vision model) - unquantized BF16, max fidelity"
    ttl: 600
    cmd: |
      llama-server
      -m /models/chandra-ocr-2/chandra-ocr-2.BF16.gguf
      --mmproj /models/chandra-ocr-2/chandra-ocr-2.mmproj-bf16.gguf
      --port ${PORT}
      --api-key ${env.CHANDRA_API_KEY}
      -ngl 999
      --parallel 1
      --flash-attn on
      --ctx-size 18000
      --temp 0.0
      --jinja
      --chat-template-kwargs '{"enable_thinking":false}'
```

The model name (`chandra-ocr-2-q8` and `chandra-ocr-2-bf16` in the example above) is what you specify in your `paperless-rearchive` docker compose file with the `PAPERLESS_CHANDRA_MODEL_NAME` environment variable.

I've downloaded two different quants of the Chandra model to `~/docker/llama.cpp/models` and added the directory with bind mount `~/docker/llama.cpp/models:/models` to `llama-swap`. Pick the one that works for you or try both and compare the results. I found Q8 to give very good results already.

```bash
mkdir -p ~/docker/llama.cpp/models/chandra-ocr-2
cd ~/docker/llama.cpp/models/chandra-ocr-2

# Q8_0 quantized (~5.16 GB)
curl -L -C - -o chandra-ocr-2.Q8_0.gguf \
  https://huggingface.co/prithivMLmods/chandra-ocr-2-GGUF/resolve/main/chandra-ocr-2.Q8_0.gguf

# Unquantized BF16 (~9.7 GB) — lossless repackaging of the bf16 checkpoint weights
curl -L -C - -o chandra-ocr-2.BF16.gguf \
  https://huggingface.co/prithivMLmods/chandra-ocr-2-GGUF/resolve/main/chandra-ocr-2.BF16.gguf

# Vision projector (shared by both variants — see note below)
curl -L -C - -o chandra-ocr-2.mmproj-f16.gguf \
  https://huggingface.co/prithivMLmods/chandra-ocr-2-GGUF/resolve/main/chandra-ocr-2.mmproj-f16.gguf
```

Files:
- `chandra-ocr-2.Q8_0.gguf`         5,157,833,312 bytes (~5.16 GB)
- `chandra-ocr-2.BF16.gguf`        9,695,791,712 bytes (~9.70 GB)  ← unquantized
- `chandra-ocr-2.mmproj-f16.gguf`     675,568,928 bytes (~676 MB)

**mmproj note:** the repo's `mmproj-bf16.gguf` is byte-identical to
`mmproj-f16.gguf` (same sha256 `a270372d…` — one projector ships with every
quant). You can keep a single physical copy and make `chandra-ocr-2.mmproj-bf16.gguf`
a **hardlink** to `chandra-ocr-2.mmproj-f16.gguf`. Re-create it after any
re-download with:
`ln chandra-ocr-2.mmproj-f16.gguf chandra-ocr-2.mmproj-bf16.gguf`

Served via llama-swap, entries in `~/docker/llama-swap/config.yaml`:
- `chandra-ocr-2-q8`  → Q8_0
- `chandra-ocr-2-bf16` → BF16 (unquantized)

Both entries use `--temp 0.0` (Chandra expects greedy decoding; llama-server
defaults to temp 0.8 which makes output nondeterministic) and
`--chat-template-kwargs '{"enable_thinking":false}'` is required — see
https://github.com/flobernd/paperless-chandra (GGUF builds re-enable thinking
otherwise, breaking the output).

**Note:** Datalab's [Chandra OCR 2 model](https://github.com/datalab-to/chandra) uses a dual licensing structure: the source code is licensed under Apache-2.0, while the model weights are governed by a modified OpenRAIL-M license.

* **License Breakdown:** 
  * Code License: Apache-2.0 for the repository's codebase.
  * Model Weights License: Modified OpenRAIL-M.
* **Usage Terms & Free Tier:**
  * Free Use: Free for research, personal use, and startups with under $2 million in funding or revenue.
  * Restrictions: Cannot be used to compete directly with Datalab's API services.
  * Commercial License: Required for larger organizations, companies with over $2M in revenue/funding, or high-volume/on-prem enterprise needs. You can obtain a commercial agreement through the Datalab Pricing page.



## 2. Is this tool for you?

Re-OCR rewrites `content` and *replaces archive files*. Check every item:

- **Docker compose paperless.** The sidecar is a compose service next to `paperless` + `postgres`.
- **Write access to the archive dir.** Mount `media/documents/archive/` at `/archive`
  read-write; run as the UID:GID that owns the files. Originals are never touched (API
  download only).
- **API token.** Paperless *Admin → Documents → Tokens*.
- **PostgreSQL** (`re-ocr-all` only). SQLite/MariaDB: `re-ocr-content` works, `re-ocr-all`
  cannot. Reuse paperless's `PAPERLESS_DB*` credentials. Content-only runs never open a DB
  connection.
- **Chandra server** (above). Same server as [paperless-chandra](https://github.com/flobernd/paperless-chandra) ingest.
- **Backup mount.** Every replaced archive is copied to `/archive-backups` first. Mount it
  outside the archive tree (separate bind mount, may be another disk).
- **No undo button.** Changes are recorded (audit note, provenance fields, `.bak` copies) — still,
  start with `REARCHIVE_DRY_RUN=true` and one test doc. Mistakes are reversible (with caveats) via
  [`restore_backup`](#restoring-from-backups-restore_backup).

Only need Chandra for **new** documents? Run
[paperless-chandra](https://github.com/flobernd/paperless-chandra) directly — this project is for
what's already in your library.

## 3. Trigger tags

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
  note.

## 4. Safety

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

## 5. OCR strategy

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

## 6. Full setup (docker compose)

Same compose file as paperless-ngx (shared network, Postgres, archive dir). Adjust `paperless` /
`postgres` / server URL to your setup.

### 6.1. Get the code next to your compose file

```bash
cd /opt/paperless            # wherever your paperless-ngx docker-compose.yml lives
git clone https://github.com/<you>/paperless-rearchive.git
```

### 6.2. Add the service to `docker-compose.yml`

Abbreviated but complete: postgres, valkey/redis, tika, gotenberg, paperless (with Chandra plugin
for ingest) plus `paperless-rearchive`. Host paths `/data/paperless/...`, UID/GID `1000` are
placeholders. Chandra server runs elsewhere (`http://my-ai.local:8000/v1` here) — or add
[chandra-server](#chandra-server-setup) to the same file.

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
      REARCHIVE_PDF_PROVENANCE: "auto"     # born-digital gate: auto | off
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


### 6.3. Build and start

```bash
docker compose build paperless-rearchive
docker compose up -d paperless-rearchive
docker compose logs -f paperless-rearchive   # watch the first cycles
```

### 6.4. First test

1. Set `REARCHIVE_DRY_RUN: "true"` first if you want a no-write rehearsal.
2. Tag one **disposable test document** `re-ocr-all` in the paperless UI.
3. Wait for a poll cycle (or `docker kill -s HUP paperless-rearchive`).
4. Check the outcome: the document should carry `re-ocr-all-success`, its `content` field fresh
   markdown, and an audit note; the archive file's `Creator` metadata should read
   `OCRmyPDF … / Chandra …`. The old archive is kept as `.bak-<timestamp>` in
   `/archive-backups`.

## 7. Secrets

Three credentials: paperless API token, Chandra API key, DB password (optionally DB user).
Pick one way per credential — or mix:

### 7.1. Way 1: plain values

```yaml
services:
  paperless-rearchive:
    environment:
      PAPERLESS_API_TOKEN: "paste-token-here"
      PAPERLESS_CHANDRA_API_KEY: "paste-key-here"   # omit if no auth needed
      PAPERLESS_DBPASS: "postgres-password"
```

- Simplest for single-operator instances. Also works via `env_file:` — see
  [`doc/deploy/env.example`](doc/deploy/env.example).

### 7.2. Way 2: `_FILE` variables

- For every credential `NAME`, `NAME_FILE` points at a file whose content is the secret.
  `_FILE` wins when both are set. Whitespace is stripped.
- Works for all four: `PAPERLESS_API_TOKEN`, `PAPERLESS_CHANDRA_API_KEY`, `PAPERLESS_DBPASS`,
  `PAPERLESS_DBUSER`.

```yaml
services:
  paperless-rearchive:
    volumes: [/data/paperless/secrets:/run/secrets:ro]
    environment:
      PAPERLESS_API_TOKEN_FILE: /run/secrets/paperless_api_token
      PAPERLESS_CHANDRA_API_KEY_FILE: /run/secrets/chandra_api_key
      PAPERLESS_DBPASS_FILE: /run/secrets/paperless_db_paperless_passwd
```

### 7.3. Way 3: compose `secrets:` (paperless-ngx convention)

- Same `_FILE` variables, files mounted at `/run/secrets/<name>`. Keeps values out of
  `docker inspect`. This is what [Full setup](#full-setup-docker-compose) uses — see
  [`doc/deploy/compose-snippet.yml`](doc/deploy/compose-snippet.yml).

## 8. Configuration reference

Booleans accept `true/false/yes/on/1`. Mounts are fixed: `/archive` (paperless
`media/documents/archive`, read-write) and `/archive-backups` (outside the archive tree).
`REARCHIVE_ARCHIVE_DIR` only exists to override `/archive` if the mount differs.

### 8.1. Paperless connection

| Variable | Default | Purpose |
| --- | --- | --- |
| `PAPERLESS_BASE_URL` | `http://paperless:8000` | Base URL of the paperless web server (REST API is `<url>/api/`). Use the *service name* from your compose file, not `localhost`. |
| `PAPERLESS_API_TOKEN` / `PAPERLESS_API_TOKEN_FILE` | *(required)* | API token created in paperless (Admin → Documents → Tokens). Needs read access to documents, write access to content/tags/notes/custom fields. |

### 8.2. Chandra inference server

| Variable | Default | Purpose |
| --- | --- | --- |
| `PAPERLESS_CHANDRA_SERVER_URL` | *(required)* | OpenAI-compatible server hosting Chandra, e.g. `http://chandra-server:8000` or `http://ai:8110/v1`. |
| `PAPERLESS_CHANDRA_MODEL_NAME` | `chandra` | The model name the server advertises (`/v1/models`). |
| `PAPERLESS_CHANDRA_API_KEY` / `…_FILE` | *(empty)* | Bearer token for the server; leave unset if the server needs no auth. |
| `PAPERLESS_CHANDRA_CONTENT_FORMAT` | `markdown` | Format of the OCR text stored in paperless: `markdown` (recommended) or `text`. |
| `PAPERLESS_CHANDRA_MAX_OUTPUT_TOKENS` | `12384` | Per-page output token budget for the model. |

### 8.3. OCR behaviour (mirrors paperless's `PAPERLESS_OCR_*`)

| Variable | Default | Purpose |
| --- | --- | --- |
| `REARCHIVE_PROVIDER` | `chandra` | OCR provider plugin. Only `chandra` exists today. |
| `REARCHIVE_OCR_MODE` | `redo` | How existing text on OCR-routed pages is treated — see [OCR strategy](#ocr-strategy). `redo`/`auto`/`force`/`off`/`skip` (legacy `skip`/`skip_noarchive` = `auto`). |
| `REARCHIVE_PDF_PROVENANCE` | `auto` | Born-digital detection (pdf-inspector): `auto` classifies each original; `off` keeps mode-driven behaviour. |
| `REARCHIVE_SKIP_BORN_DIGITAL` | `true` | `true`: born-digital docs untouched (no PATCH, no ocrmypdf, no DB); tagged `re-ocr-preserved`. |
| `REARCHIVE_PRESERVED_TAG` | `re-ocr-preserved` | Tag added alongside `-success` when native text was preserved. Empty disables. |
| `REARCHIVE_OCR_MIXED_MODE` | `skip` | ocrmypdf mode for mixed docs (`skip`/`redo`/`force`). `skip` = `--skip-text`, keeps native pages. |
| `REARCHIVE_PROVENANCE_MAX_PAGES` | `0` | Pages inspected by the provenance classifier; `0` = all. |
| `REARCHIVE_FORCE_TAG` | `re-ocr-force` | Modifier tag (never auto-created) to force OCR of every page. Empty disables. |
| `REARCHIVE_OCR_CLEAN` | `clean` | Image cleaning: `clean` (pre-OCR), `final` (becomes pre-OCR under `redo`), `none`. |
| `REARCHIVE_OCR_DESKEW` | `true` | Fix small skew. Silently skipped under `redo` (ocrmypdf constraint, same as paperless). |
| `REARCHIVE_OCR_ROTATE_PAGES` | `true` | Fix page orientation. Detection: local Tesseract OSD (no GPU). |
| `REARCHIVE_OCR_ROTATE_PAGES_THRESHOLD` | `12.0` | Rotation confidence threshold; lower = more aggressive. |
| `REARCHIVE_OCR_OUTPUT_TYPE` | `pdfa` | Archive flavour (`pdfa`, `pdfa-1/2/3`, `pdf`), like `PAPERLESS_OCR_OUTPUT_TYPE`. |
| `REARCHIVE_OCR_LANGUAGE` | `eng` | Label passed to ocrmypdf (e.g. `eng+deu`). Chandra is language-agnostic. |
| `REARCHIVE_OCR_USER_ARGS` | *(unset)* | JSON ocrmypdf kwargs, merged **last** (same escape hatch as `PAPERLESS_OCR_USER_ARGS`). |
| `REARCHIVE_OCR_DPI` | `300` | Render DPI for the content-only fast path. Archive runs rasterise inside ocrmypdf. |
| `REARCHIVE_MAX_PAGES` | `0` | `0` = all pages; `N` = first N pages only. |
| `REARCHIVE_OCR_CONCURRENCY` | `1` | Concurrent pages per doc. GPU-bound: raise gradually (2, 4), watch the GPU. |
| `REARCHIVE_ARCHIVE_FOR_IMAGES` | `false` | Reserved. Non-PDF originals are always handled content-only today. |

### 8.4. Tags, loop and safety

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

### 8.5. Database (re-ocr-all only)

- **PostgreSQL only.** SQLite: content-only works, `re-ocr-all` cannot. Copy from paperless's env.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PAPERLESS_DBHOST` | `postgres` | Postgres host (compose service name). |
| `PAPERLESS_DBPORT` | `5432` | Postgres port. |
| `PAPERLESS_DBNAME` | `paperless` | DB name. |
| `PAPERLESS_DBUSER` / `…_FILE` | `paperless` | DB user. |
| `PAPERLESS_DBPASS` / `…_FILE` | *(required for re-ocr-all)* | DB password. |

## 9. OCR provenance

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

## 10. Manual trigger

- Poll interval `REARCHIVE_POLL_INTERVAL` (default 300 s). Immediate cycle:
  `docker kill -s HUP paperless-rearchive`. In-flight runs are not interrupted; a signal during a
  cycle triggers the next one immediately after.

## 11. Backups

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

## 12. Restoring from backups (`restore_backup`)

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

## 13. Status

- ✅ Both trigger paths verified live (content PATCH, archive replace + checksum UPDATE).
- ✅ Born-digital gate, per-page content path (`re-ocr-page-errors`), adaptive polling (~10 s
  drain, backoff), 3-strikes escalation, `.bak` backups + `restore_backup`, provenance fields +
  audit notes, `Creator [model: …]` stamp.
- ⬜ Open (see PLANNING Risks): repeat-retry visibility + raster parity, mixed-archive divergence,
  `ARCHIVE_FOR_IMAGES` reserved, bulk-run rehearsal.

## 14. Development & releases

- `main` is dev; releases are source tags built with compose — see
  [`doc/RELEASING.md`](doc/RELEASING.md) (versioning, `scripts/release-check.sh`, rollback).
- [`doc/PLANNING.md`](doc/PLANNING.md) — plan, research, config, risks.
- `tests/` — unit tests (`pytest`); `tests/integration/` — live helpers
  (`check_archive_provenance.sh`: exit 0 = all archives Chandra).
- OCR engine is pluggable (`OcrProviderPlugin`); `ChandraProvider` wraps paperless-chandra today.

## 15. License

MIT — see [`LICENSE`](LICENSE).

