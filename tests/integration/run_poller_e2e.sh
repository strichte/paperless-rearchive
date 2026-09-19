#!/bin/bash
# End-to-end run of the *real* poller entrypoint against the live stack.
#
#   tests/run_poller_e2e.sh [extra docker env args...]
#
# Runs one poll cycle (REARCHIVE_RUN_ONCE=true) as the deployed container would,
# against the running paperless/Postgres/Chandra services. Output: /tmp/poller_e2e.txt
set -euo pipefail

REPO=/home/paperless/paperless-rearchive
ENV_FILE=/home/paperless/paperless-lxc/.env.paperless-gpt
SECRETS=/home/paperless/paperless-lxc/secrets
OUT=/tmp/poller_e2e.txt

TOKEN=$(grep '^PAPERLESS_API_TOKEN=' "$ENV_FILE" | cut -d= -f2-)

docker rm -f rearch-poller >/dev/null 2>&1 || true

: > "$OUT"
docker run --rm --name rearch-poller \
  --network backend \
  -v /data/paperless/media/documents/archive:/archive \
  -v /data/paperless/archive-backups:/archive-backups \
  -v "${SECRETS}/chandra_api_key:/run/secrets/chandra_api_key:ro" \
  -v "${SECRETS}/paperless_db_paperless_passwd:/run/secrets/paperless_db_paperless_passwd:ro" \
  -e PAPERLESS_BASE_URL=http://paperless:8000 \
  -e PAPERLESS_API_TOKEN="${TOKEN}" \
  -e PAPERLESS_CHANDRA_SERVER_URL=http://ai:8110/v1 \
  -e PAPERLESS_CHANDRA_MODEL_NAME=chandra-ocr-2-q8 \
  -e PAPERLESS_CHANDRA_CONTENT_FORMAT=markdown \
  -e PAPERLESS_CHANDRA_API_KEY_FILE=/run/secrets/chandra_api_key \
  -e PAPERLESS_DBHOST=postgres \
  -e PAPERLESS_DBNAME=paperless \
  -e PAPERLESS_DBUSER=paperless \
  -e PAPERLESS_DBPASS_FILE=/run/secrets/paperless_db_paperless_passwd \
  -e REARCHIVE_RUN_ONCE=true \
  -e REARCHIVE_BATCH_LIMIT=1 \
  -e REARCHIVE_LOG_LEVEL=INFO \
  "$@" \
  paperless-rearchive:latest >>"$OUT" 2>&1

echo "exit=$?" >>"$OUT"