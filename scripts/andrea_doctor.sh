#!/usr/bin/env bash
# Single entry: security + readiness next-step contract + reliability probes + optional OpenClaw probe.
# Usage: bash scripts/andrea_doctor.sh
#        bash scripts/andrea_doctor.sh --offline
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

usage() {
  echo "Usage: bash scripts/andrea_doctor.sh [--offline]"
  echo "  --offline  Run security, the readiness next-step contract (Andrea / Bob / owner),"
  echo "             and deterministic probes without the live OpenClaw model probe."
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --offline)
      SKIP_OPENCLAW=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

cd "$BASE_DIR"

echo "╔════════════════════════════════════════╗"
echo "║  Andrea doctor (masterclass health)   ║"
echo "╚════════════════════════════════════════╝"
echo ""

echo ">>> [1/4] Security sanity (repo)"
bash "${BASE_DIR}/scripts/andrea_security_sanity.sh"
echo ""

echo ">>> [2/4] Readiness grade + Andrea/Bob next-step contract"
echo "Full capability table (optional): python3 scripts/andrea_capabilities.py"
python3 "${BASE_DIR}/scripts/andrea_readiness_grade.py" || {
  echo "Grade C — follow Next action / Next for Andrea / Next for the coding agent (Bob) above." >&2
  echo "Do not hunt a capability table, send a message, install a skill, or restart a gateway unless the owner acts." >&2
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
  echo "(Skip: offline mode / SKIP_OPENCLAW_PROBE=1)"
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

if [[ "${SKIP_OPENCLAW}" == "1" ]]; then
  echo "Offline doctor complete."
  echo "Use the Next for Andrea / Next for the coding agent (Bob) / Next for the owner lines from the readiness grade."
  echo "Holds: no live send, Private API stays off, no BlueBubbles live send, no credential writes, no gateway restart unless the owner asks."
else
  echo "Sprint readiness note:"
  echo "- intentional tri-LLM sprints need a healthy OpenClaw probe and cursor_handoff availability; OPENCLAW_ENFORCE=1 is the strict baseline check"
  echo "- sessions_spawn attachments are optional and only matter for deliberate multi-session handoffs, not normal Telegram chat/news flows"
  echo "- live send still requires the exact standalone phrases send it / send it now / send now"
fi
echo ""

echo "=== Andrea doctor complete ==="
echo "Docs: docs/ANDREA_OPERATIONS_PLAYBOOK.md | docs/ANDREA_SECURITY.md | docs/ANDREA_MODEL_POLICY.md | docs/ANDREA_LOCKSTEP_ARCHITECTURE.md"
