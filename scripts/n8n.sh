#!/bin/sh
# Local n8n with the Rolecall workflows (the Docker setup runs the same thing as a service).
# UI: http://localhost:5678  ·  Rolecall must be running (scripts/dev.sh) on ROLECALL_URL.
set -e
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
export ROLECALL_URL="${ROLECALL_URL:-http://127.0.0.1:8080}"
if [ -z "$ROLECALL_API_TOKEN" ]; then
  ROLECALL_API_TOKEN="$(ROLECALL_DATA="${ROLECALL_DATA:-./data}" .venv/bin/python -c 'from rolecall.security import api_token; print(api_token())')"
  export ROLECALL_API_TOKEN
fi
export N8N_USER_FOLDER="$PWD/n8n/data"
export N8N_BLOCK_ENV_ACCESS_IN_NODE=false      # workflows read ROLECALL_URL / ROLECALL_API_TOKEN / NOTIFY_WEBHOOK_URL
export NOTIFY_WEBHOOK_URL="${NOTIFY_WEBHOOK_URL:-$NOTIFY_WEBHOOK}"
export GENERIC_TIMEZONE="${TZ:-America/New_York}"
export N8N_HOST=localhost N8N_PORT="${N8N_PORT:-5678}" N8N_LISTEN_ADDRESS=127.0.0.1
export N8N_DIAGNOSTICS_ENABLED=false N8N_PERSONALIZATION_ENABLED=false
N8N=n8n/.runtime/node_modules/.bin/n8n
if [ ! -x "$N8N" ]; then
  mkdir -p n8n/.runtime
  (cd n8n/.runtime && [ -f package.json ] || echo '{"private":true}' > package.json; npm install --no-audit --no-fund n8n@2.48.6)
fi
if [ "$1" = "--reimport" ] || [ ! -f n8n/data/.rolecall-imported ]; then
  "$N8N" import:workflow --separate --input=n8n/workflows
  for id in RolecallAutopilt RolecallEventsAl RolecallDailyRpt RolecallDiscover; do "$N8N" publish:workflow --id="$id"; done
  touch n8n/data/.rolecall-imported
fi
exec "$N8N" start
