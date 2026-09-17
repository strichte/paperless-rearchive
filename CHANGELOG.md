# Changelog

All notable changes to paperless-rearchive are documented here. Format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the
project versions with SemVer (pre-1.0: features bump the minor, fixes the
patch).

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
