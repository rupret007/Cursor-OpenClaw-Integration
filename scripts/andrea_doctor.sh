#!/usr/bin/env bash
# Single entry: security + capability grade + reliability probes + optional OpenClaw probe.
# Usage: bash scripts/andrea_doctor.sh
#        SKIP_OPENCLAW_PROBE=1 bash scripts/andrea_doctor.sh
#        STRICT_SECURITY=1 bash scripts/andrea_doctor.sh   # fail on security warnings too
#        MODEL_GUARD_ON_FAIL=1 bash scripts/andrea_doctor.sh
#        OPENCLAW_ENFORCE=1 bash scripts/andrea_doctor.sh
#        ANDREA_SYNC_DOCTOR=1 ANDREA_SYNC_URL=http://127.0.0.1:8765 bash scripts/andrea_doctor.sh
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export STRICT="${STRICT_SECURITY:-0}"
SKIP_OPENCLAW="${SKIP_OPENCLAW_PROBE:-0}"
MODEL_GUARD_ON_FAIL="${MODEL_GUARD_ON_FAIL:-0}"
OPENCLAW_ENFORCE="${OPENCLAW_ENFORCE:-0}"

cd "$BASE_DIR"

echo "╔════════════════════════════════════════╗"
echo "║  Andrea doctor (masterclass health)   ║"
echo "╚════════════════════════════════════════╝"
echo ""

echo ">>> [1/4] Security sanity (repo)"
bash "${BASE_DIR}/scripts/andrea_security_sanity.sh"
echo ""

echo ">>> [2/4] Capability matrix summary + readiness grade"
python3 "${BASE_DIR}/scripts/andrea_capabilities.py" | head -20
echo "…"
python3 "${BASE_DIR}/scripts/andrea_readiness_grade.py" || {
  echo "Grade C — fix blocked rows above, then re-run." >&2
  exit 1
}
echo ""

echo ">>> [3/4] Reliability probes (deterministic)"
bash "${BASE_DIR}/scripts/andrea_reliability_probes.sh"
echo ""

if [[ "${OPENCLAW_ENFORCE}" == "1" ]]; then
  echo ">>> [3.5/4] OpenClaw enforcer (sync + required skills + probe)"
  bash "${BASE_DIR}/scripts/andrea_openclaw_enforce.sh" \
    || echo "WARN: openclaw enforcer failed; continuing to direct probe step" >&2
  echo ""
fi

if [[ "${ANDREA_SYNC_DOCTOR:-0}" == "1" ]]; then
  echo ">>> [3.6/4] Andrea lockstep health (ANDREA_SYNC_DOCTOR=1)"
  python3 "${BASE_DIR}/scripts/andrea_sync_health.py" || {
    echo "Lockstep health failed — start python3 scripts/andrea_sync_server.py or unset ANDREA_SYNC_REQUIRED" >&2
    exit 1
  }
  echo ""
fi

echo ">>> [4/4] OpenClaw model probe (optional)"
if [[ "${SKIP_OPENCLAW}" == "1" ]]; then
  echo "(Skip: SKIP_OPENCLAW_PROBE=1)"
elif command -v openclaw >/dev/null 2>&1; then
  _ms="${OPENCLAW_PROBE_MS:-30000}"
  if ! openclaw models status --probe --probe-timeout "${_ms}" --probe-concurrency 1; then
    echo "WARN: openclaw probe failed — check keys / network / timeout is ms" >&2
    if [[ "${MODEL_GUARD_ON_FAIL}" == "1" ]]; then
      echo "INFO: running model guard remediation (MODEL_GUARD_ON_FAIL=1)"
      bash "${BASE_DIR}/scripts/andrea_model_guard.sh" \
        || echo "WARN: model guard remediation failed; see logs and docs/ANDREA_MODEL_POLICY.md" >&2
    fi
  fi
else
  echo "(Skip: openclaw not on PATH)"
fi
echo ""

echo "=== Andrea doctor complete ==="
echo "Docs: docs/ANDREA_OPERATIONS_PLAYBOOK.md | docs/ANDREA_SECURITY.md | docs/ANDREA_MODEL_POLICY.md | docs/ANDREA_LOCKSTEP_ARCHITECTURE.md"
