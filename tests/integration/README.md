# Manual / integration tooling for paperless-rearchive.
#
# These scripts talk to the *live* paperless-ngx / Postgres / Chandra stack and are
# not part of the pytest suite (which lives one directory up). They are the harness
# used during development and for the runbook in doc/PLANNING.md.
#
# | Script | What it does |
# | --- | --- |
# | `check_archive_provenance.sh <rel_path>...` (or `--all [root] [--limit N]`) | Report which engine (Chandra vs Tesseract) produced an archive's text layer, from the PDF `Creator` metadata. Exit 1 if any archive was not written by Chandra or could not be verified - the P1 acceptance check (`doc/OCR_STRATEGY.md`). |
# | `check_download.py <doc_id>` | Verify `download_original()` returns the immutable original, not the archive (`?original=true`). |
# | `check_tags.py` | Probe `/api/tags/` filter semantics (documents which query params work). |
# | `diag_original.py <doc_id>` | Dump what the API serves for a document (original vs archive, text layer, mime). |
# | `e2e_one.py <doc_id> [content]` | Run the pipeline for one document, including writes and tag swap. |
# | `size_compare.py <doc_id>` | OCR a document's original and compare the archive size/quality against the current one. |
# | `reset_baseline.sh <doc> <rel> <archive>` | Restore a test document to a clean pre-run baseline (archive file + DB checksum + content marker + trigger tag). |
# | `run_poller_e2e.sh [extra env...]` | Run one real poll cycle (`REARCHIVE_RUN_ONCE=true`) as the deployed container would. |
# | `run_content_e2e.sh` | End-to-end test of `re-ocr-content`: content updated, archive byte-identical. |
# | `verify_state.sh [doc_id]` | Dump the document's DB row, tags, archive dir, and checksums for manual inspection. |
#
# Example (host, using the published API port):
#
#     export PAPERLESS_API_TOKEN=$(grep '^PAPERLESS_API_TOKEN=' ../paperless-lxc/.env.paperless-gpt | cut -d= -f2-)
#     PAPERLESS_BASE_URL=http://localhost:8001 python3 tests/integration/check_download.py 123