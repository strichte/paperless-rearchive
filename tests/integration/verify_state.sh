#!/bin/bash
# Dump the current state of the re-archive test document for manual inspection.
# Usage: bash verify_state.sh [doc_id]
DOC="${1:-5820}"
OUT=/tmp/verify.txt
: > "$OUT"

exec >>"$OUT" 2>&1

echo "=== running containers ==="
docker ps -a --format '{{.Names}}\t{{.Status}}' | grep -i rearch || echo "(none)"

echo
echo "=== DB row (id|content_len|archive_filename|archive_checksum) ==="
docker exec postgres psql -U paperless -d paperless -t -A -F'|' -c \
  "SELECT id, length(content), archive_filename, archive_checksum FROM documents_document WHERE id=${DOC};"

echo
echo "=== tags -> counts for all documents ==="
docker exec postgres psql -U paperless -d paperless -t -A -F'|' -c \
  "SELECT t.name, count(dt.document_id) FROM documents_tag t
   LEFT JOIN documents_document_tags dt ON dt.tag_id = t.id
   WHERE t.name LIKE 're-ocr%' GROUP BY t.name ORDER BY t.name;"

echo
echo "=== content head/tail ==="
docker exec postgres psql -U paperless -d paperless -t -A -c \
  "SELECT left(content, 200) FROM documents_document WHERE id=${DOC};"
echo "..."
docker exec postgres psql -U paperless -d paperless -t -A -c \
  "SELECT right(content, 200) FROM documents_document WHERE id=${DOC};"

echo
echo "=== archive dir listing (with backups) ==="
D=/data/paperless/media/documents/archive/Passports_Visas_IDs/DE/2026
ls -l "$D"

echo
echo "=== sha256 of current archive vs DB ==="
sha256sum "$D/2026-01-07_re-archive-test.pdf"
echo "DB: $(docker exec postgres psql -U paperless -d paperless -t -A -c \
  "SELECT archive_checksum FROM documents_document WHERE id=${DOC};")"

echo
echo "=== immutable original untouched ==="
ls -l /data/paperless/media/documents/originals/Passports_Visas_IDs/DE/2026/2026-01-07_re-archive-test.pdf
sha256sum /data/paperless/media/documents/originals/Passports_Visas_IDs/DE/2026/2026-01-07_re-archive-test.pdf

echo
echo "=== sqlite-free git status ==="
cd /home/paperless/paperless-rearchive || exit 1
git status --short
echo "--- last 3 commits ---"
git --no-pager log --oneline | head -3
