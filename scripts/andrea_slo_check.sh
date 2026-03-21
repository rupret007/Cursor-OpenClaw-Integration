#!/usr/bin/env bash
# SLO-oriented checks: readiness grade + optional OpenClaw model probes.
# Usage: bash scripts/andrea_slo_check.sh
#        SKIP_OPENCLAW_PROBE=1 bash scripts/andrea_slo_check.sh
#        OPENCLAW_PROBE_MS=30000 bash scripts/andrea_slo_check.sh
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKIP_OPENCLAW_PROBE="${SKIP_OPENCLAW_PROBE:-0}"
TELEGRAM_SLO="${TELEGRAM_SLO:-0}"
# Per-probe timeout in milliseconds (OpenClaw CLI convention)
OPENCLAW_PROBE_MS="${OPENCLAW_PROBE_MS:-30000}"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "OK  $*"; }

cd "$BASE_DIR"

echo "======== Andrea SLO check ========"

echo "-------- Readiness grade (A/B/C) --------"
python3 "${BASE_DIR}/scripts/andrea_readiness_grade.py" || fail "readiness grade C or capabilities failure"
pass "andrea_readiness_grade.py"

if [[ "$TELEGRAM_SLO" == "1" ]]; then
  echo "-------- Telegram getMe SLO (TELEGRAM_SLO=1) --------"
  bash "${BASE_DIR}/scripts/andrea_slo_telegram.sh" || fail "telegram SLO"
else
  echo "(Skip Telegram SLO: set TELEGRAM_SLO=1 and TELEGRAM_BOT_TOKEN)"
fi

if [[ "$SKIP_OPENCLAW_PROBE" == "1" ]]; then
  echo "(Skip OpenClaw model probe: SKIP_OPENCLAW_PROBE=1)"
else
  if command -v openclaw >/dev/null 2>&1; then
    echo "-------- OpenClaw model probe (timeout ${OPENCLAW_PROBE_MS} ms) --------"
    _t0="$(python3 -c 'import time; print(time.perf_counter())')"
    openclaw models status --probe --probe-timeout "${OPENCLAW_PROBE_MS}" --probe-concurrency 1 \
      || fail "openclaw models status --probe failed"
    _t1="$(python3 -c 'import time; print(time.perf_counter())')"
    _wall_ms="$(python3 -c "print(int((float('${_t1}') - float('${_t0}')) * 1000))")"
    echo "openclaw_probe_wall_ms=${_wall_ms} (wall-clock; for SLO log in ${BASE_DIR}/docs/ANDREA_READINESS_REPORT.md)"
    pass "openclaw models probe"
  else
    echo "(Skip OpenClaw: not on PATH)"
  fi
fi

echo "======== Andrea SLO check passed ========"

