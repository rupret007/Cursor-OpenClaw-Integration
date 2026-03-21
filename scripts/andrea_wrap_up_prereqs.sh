#!/usr/bin/env bash
# Pre-flight for live wrap-up: env + andrea_sync health (no secrets printed).
#
# Usage:
#   export ANDREA_SYNC_INTERNAL_TOKEN='...'
#   export ANDREA_SYNC_URL='http://127.0.0.1:8765'   # optional
#   bash scripts/andrea_wrap_up_prereqs.sh
#
# Exit 0: ready for bash scripts/andrea_full_cycle.sh
# Exit 1: fix printed items first
#
set -euo pipefail
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE_DIR"

ok=1
say() { echo "[prereqs] $*"; }

if [[ -z "${ANDREA_SYNC_INTERNAL_TOKEN:-}" ]]; then
  say "FAIL: export ANDREA_SYNC_INTERNAL_TOKEN (required for full_cycle publish/kill_switch)"
  ok=0
else
  say "OK: ANDREA_SYNC_INTERNAL_TOKEN is set"
fi

export ANDREA_SYNC_URL="${ANDREA_SYNC_URL:-http://127.0.0.1:8765}"
export ANDREA_SYNC_URL="${ANDREA_SYNC_URL%/}"

if ! command -v curl >/dev/null 2>&1; then
  say "FAIL: curl not on PATH"
  ok=0
fi

if ! out="$(curl -sS -m 10 "${ANDREA_SYNC_URL}/v1/health" 2>&1)"; then
  say "FAIL: cannot reach ${ANDREA_SYNC_URL}/v1/health — start: python3 scripts/andrea_sync_server.py"
  ok=0
elif ! echo "$out" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('ok') is True" 2>/dev/null; then
  say "FAIL: /v1/health not ok: ${out:0:200}"
  ok=0
else
  say "OK: andrea_sync /v1/health"
fi

if [[ -z "${ANDREA_SYNC_TELEGRAM_SECRET:-}" ]]; then
  say "WARN: ANDREA_SYNC_TELEGRAM_SECRET unset (Telegram webhook ingest needs it on server)"
else
  say "OK: ANDREA_SYNC_TELEGRAM_SECRET is set"
fi

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  say "INFO: TELEGRAM_BOT_TOKEN unset (optional for comm smoke / webhook-info)"
else
  say "OK: TELEGRAM_BOT_TOKEN is set"
fi

if command -v openclaw >/dev/null 2>&1; then
  say "OK: openclaw on PATH"
else
  say "WARN: openclaw not on PATH (full_cycle will skip gateway restart)"
fi

if [[ "$ok" -eq 1 ]]; then
  say "Ready: bash scripts/andrea_full_cycle.sh"
  exit 0
fi
say "Fix failures above, then re-run."
exit 1
