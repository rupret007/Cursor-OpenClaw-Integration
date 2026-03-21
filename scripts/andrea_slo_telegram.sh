#!/usr/bin/env bash
# Timed Telegram Bot API getMe (no token printed). Optional SLO gate.
# Usage:
#   TELEGRAM_BOT_TOKEN=... bash scripts/andrea_slo_telegram.sh
#   TELEGRAM_SLO_MAX_MS=8000 TELEGRAM_BOT_TOKEN=... bash scripts/andrea_slo_telegram.sh
# Skip: TELEGRAM_SLO_SKIP=1 bash scripts/andrea_slo_telegram.sh
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAX_MS="${TELEGRAM_SLO_MAX_MS:-8000}"
SKIP="${TELEGRAM_SLO_SKIP:-0}"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "OK  $*"; }

if [[ "$SKIP" == "1" ]]; then
  echo "(Skip Telegram SLO: TELEGRAM_SLO_SKIP=1)"
  exit 0
fi

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  echo "(Skip Telegram SLO: TELEGRAM_BOT_TOKEN unset)"
  exit 0
fi

cd "$BASE_DIR"

echo "-------- Telegram getMe latency (token not logged) --------"
set +e
out="$(python3 "${BASE_DIR}/scripts/andrea_slo_telegram_probe.py" 2>&1)"
rc=$?
set -e
echo "$out"
[[ "$rc" -eq 0 ]] || fail "Telegram getMe failed (check token / network)"

_ms="$(echo "$out" | sed -n 's/^elapsed_ms=//p' | head -1)"
if [[ -n "${_ms}" ]] && [[ "${_ms}" =~ ^[0-9]+$ ]] && [[ "${_ms}" -gt "${MAX_MS}" ]]; then
  fail "Telegram SLO exceeded: ${_ms}ms > ${MAX_MS}ms (tune TELEGRAM_SLO_MAX_MS if needed)"
fi
pass "telegram getMe within ${MAX_MS}ms (or unset elapsed)"
