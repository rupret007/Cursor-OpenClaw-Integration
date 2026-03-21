#!/usr/bin/env bash
# SLO-oriented checks: readiness grade + optional OpenClaw model probes.
# Usage: bash scripts/andrea_slo_check.sh
#        SKIP_OPENCLAW_PROBE=1 bash scripts/andrea_slo_check.sh
#        OPENCLAW_PROBE_MS=30000 bash scripts/andrea_slo_check.sh
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKIP_OPENCLAW_PROBE="${SKIP_OPENCLAW_PROBE:-0}"
# Per-probe timeout in milliseconds (OpenClaw CLI convention)
OPENCLAW_PROBE_MS="${OPENCLAW_PROBE_MS:-30000}"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "OK  $*"; }

cd "$BASE_DIR"

echo "======== Andrea SLO check ========"

echo "-------- Readiness grade (A/B/C) --------"
python3 "${BASE_DIR}/scripts/andrea_readiness_grade.py" || fail "readiness grade C or capabilities failure"
pass "andrea_readiness_grade.py"

if [[ "$SKIP_OPENCLAW_PROBE" == "1" ]]; then
  echo "(Skip OpenClaw model probe: SKIP_OPENCLAW_PROBE=1)"
else
  if command -v openclaw >/dev/null 2>&1; then
    echo "-------- OpenClaw model probe (timeout ${OPENCLAW_PROBE_MS} ms) --------"
    openclaw models status --probe --probe-timeout "${OPENCLAW_PROBE_MS}" --probe-concurrency 1 \
      || fail "openclaw models status --probe failed"
    pass "openclaw models probe"
  else
    echo "(Skip OpenClaw: not on PATH)"
  fi
fi

echo "======== Andrea SLO check passed ========"

