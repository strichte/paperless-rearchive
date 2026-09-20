# Changelog

All notable changes to paperless-rearchive are documented here. Format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the
project versions with SemVer (pre-1.0: features bump the minor, fixes the
patch).

## Unreleased

### Added
- Born-digital provenance gate (pdf-inspector, per page): a PDF original is
  classified as `text_based`, `scanned`, or `mixed` before any OCR run. A
  born-digital document is left completely untouched (content and archive)
  and tagged `re-ocr-preserved`; a mixed document is OCR'd only on the pages
  that need it.
- `re-ocr-force` modifier tag (configurable via `REARCHIVE_FORCE_TAG`):
  placed next to a trigger, it bypasses the provenance gate and forces OCR of
  every page for that document only.
- New settings: `REARCHIVE_PDF_PROVENANCE`, `REARCHIVE_SKIP_BORN_DIGITAL`,
  `REARCHIVE_PRESERVED_TAG`, `REARCHIVE_OCR_MIXED_MODE`,
  `REARCHIVE_PROVENANCE_MAX_PAGES`, `REARCHIVE_FORCE_TAG`.
- Explicit `skip` value for `REARCHIVE_OCR_MODE` (ocrmypdf `--skip-text`).
- `inspect_pdf.py --decide` reports the sidecar's gate decision.
- Model-name preflight (`ocr/model_check.py`): `PAPERLESS_CHANDRA_MODEL_NAME`
  is checked once per process against the server's `GET /v1/models` list, so a
  typo fails the document before any page (and any retry) is attempted.

### Fixed
- `re-ocr-all` could mangle born-digital PDFs: the default `redo` mode
  stripped their native text layer and replaced it with Chandra output.
- `_finish()` logged `extra_tags` but never added them to the document.
- A wrong `PAPERLESS_CHANDRA_MODEL_NAME` no longer pays the upstream retry
  ladder per page before failing: the failure names the requested model and
  lists the models the server actually serves. The poller treats it as a
  deployment-wide misconfiguration: the cycle is aborted (no per-document
  failure is recorded and no document is escalated) and the next cycle waits
  the full poll interval. The upstream `Error during VLLM generation: ...`
  print is also re-logged with the model name when the server exposes no
  usable `/v1/models` list.

### Changed
- OCR strategy is now two layers: provenance (which pages need OCR) and mode
  (how existing text on those pages is treated). `auto` no longer silently
  upgrades to `redo`; mixed documents use `--skip-text`.
- Backup location hardwired to `/archive-backups` (`REARCHIVE_BACKUP_DIRECTORY`
  removed); archive mount default is now `/archive` (paperless-ngx naming).
  Control both via bind mounts.

## 0.1.0 — 2026-09-17

First public release. Tag-driven re-OCR sidecar for paperless-ngx: re-OCR
existing documents with Chandra (LLM vision OCR) and regenerate their
archive PDF/A exactly like paperless-chandra ingest does.

### Added
- `re-ocr-content` trigger: replaces the document `content` field with
  Chandra markdown (per-page fast path, page-level error tracking via the
  `re-ocr-page-errors` tag).
- `re-ocr-all` trigger: additionally regenerates the archive PDF/A with the
  same ocrmypdf pipeline paperless ingest uses (Chandra plugin as the OCR
  engine), atomically replacing the file and updating
  `documents_document.archive_checksum` in the database.
- Served-model provenance baked into the archive: `Creator` metadata carries
  ocrmypdf/library versions plus `[model: <PAPERLESS_CHANDRA_MODEL_NAME>]`.
- Run provenance in paperless: `OCR engine` / `OCR date` / `OCR pages` /
  `OCR archive ratio` custom fields and an append-only audit note per run.
- Adaptive polling: ~10 s cycles while a backlog drains, exponential backoff
  on no-progress cycles, idle `REARCHIVE_POLL_INTERVAL` when quiet; SIGHUP
  forces an immediate cycle.
- Failure escalation: 3 consecutive failed attempts per document escalate to
  `<trigger>-failure` with an audit note of the last error.
- Mandatory `REARCHIVE_BACKUP_DIRECTORY` outside the archive tree (startup
  validation + runtime backstop), atomic archive replacement with checksum
  verification and drift adoption.
- OCR semantics mirroring paperless ingest: `redo`/`auto`/`force`/`off`
  modes, clean/deskew/rotate handling incl. the ocrmypdf `redo` constraints,
  `REARCHIVE_OCR_USER_ARGS` escape hatch.
- Docker Compose deployment (source build or Gitea container registry), CI
  and release automation via Gitea Actions.
- Secret handling: plain env, compose `env_file`, or the paperless-ngx
  `_FILE` convention (API token, Chandra key, DB credentials).

### Requirements
- paperless-ngx under docker compose, running on PostgreSQL (`re-ocr-all`
  needs direct DB access; content-only does not).
- A Chandra inference server with an OpenAI-compatible API.
- Write access to the archive directory; a backup directory outside the
  archive tree.

### Known limitations
- Non-PDF originals are always content-only (`REARCHIVE_ARCHIVE_FOR_IMAGES`
  is reserved).
- SQLite paperless instances cannot use `re-ocr-all` (PostgreSQL-only DB
  access).
- paperless-chandra installs from git `master` unless `CHANDRA_REF` is
  pinned at image build time.
