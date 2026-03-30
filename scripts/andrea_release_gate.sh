#!/usr/bin/env bash
# Pre-release / pre-deploy gate: strict security hygiene + readiness grade A/B + full integration tests.
# Usage: bash scripts/andrea_release_gate.sh
# Fails on: tracked secrets patterns, readiness grade C, any test_integration step.
# OpenClaw skill catalog / eligibility (e.g. bluebubbles) is not exercised here; run
# OPENCLAW_ENFORCE=1 bash scripts/andrea_doctor.sh or scripts/andrea_openclaw_enforce.sh before relying on hybrid skills.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE_DIR"

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "======== Andrea release gate ========"

echo ">>> [1/3] Security sanity (STRICT=1 — warnings fail)"
export STRICT=1
bash "${BASE_DIR}/scripts/andrea_security_sanity.sh"

echo ">>> [2/3] Readiness grade (must not be C)"
python3 "${BASE_DIR}/scripts/andrea_readiness_grade.py" || fail "readiness grade C — fix capability blockers before release"

echo ">>> [3/3] Integration test suite"
bash "${BASE_DIR}/scripts/test_integration.sh"

echo "======== Andrea release gate passed ========"
