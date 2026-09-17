#!/bin/bash
# Dump the full pdfinfo output for a document's archive and/or original version.
#
# Companion to check_archive_provenance.sh: instead of just the Creator line,
# this prints the *complete* pdfinfo output (pages, page size, producer,
# Creator, file size, ...) so the two versions of the same document can be
# compared by eye.
#
# Usage:
#     tests/integration/pdfinfo_compare.sh -a <doc_id> [...]   # archive only
#     tests/integration/pdfinfo_compare.sh -o <doc_id> [...]   # original only
#     tests/integration/pdfinfo_compare.sh <doc_id> [...]      # both versions
#     tests/integration/pdfinfo_compare.sh -o --doc 4221 --doc 4222
#     tests/integration/pdfinfo_compare.sh /abs/path.pdf [...] # raw path
#
# Flags may be given anywhere and apply to the whole run; the last one wins.
# Bare numeric arguments are document IDs; anything else is a path inside the
# paperless container (absolute, or relative to $MEDIA_ROOT_IN_CONTAINER).
#
# Document IDs are resolved in the DB (documents_document): archive_filename
# for the archive version and filename for the original, relative to archive/
# and originals/ respectively beneath the media documents root.  The PDFs are then read inside the *paperless*
# container, which mounts the whole media tree (originals/ AND archive/) and
# ships poppler-utils.  The paperless-rearchive container only mounts
# /archives, so it cannot see originals and is not used here.
#
# Override via environment:
#     PAPERLESS_CONTAINER      (default paperless)
#     MEDIA_ROOT_IN_CONTAINER  (default /usr/src/paperless/media/documents)
#     PAPERLESS_DB_CONTAINER / PAPERLESS_DB_USER / PAPERLESS_DB_NAME
#
# Exit status: 0 = every requested file was readable, 1 = at least one missing/
# unreadable file, 2 = usage error.
set -euo pipefail

CONTAINER="${PAPERLESS_CONTAINER:-paperless}"
MEDIA_ROOT="${MEDIA_ROOT_IN_CONTAINER:-/usr/src/paperless/media/documents}"

if [ "$#" -eq 0 ]; then
  sed -n '2,33p' "$0"
  exit 2
fi

# ---------------------------------------------------------------------------
# Outer script: argument parsing + DB resolution.
# The docker CLI is available here (on the host) but NOT inside the containers,
# so all docker/DB calls happen at this level.
# ---------------------------------------------------------------------------

mode=both          # both | archive | original
doc_ids=()
paths=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    -a|--archive)  mode=archive; shift ;;
    -o|--original) mode=original; shift ;;
    -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
    -*)
      case "$1" in
        --doc|--doc=*) ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
      esac
      if [ "$1" = --doc ]; then
        [ -n "${2:-}" ] || { echo "--doc requires an ID" >&2; exit 2; }
        doc_ids+=("$2"); shift 2
      else
        doc_ids+=("${1#*=}"); shift
      fi
      ;;
    *)
      if [[ "$1" =~ ^[0-9]+$ ]]; then
        doc_ids+=("$1")
      else
        paths+=("$1")
      fi
      shift
      ;;
  esac
done

if [ "${#doc_ids[@]}" -eq 0 ] && [ "${#paths[@]}" -eq 0 ]; then
  echo "At least one document ID or path is required" >&2
  exit 2
fi
# Validate all IDs before making any DB calls.
for id in "${doc_ids[@]}"; do
  case "$id" in
    ''|*[!0-9]*) echo "not a document ID: '$id'" >&2; exit 2 ;;
  esac
done

# Resolve document IDs to on-disk paths via the DB (same source the sidecar
# itself uses): archive_filename -> under archive/, filename -> under originals/.
missing=0
resolved=()
if [ "${#doc_ids[@]}" -gt 0 ]; then
  db_container="${PAPERLESS_DB_CONTAINER:-postgres}"
  db_user="${PAPERLESS_DB_USER:-paperless}"
  db_name="${PAPERLESS_DB_NAME:-paperless}"
  for id in "${doc_ids[@]}"; do
    case "$id" in
      ''|*[!0-9]*) echo "not a document ID: '$id'" >&2; exit 2 ;;
    esac
    row="$(docker exec "$db_container" psql -X -v ON_ERROR_STOP=1 -U "$db_user" -d "$db_name" -t -A -F '|' \
      -c "select coalesce(filename,''), coalesce(archive_filename,'') from documents_document where id = $id")" \
      || { echo "DB query failed for document $id" >&2; exit 1; }
    if [ -z "$row" ]; then
      printf 'ERROR     document %s\n             (not found in the database)\n' "$id"
      missing=$((missing + 1))
      continue
    fi
    filename="${row%%|*}"
    archive="${row#*|}"
    case "$mode" in
      archive)
        if [ -n "$archive" ]; then
          printf 'resolved  document %s archive  -> %s\n' "$id" "$archive"
          resolved+=("archive/$archive")
        else
          printf 'ERROR     document %s\n             (no archive file)\n' "$id"
          missing=$((missing + 1))
        fi
        ;;
      original)
        if [ -n "$filename" ]; then
          printf 'resolved  document %s original -> %s\n' "$id" "$filename"
          resolved+=("originals/$filename")
        else
          printf 'ERROR     document %s\n             (no original file)\n' "$id"
          missing=$((missing + 1))
        fi
        ;;
      *)
        if [ -n "$archive" ]; then
          printf 'resolved  document %s archive  -> %s\n' "$id" "$archive"
          resolved+=("archive/$archive")
        else
          printf 'ERROR     document %s\n             (no archive file)\n' "$id"
          missing=$((missing + 1))
        fi
        if [ -n "$filename" ]; then
          printf 'resolved  document %s original -> %s\n' "$id" "$filename"
          resolved+=("originals/$filename")
        else
          printf 'ERROR     document %s\n             (no original file)\n' "$id"
          missing=$((missing + 1))
        fi
        ;;
    esac
  done
fi

# Build the argument vector for the inner script.  DB-resolved paths carry an
# explicit kind (archive|originals); raw paths get "path:" and are labelled
# as given, without guessing which version they are.
inner_args=()
for p in "${resolved[@]}"; do
  inner_args+=("$p")
done
for p in "${paths[@]}"; do
  inner_args+=("path:$p")
done

# ---------------------------------------------------------------------------
# Inner script: runs inside the *paperless* container.  Only needs pdfinfo.
# Each argument is either "archive/...", "originals/...", or "path:<raw>"
# (absolute or media-root-relative, printed without a version label).
# ---------------------------------------------------------------------------
set +e
docker exec -i "$CONTAINER" bash -s -- "$MEDIA_ROOT" "${inner_args[@]}" <<'INNER'
set -eu
root="$1"; shift

command -v pdfinfo >/dev/null || { echo "pdfinfo not found in container" >&2; exit 1; }

dump() {
  spec="$1"
  case "$spec" in
    archive/*)   file="$spec"; label=ARCHIVE ;;
    originals/*) file="$spec"; label=ORIGINAL ;;
    path:*)      file="${spec#path:}";      label=FILE ;;
    *)           file="$spec";              label=FILE ;;
  esac
  case "$file" in
    /*) target="$file" ;;
    *)  target="$root/$file" ;;
  esac
  printf '=== %s: %s ===\n' "$label" "$target"
  if [ ! -f "$target" ]; then
    printf 'ERROR     (file not found)\n\n'
    return 1
  fi
  if ! pdfinfo "$target"; then
    printf 'ERROR     (pdfinfo failed)\n\n'
    return 1
  fi
  printf '\n'
}

failed=0
for spec in "$@"; do
  dump "$spec" || failed=1
done
exit "$failed"
INNER
inner_exit=$?
set -e

# Docs that could not be resolved (missing from DB / no archive / no original)
# never reach the inner script; surface them as a failure.
if [ "$missing" -gt 0 ]; then
  inner_exit=1
fi

exit "$inner_exit"
