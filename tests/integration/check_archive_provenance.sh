#!/bin/bash
# Report which OCR engine produced the text layer of archive PDF(s).
#
# A re-OCR'd archive is only correct when its embedded text layer comes from
# Chandra. Before the P1 fix (doc/OCR_STRATEGY.md) the PDF/A assembly handed the
# pages to ocrmypdf's *default* engine, so the text layer was Tesseract's while
# the `content` field was Chandra's markdown. The engine is recorded in the
# PDF's Creator metadata:
#
#     OCRmyPDF 17.11.0 / OCRmyPDF fdp2 + Chandra 0.2.0       <- expected
#     OCRmyPDF 17.11.0 / OCRmyPDF fdp2 + Tesseract OCR 5.5.0 <- bug (P1)
#
# Usage:
#     tests/integration/check_archive_provenance.sh <rel_path_to_archive> [...]
#     tests/integration/check_archive_provenance.sh <doc_id> [...]          # by document ID
#     tests/integration/check_archive_provenance.sh --doc 4221 [--doc 4222 ...]
#     tests/integration/check_archive_provenance.sh --all [root] [--limit N]
#
# Bare numeric arguments are treated as document IDs; anything else is an
# archive path (absolute, or relative to the archive root).
#
# Document IDs are resolved to their on-disk archive path via the
# documents_document.archive_filename DB column (the same source the sidecar
# itself uses), queried with docker exec against the Postgres container
# (override with PAPERLESS_DB_CONTAINER / PAPERLESS_DB_USER / PAPERLESS_DB_NAME).
# Docs without an archive file are reported and counted as unverifiable.
#
# DB queries are performed by the *outer* script on the host where the docker
# CLI is available.  The docker CLI is intentionally not installed inside the
# paperless-rearchive container; only pdfinfo (poppler-utils) runs there.
#
# Paths are resolved inside the container against $ARCHIVE_ROOT_IN_CONTAINER
# (default /archive); absolute paths are used as-is.  --all scans the mount and
# may take minutes on a large library (one pdfinfo per file).
#
# Exit status: 0 = every archive showed Chandra, 1 = at least one Tesseract or
# unreadable file, or any --doc lacked an archive (verification failed), 2 =
# usage error.
#
# Related log evidence (the fallback engine logs its own warnings):
#     docker logs paperless-rearchive 2>&1 | grep -iE 'ocrmypdf\._exec\.tesseract'
set -euo pipefail

CONTAINER="${REARCHIVE_CONTAINER:-paperless-rearchive}"
ROOT="${ARCHIVE_ROOT_IN_CONTAINER:-/archive}"

if [ "$#" -eq 0 ]; then
  sed -n '2,27p' "$0"
  exit 2
fi

# ---------------------------------------------------------------------------
# Outer script: argument parsing + DB resolution.
# The docker CLI is available here (on the host) but NOT inside the
# paperless-rearchive container, so all docker/DB calls happen at this level.
# ---------------------------------------------------------------------------

mode=files
limit=""
scan_root=""
doc_ids=()
paths=()

# Parse every argument up-front so we can resolve --doc IDs before calling
# docker exec into the container.
while [ "$#" -gt 0 ]; do
  case "$1" in
    --all)
      mode=all
      shift
      # Optional scan root, consumed only if it is not the --limit flag.
      if [ "$#" -gt 0 ] && [ "$1" != "--limit" ]; then
        scan_root="$1"; shift
      fi
      if [ "${1:-}" = "--limit" ]; then
        limit="${2:-}"; shift 2
      fi
      ;;
    --doc)
      [ -n "${2:-}" ] || { echo "--doc requires an ID" >&2; exit 2; }
      doc_ids+=("$2"); shift 2
      ;;
    --doc=*)
      doc_ids+=("${1#*=}"); shift
      ;;
    *)
      # Bare numeric arguments are document IDs (see usage above); anything
      # else is a relative/absolute archive path.
      if [[ "$1" =~ ^[0-9]+$ ]]; then
        doc_ids+=("$1")
      else
        paths+=("$1")
      fi
      shift
      ;;
  esac
done

# Resolve document IDs to archive paths via the DB (same source as the sidecar).
missing=0
if [ "${#doc_ids[@]}" -gt 0 ]; then
  db_container="${PAPERLESS_DB_CONTAINER:-postgres}"
  db_user="${PAPERLESS_DB_USER:-paperless}"
  db_name="${PAPERLESS_DB_NAME:-paperless}"
  for id in "${doc_ids[@]}"; do
    case "$id" in
      ''|*[!0-9]*) echo "not a document ID: '$id'" >&2; exit 2 ;;
    esac
    row="$(docker exec "$db_container" psql -U "$db_user" -d "$db_name" -t -A \
      -c "select coalesce(archive_filename,'') from documents_document where id = $id")" \
      || { echo "DB query failed for document $id" >&2; exit 1; }
    if [ -z "$row" ]; then
      printf 'SKIPPED   document %s\n             (no archive file: content-only document, nothing to verify)\n' "$id"
      missing=$((missing + 1))
    else
      printf 'resolved  document %s -> %s\n' "$id" "$row"
      paths+=("$row")
    fi
  done
fi

# Build the argument vector for the inner script.
inner_args=()
if [ "$mode" = all ]; then
  inner_args+=(--all)
  [ -n "$scan_root" ] && inner_args+=("$scan_root")
  [ -n "$limit" ] && inner_args+=("--limit" "$limit")
else
  inner_args+=("${paths[@]}")
fi

# ---------------------------------------------------------------------------
# Inner script: runs inside the container.  Only needs pdfinfo.
# Receives either --all [scan_root] [--limit N], or a list of file paths.
# ---------------------------------------------------------------------------
set +e
docker exec -i "$CONTAINER" bash -s -- "$ROOT" "${inner_args[@]}" <<'INNER'
set -eu
root="$1"; shift

command -v pdfinfo >/dev/null || { echo "pdfinfo not found in container" >&2; exit 2; }

verdict() {
  file="$1"
  creator="$(pdfinfo "$file" 2>/dev/null | sed -n 's/^Creator:[[:space:]]*//p')" || creator=""
  if [ -z "$creator" ]; then
    printf 'ERROR     %s\n             (no Creator metadata or unreadable)\n' "$file"
    return 0
  fi
  case "$creator" in
    *Chandra*)   label=CHANDRA ;;
    *Tesseract*) label=TESSERACT ;;
    *)           label=OTHER ;;
  esac
  printf '%-9s %s\n             %s\n' "$label" "$file" "$creator"
}

if [ "${1:-}" = "--all" ]; then
  mode=all
  shift
  scan_root="$root"
  limit=""
  if [ "$#" -gt 0 ] && [ "$1" != "--limit" ]; then scan_root="$1"; shift; fi
  if [ "${1:-}" = "--limit" ]; then limit="${2:-}"; fi
  if [ -n "$limit" ]; then
    files="$(find "$scan_root" -type f -name '*.pdf' | head -n "$limit")"
  else
    files="$(find "$scan_root" -type f -name '*.pdf')"
  fi
  report="$(printf '%s\n' "$files" | while IFS= read -r f; do
    [ -n "$f" ] && verdict "$f"
  done)"
else
  report=""
  for path in "$@"; do
    case "$path" in
      /*) out="$(verdict "$path")" ;;
      *)  out="$(verdict "$root/$path")" ;;
    esac
    report="${report}${out}
"
  done
fi

printf '%s' "$report"
chandra=$(printf '%s' "$report" | grep -c '^CHANDRA' || true)
tess=$(printf '%s' "$report" | grep -c '^TESSERACT' || true)
other=$(printf '%s' "$report" | grep -c '^OTHER' || true)
err=$(printf '%s' "$report" | grep -c '^ERROR' || true)
printf '\nsummary: %s Chandra, %s Tesseract, %s other, %s unreadable\n' "$chandra" "$tess" "$other" "$err"
failed=0
if [ "$tess" -gt 0 ]; then
  printf '=> Tesseract text layer found: those archives were NOT written by Chandra (see doc/OCR_STRATEGY.md, P1)\n'
  failed=1
fi
if [ "$err" -gt 0 ]; then
  printf '=> %s archive(s) could not be verified (missing/unreadable) - treat as failure\n' "$err"
  failed=1
fi
exit "$failed"
INNER
inner_exit=$?
set -e

# Surface any docs that lacked an archive file — they are unverifiable.
if [ "$missing" -gt 0 ]; then
  printf '=> %s document(s) have no archive file (content-only) - unverifiable, treat as failure\n' "$missing"
  inner_exit=1
fi

exit "$inner_exit"
