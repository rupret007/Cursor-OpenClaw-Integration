#!/usr/bin/env bash
# Live communication smoke: lockstep health, optional Telegram getMe, optional OpenClaw skills list.
#
# Usage (from repo root):
#   ANDREA_SYNC_URL=http://127.0.0.1:8765 bash scripts/andrea_communication_smoke.sh
#   TELEGRAM_BOT_TOKEN=... ANDREA_SYNC_URL=... bash scripts/andrea_communication_smoke.sh
#
# Exit 0: all configured probes passed, or nothing configured (informational skip).
# Exit 1: a configured probe failed.
#
set -euo pipefail
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0
ran=0

url="${ANDREA_SYNC_URL:-}"
url="${url%/}"
tok="${TELEGRAM_BOT_TOKEN:-}"

if [[ -n "$url" ]]; then
  ran=1
  echo "-------- andrea_sync health ($url) --------"
  if ! out="$(curl -sS -m 8 "${url}/v1/health" 2>&1)"; then
    echo "FAIL: curl health: $out" >&2
    fail=1
  else
    if ! echo "$out" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('ok') is True, d" 2>/dev/null; then
      echo "FAIL: health JSON not ok: ${out:0:400}" >&2
      fail=1
    else
      echo "OK: andrea_sync /v1/health"
    fi
  fi
fi

if [[ -n "$tok" ]]; then
  ran=1
  echo "-------- Telegram getMe --------"
  if ! python3 "${BASE_DIR}/scripts/andrea_slo_telegram_probe.py" 2>&1; then
    fail=1
  else
    echo "OK: telegram probe"
  fi
fi

if command -v openclaw >/dev/null 2>&1; then
  ran=1
  echo "-------- openclaw skills list (first lines) --------"
  set +o pipefail
  out="$(openclaw skills list 2>&1 | head -25)"
  oc="${PIPESTATUS[0]:-0}"
  set -o pipefail
  # 141: SIGPIPE when head closes early after enough output
  if [[ "$oc" -ne 0 && "$oc" -ne 141 ]]; then
    echo "FAIL: openclaw skills list (exit $oc)" >&2
    echo "$out" >&2
    fail=1
  else
    echo "$out"
    echo "OK: openclaw skills list"
  fi
else
  echo "(Skip openclaw: not on PATH)"
fi

if [[ "$ran" -eq 0 ]]; then
  echo "SKIP: no live targets (set ANDREA_SYNC_URL and/or TELEGRAM_BOT_TOKEN; openclaw optional on PATH)"
  exit 0
fi

if [[ "$fail" -ne 0 ]]; then
  echo "FAIL: one or more communication probes failed" >&2
  exit 1
fi
echo "======== Communication smoke passed ========"
exit 0
