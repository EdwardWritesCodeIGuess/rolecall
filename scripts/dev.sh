#!/bin/sh
# Local dev server (same app as the Docker image). Uses ./data and ./.env if present.
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
export ROLECALL_DATA="${ROLECALL_DATA:-./data}"
exec .venv/bin/rolecall serve --host 127.0.0.1 --port 8080
