#!/usr/bin/env bash
# Runtime / auth drift probes with deterministic env isolation for CLI checks.
# Usage: bash scripts/andrea_reliability_probes.sh
#        RUN_LIVE_PROBES=1 bash scripts/andrea_reliability_probes.sh
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLI="${BASE_DIR}/scripts/cursor_openclaw.py"
CAP="${BASE_DIR}/scripts/andrea_capabilities.py"
RUN_LIVE_PROBES="${RUN_LIVE_PROBES:-0}"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "OK  $*"; }

cd "$BASE_DIR"

# Mirror exhaustive_feature_check: prevent parent shell / .env from leaking secrets into subprocess env.
ENV_NO_SECRETS=(
  env
  CURSOR_API_KEY=""
  OPENAI_API_KEY=""
  OPENAI_API_ENABLED="0"
  GH_TOKEN=""
  GITHUB_TOKEN=""
  GEMINI_API_KEY=""
  TELEGRAM_BOT_TOKEN=""
  TELEGRAM_CHAT_ID=""
  BRAVE_SEARCH_API_KEY=""
  BRAVE_ANSWERS_API_KEY=""
  MINIMAX_API_KEY=""
)

echo "======== Andrea reliability probes ========"

python3 -m py_compile "${BASE_DIR}/scripts/andrea_capabilities.py" || fail "py_compile andrea_capabilities"
pass "py_compile andrea_capabilities.py"

[[ -f "$CAP" ]] || fail "missing andrea_capabilities.py"
[[ -f "$CLI" ]] || fail "missing cursor_openclaw.py"

echo "-------- cursor_openclaw diagnose (cleared env secrets) --------"
"${ENV_NO_SECRETS[@]}" python3 "$CLI" --json diagnose \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("ok") is True; assert d.get("api_key_present") is False' \
  || fail "diagnose with empty CURSOR_API_KEY"
pass "diagnose deterministic (no key in env)"

echo "-------- Andrea capability snapshot (non-strict) --------"
# Script may still read repo .env on disk; this probe ensures the runner does not crash.
python3 "$CAP" --json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("ok") is True; assert "rows" in d; assert "summary" in d' \
  || fail "andrea_capabilities json shape"
pass "andrea_capabilities.py --json"

if [[ "$RUN_LIVE_PROBES" == "1" ]]; then
  echo "-------- LIVE: gh auth status --------"
  if command -v gh >/dev/null 2>&1; then
    gh auth status || fail "gh auth status"
    pass "gh auth status"
  else
    echo "(skip: gh not installed)"
  fi
  echo "-------- LIVE: openclaw skills list --------"
  if command -v openclaw >/dev/null 2>&1; then
    openclaw skills list >/dev/null || fail "openclaw skills list"
    pass "openclaw skills list"
  else
    echo "(skip: openclaw not installed)"
  fi
else
  echo "(Skip live probes: set RUN_LIVE_PROBES=1 for gh + openclaw)"
fi

echo "======== Andrea reliability probes passed ========"
