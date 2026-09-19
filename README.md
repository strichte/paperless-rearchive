# paperless-rearchive

A tag-driven sidecar for [paperless-ngx](https://docs.paperless-ngx.com) that **re-OCRs documents
already in your library** with [Chandra](https://github.com/datalab-to/chandra), an LLM vision
OCR model — and optionally regenerates the *archive* (searchable PDF/A) the same way
paperless-ngx does at ingest. Something the paperless-ngx API deliberately does not let you do.

- Tag a document `re-ocr-content` → its `content` field is replaced with fresh OCR markdown.
- Tag it `re-ocr-all` → content **plus** the archive file is regenerated.
- The sidecar polls paperless for these tags and processes tagged documents in the background.

> Design notes and the engineering log live in [`doc/PLANNING.md`](doc/PLANNING.md).

## Quickstart

Minimal service for the paperless-ngx compose wizards. Only required settings — everything else
runs on defaults (see [Configuration reference](#configuration-reference)).

```yaml
services:
  paperless-rearchive:
    build:
      context: ./paperless-rearchive
      dockerfile: docker/Dockerfile
    image: paperless-rearchive:v0.1.0
    user: "1000:1000"   # UID:GID that owns the archive files (same as paperless USERMAP_UID/GID)
    environment:
      PAPERLESS_API_TOKEN: "<paperless-api-token>"          # Admin -> Documents -> Tokens
      PAPERLESS_CHANDRA_SERVER_URL: "http://chandra-server:8000"  # see Chandra server below
      PAPERLESS_DBPASS: "<postgres-password>"               # re-ocr-all only; omit for content-only
    volumes:
      - /data/paperless/media/documents/archive:/archive         # read-write
      - /data/paperless/archive-backups:/archive-backups         # required, outside the archive tree
```

Steps:

- Clone the repo next to your compose file: `git clone https://github.com/<you>/paperless-rearchive.git`
- Adjust the three placeholders above (token, server URL, DB password) and the two host paths.
- `docker compose build paperless-rearchive && docker compose up -d paperless-rearchive`
- Tag one disposable test document `re-ocr-content`, wait a cycle, check for
  `re-ocr-content-success`. For an immediate cycle: `docker kill -s HUP paperless-rearchive`.
- Start real runs with `REARCHIVE_DRY_RUN: "true"` first for a no-write rehearsal.

## Chandra server setup

All inference happens on a self-hosted OpenAI-compatible server. The sidecar itself is CPU-only.
Any server exposing `/v1/chat/completions` works; [vLLM](https://github.com/vllm-project/vllm) is
the reference. Same server you (would) use for
[paperless-chandra](https://github.com/flobernd/paperless-chandra) ingest works as-is.

Minimal server next to paperless (from
[paperless-chandra's example](https://github.com/flobernd/paperless-chandra/blob/master/examples/docker-compose.vllm.yml)):

```yaml
services:
  chandra-server:
    image: vllm/vllm-openai:v0.17.0
    command:
      - --model=datalab-to/chandra-ocr-2
      - --served-model-name=chandra
      - --api-key=${CHANDRA_API_KEY:?missing CHANDRA_API_KEY in .env}
      - --dtype=bfloat16
      - --max-model-len=18000
      - --max-num-seqs=16
      - --max-num-batched-tokens=2048
      - --gpu-memory-utilization=0.85
      - --enable-prefix-caching
      - --no-enforce-eager
      - --mm-processor-kwargs={"min_pixels":3136,"max_pixels":6291456}
    expose: ["8000"]
    ipc: host
    volumes: [hf_cache:/root/.cache/huggingface]   # ~10 GB model weights, persist between restarts
    deploy:
      resources:
        reservations:
          devices: [{driver: nvidia, capabilities: [gpu], count: 1}]

volumes:
  hf_cache:
```

Notes:

- GPU: ~24 GB class (L4, RTX 4090) for full-precision bf16 at 1–2 pages/sec. Smaller GPUs via
  quantized GGUF take tens of seconds per page; CPU-only takes minutes per page.
- Point the sidecar at it: `PAPERLESS_CHANDRA_SERVER_URL: "http://chandra-server:8000"` (same
  compose) or `http://ai:8110/v1` (remote GPU box).
- `PAPERLESS_CHANDRA_MODEL_NAME` must equal `--served-model-name` (`chandra` above).
- `PAPERLESS_CHANDRA_API_KEY` must equal `CHANDRA_API_KEY` — omit both if the server needs no auth.
- Check it: `curl http://chandra-server:8000/v1/models` should list your model.
- Commercial self-hosting needs a [license](https://datalab.to/pricing) (weights are modified
  OpenRAIL-M; the plugin code is MIT).

## Is this tool for you?

Re-OCR rewrites `content` and *replaces archive files*. Check every item:

- **Docker compose paperless.** The sidecar is a compose service next to `paperless` + `postgres`.
- **Write access to the archive dir.** Mount `media/documents/archive/` at `/archive`
  read-write; run as the UID:GID that owns the files. Originals are never touched (API
  download only).
- **API token.** Paperless *Admin → Documents → Tokens*.
- **PostgreSQL** (`re-ocr-all` only). SQLite/MariaDB: `re-ocr-content` works, `re-ocr-all`
  cannot. Reuse paperless's `PAPERLESS_DB*` credentials. Content-only runs never open a DB
  connection.
- **Chandra server** (above). Same server as paperless-chandra ingest.
- **Backup mount.** Every replaced archive is copied to `/archive-backups` first. Mount it
  outside the archive tree (separate bind mount, may be another disk) or paperless flags the
  backups as orphaned files.
- **No undo button.** Changes are recorded (audit note, provenance fields, `.bak` copies) — still,
  start with `REARCHIVE_DRY_RUN=true` and one test doc.

Only need Chandra for **new** documents? Run
[paperless-chandra](https://github.com/flobernd/paperless-chandra) directly — this project is for
what's already in your library.

## Trigger tags

| Tag | Effect |
| --- | --- |
| `re-ocr-content` | Replace `content` with fresh OCR markdown. No archive, no DB. |
| `re-ocr-all` | As above, **plus** regenerate the archive (same ocrmypdf pipeline as ingest) and update `archive_checksum` in the DB. |
| `re-ocr-force` | Modifier, not a trigger: add next to one of the above to force OCR of every page. Never auto-created. |

- Trigger tags are auto-created. No manual setup — tag a doc and wait (or `docker kill -s HUP
  paperless-rearchive`).
- After processing the trigger is swapped for `<trigger>-success` or `<trigger>-failure`.
- Extra tags: `re-ocr-preserved` (born-digital, left untouched), `re-ocr-detection-unknown`
  (could not classify, processed anyway), `re-ocr-page-errors` (content mode, some pages failed).
- Transient errors (server down, network hiccup) keep the trigger tag — documents self-heal next
  cycle. After **3 consecutive failures** a doc is escalated to `<trigger>-failure` with an audit
  note.

## Safety

- **Originals immutable.** Downloaded via `?original=true` into a scratch dir, used as OCR source.
- **Born-digital PDFs preserved.** Classified page by page (pdf-inspector) before any OCR run;
  all-native docs are left completely untouched (`-success` + `re-ocr-preserved` + note).
- **Atomic archive replace.** Temp file → `os.replace`, checksum verified before (never races a
  concurrent write), checksum updated after. Non-PDF originals and docs without an archive fall
  through to content-only.
- **Backups unconditional.** Every replacement copies the old archive to `/archive-backups`
  (layout mirrored). Startup creates the dir, verifies writability, and refuses to run when the
  mounts overlap.
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

## Full setup (docker compose)

Same compose file as paperless-ngx (shared network, Postgres, archive dir). Adjust `paperless` /
`postgres` / server URL to your setup.

### 1. Get the code next to your compose file

```bash
cd /opt/paperless            # wherever your paperless-ngx docker-compose.yml lives
git clone https://github.com/<you>/paperless-rearchive.git
```

### 2. Add the service to `docker-compose.yml`

Abbreviated but complete: postgres, valkey/redis, tika, gotenberg, paperless (with Chandra plugin
for ingest) plus `paperless-rearchive`. Host paths `/data/paperless/...`, UID/GID `1000` are
placeholders. Chandra server runs elsewhere (`http://ai:8110/v1` here) — or add
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
      PAPERLESS_CHANDRA_SERVER_URL: http://ai:8110/v1
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
      PAPERLESS_CHANDRA_SERVER_URL: "http://ai:8110/v1"
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


### 3. Build and start

```bash
docker compose build paperless-rearchive
docker compose up -d paperless-rearchive
docker compose logs -f paperless-rearchive   # watch the first cycles
```

### 4. First test

1. Set `REARCHIVE_DRY_RUN: "true"` first if you want a no-write rehearsal.
2. Tag one **disposable test document** `re-ocr-all` in the paperless UI.
3. Wait for a poll cycle (or `docker kill -s HUP paperless-rearchive`).
4. Check the outcome: the document should carry `re-ocr-all-success`, its `content` field fresh
   markdown, and an audit note; the archive file's `Creator` metadata should read
   `OCRmyPDF … / Chandra …`. The old archive is kept as `.bak-<timestamp>` in
   `/archive-backups`.

## Secrets

Three credentials: paperless API token, Chandra API key, DB password (optionally DB user).
Pick one way per credential — or mix:

### Way 1: plain values

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

### Way 2: `_FILE` variables

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

### Way 3: compose `secrets:` (paperless-ngx convention)

- Same `_FILE` variables, files mounted at `/run/secrets/<name>`. Keeps values out of
  `docker inspect`. This is what [Full setup](#full-setup-docker-compose) uses — see
  [`doc/deploy/compose-snippet.yml`](doc/deploy/compose-snippet.yml).

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
| `PAPERLESS_CHANDRA_MODEL_NAME` | `chandra` | The model name the server advertises (`/v1/models`). |
| `PAPERLESS_CHANDRA_API_KEY` / `…_FILE` | *(empty)* | Bearer token for the server; leave unset if the server needs no auth. |
| `PAPERLESS_CHANDRA_CONTENT_FORMAT` | `markdown` | Format of the OCR text stored in paperless: `markdown` (recommended) or `text`. |
| `PAPERLESS_CHANDRA_MAX_OUTPUT_TOKENS` | `12384` | Per-page output token budget for the model. |

### OCR behaviour (mirrors paperless's `PAPERLESS_OCR_*`)

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

## Status

- ✅ Both trigger paths verified live (content PATCH, archive replace + checksum UPDATE).
- ✅ Born-digital gate, per-page content path (`re-ocr-page-errors`), adaptive polling (~10 s
  drain, backoff), 3-strikes escalation, `.bak` backups + `restore_backup`, provenance fields +
  audit notes, `Creator [model: …]` stamp.
- ⬜ Open (see PLANNING Risks): repeat-retry visibility + raster parity, mixed-archive divergence,
  `ARCHIVE_FOR_IMAGES` reserved, bulk-run rehearsal.

## Development & releases

- `main` is dev; releases are source tags built with compose — see
  [`doc/RELEASING.md`](doc/RELEASING.md) (versioning, `scripts/release-check.sh`, rollback).
- [`doc/PLANNING.md`](doc/PLANNING.md) — plan, research, config, risks.
- `tests/` — unit tests (`pytest`); `tests/integration/` — live helpers
  (`check_archive_provenance.sh`: exit 0 = all archives Chandra).
- OCR engine is pluggable (`OcrProviderPlugin`); `ChandraProvider` wraps paperless-chandra today.

## License

MIT — see [`LICENSE`](LICENSE).

