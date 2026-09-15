#!/bin/bash
# End-to-end test of the content-only trigger (re-ocr-content).
#
# Verifies that the content key is rewritten *and* the archive file is left
# byte-identical. Output: /tmp/content_e2e.txt
set -euo pipefail

DOC=5820
REL='Passports_Visas_IDs/DE/2026/2026-01-07_re-archive-test.pdf'
ARCHIVE="/data/paperless/media/documents/archive/${REL}"
OUT=/tmp/content_e2e.txt
: > "$OUT"

run_sql() {
  docker exec postgres psql -U paperless -d paperless -v ON_ERROR_STOP=1 -t -A -c "$1"
}

{
  echo "=== 1. prepare: marker content + re-ocr-content trigger ==="
  run_sql "UPDATE documents_document SET content='[BASELINE] content-only test' WHERE id=${DOC};"
  run_sql "DELETE FROM documents_document_tags WHERE document_id=${DOC}
             AND tag_id IN (SELECT id FROM documents_tag WHERE name LIKE 're-ocr-%');"
  run_sql "INSERT INTO documents_document_tags (document_id, tag_id)
             SELECT ${DOC}, id FROM documents_tag WHERE name='re-ocr-content';"

  echo "before: $(run_sql "SELECT length(content) FROM documents_document WHERE id=${DOC};") chars"

  echo
  echo "=== 2. archive sha256 BEFORE (must not change) ==="
  BEFORE_SUM=$(sha256sum "$ARCHIVE" | cut -d' ' -f1)
  echo "$BEFORE_SUM"

  echo
  echo "=== 3. run one poll cycle ==="
  bash /home/paperless/paperless-rearchive/tests/integration/run_poller_e2e.sh >/dev/null 2>&1 || true
  grep -E 'processing|strategy|Archive|archive|Updated|complete|error|Error' /tmp/poller_e2e.txt || true

  echo
  echo "=== 4. results ==="
  echo "after:  $(run_sql "SELECT length(content) FROM documents_document WHERE id=${DOC};") chars"
  echo "content head: $(run_sql "SELECT left(content, 80) FROM documents_document WHERE id=${DOC};")"
  echo "tags: $(run_sql "SELECT string_agg(t.name, ',') FROM documents_document_tags dt
            JOIN documents_tag t ON t.id=dt.tag_id WHERE dt.document_id=${DOC};")"

  AFTER_SUM=$(sha256sum "$ARCHIVE" | cut -d' ' -f1)
  echo "archive sha256 AFTER:  $AFTER_SUM"
  echo "archive sha256 BEFORE: $BEFORE_SUM"
  if [ "$BEFORE_SUM" = "$AFTER_SUM" ]; then
    echo "RESULT: archive UNCHANGED (correct for re-ocr-content)"
  else
    echo "RESULT: ARCHIVE CHANGED (WRONG for re-ocr-content)"
  fi

  echo "db archive_checksum: $(run_sql "SELECT archive_checksum FROM documents_document WHERE id=${DOC};")"
  echo "=== done ==="
} >>"$OUT" 2>&1
