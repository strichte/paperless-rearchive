#!/bin/bash
# Reset the re-archive test document to a clean pre-run baseline.
#
#   tests/reset_baseline.sh <doc_id> <archive_path_relative_to_archive_dir> <baseline_archive>
#
# Restores the archive file from a backup, syncs documents_document.archive_checksum
# to the restored bytes, sets a recognisable content marker and resets the tags to
# just the trigger tag. Output goes to /tmp/reset.txt.
set -euo pipefail

DOC="${1:?doc_id required}"
REL="${2:?archive relative path required}"
BASELINE="${3:?baseline archive file required}"

ARCHIVE_ROOT=/data/paperless/media/documents/archive
TARGET="${ARCHIVE_ROOT}/${REL}"
OUT=/tmp/reset.txt
: > "$OUT"
exec >>"$OUT" 2>&1

echo "=== restoring $TARGET from $BASELINE ==="
cp -f "$BASELINE" "$TARGET"

NEW_SUM=$(sha256sum "$TARGET" | cut -d' ' -f1)
echo "restored sha256: $NEW_SUM  ($(stat -c%s "$TARGET") bytes)"

echo
echo "=== syncing archive_checksum + baseline content ==="
docker exec postgres psql -U paperless -d paperless -v ON_ERROR_STOP=1 -c \
  "UPDATE documents_document SET archive_checksum = '${NEW_SUM}', content = '[BASELINE] pre-rearchive content' WHERE id = ${DOC};"

echo
echo "=== reseting tags to trigger only ==="
docker exec postgres psql -U paperless -d paperless -v ON_ERROR_STOP=1 -c \
  "DELETE FROM documents_document_tags WHERE document_id = ${DOC}
     AND tag_id IN (SELECT id FROM documents_tag WHERE name LIKE 're-ocr-%');
   INSERT INTO documents_document_tags (document_id, tag_id)
     SELECT ${DOC}, id FROM documents_tag WHERE name = 're-ocr-all'
     ON CONFLICT DO NOTHING;"

echo
echo "=== state after reset ==="
docker exec postgres psql -U paperless -d paperless -t -A -F'|' -c \
  "SELECT id, length(content), archive_checksum FROM documents_document WHERE id=${DOC};"
docker exec postgres psql -U paperless -d paperless -t -A -F'|' -c \
  "SELECT t.name FROM documents_document_tags dt JOIN documents_tag t ON t.id=dt.tag_id
    WHERE dt.document_id=${DOC} ORDER BY t.id;"
echo "=== done ==="
