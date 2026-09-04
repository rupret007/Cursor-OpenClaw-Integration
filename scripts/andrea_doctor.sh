#!/usr/bin/env bash
# Single entry: security + readiness next-step contract + reliability probes + optional OpenClaw probe.
# Usage: bash scripts/andrea_doctor.sh --offline
#        bash scripts/andrea_doctor.sh --offline --receipt /tmp/andrea-doctor-receipt.json
#        bash scripts/andrea_doctor.sh
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
RECEIPT_PATH=""
RECEIPT_READINESS_JSON=""
RECEIPT_WRITTEN=0
SECURITY_STATUS="not_run"
RELIABILITY_STATUS="not_run"
OPENCLAW_STATUS="not_run"

usage() {
  echo "Usage: bash scripts/andrea_doctor.sh [--offline] [--receipt PATH]"
  echo "  --offline  Run security, the readiness next-step contract (Andrea / Bob / owner),"
  echo "             and deterministic probes without the live OpenClaw model probe."
  echo "             Prints and reprints the operator recap even on Grade C; exit 1 still"
  echo "             means Grade C (owner must act). This is the operator-testable path."
  echo "  --receipt  With --offline, atomically write a mode-600, redaction-safe JSON"
  echo "             handoff receipt for Codex, Grok, Claude, dashboards, and scripts."
  echo "             After write, print a verify summary. Consumers then run"
  echo "             python3 scripts/andrea_doctor_receipt.py --consume PATH --audience bob"
}

reprint_operator_recap() {
  awk '
    /^--- Operator next steps ---$/ {keep=1}
    keep {print}
    /^--- End operator next steps ---$/ {if (keep) exit}
  '
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --offline)
      SKIP_OPENCLAW=1
      ;;
    --receipt)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "--receipt requires a file path" >&2
        usage >&2
        exit 2
      fi
      RECEIPT_PATH="$2"
      shift
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

if [[ -n "${RECEIPT_PATH}" && "${SKIP_OPENCLAW}" != "1" ]]; then
  echo "--receipt is offline-only; add --offline so no live probe can run" >&2
  usage >&2
  exit 2
fi

if [[ -n "${RECEIPT_PATH}" ]]; then
  RECEIPT_READINESS_JSON="$(mktemp -t andrea-doctor-readiness.XXXXXX)"
fi

emit_receipt() {
  local doctor_rc="$1"
  [[ -n "${RECEIPT_PATH}" ]] || return 0
  python3 "${BASE_DIR}/scripts/andrea_doctor_receipt.py" \
    --readiness-json "${RECEIPT_READINESS_JSON}" \
    --security-status "${SECURITY_STATUS}" \
    --reliability-status "${RELIABILITY_STATUS}" \
    --openclaw-status "${OPENCLAW_STATUS}" \
    --exit-code "${doctor_rc}" \
    --output "${RECEIPT_PATH}"
  RECEIPT_WRITTEN=1
}

on_exit() {
  local doctor_rc=$?
  trap - EXIT
  if [[ -n "${RECEIPT_PATH}" && "${RECEIPT_WRITTEN}" != "1" ]]; then
    if ! emit_receipt "${doctor_rc}"; then
      echo "FAIL: could not write requested doctor receipt: ${RECEIPT_PATH}" >&2
      doctor_rc=1
    fi
  fi
  if [[ -n "${RECEIPT_READINESS_JSON}" ]]; then
    rm -f "${RECEIPT_READINESS_JSON}"
  fi
  exit "${doctor_rc}"
}

trap on_exit EXIT

cd "$BASE_DIR"

echo "╔════════════════════════════════════════╗"
echo "║  Andrea doctor (masterclass health)   ║"
echo "╚════════════════════════════════════════╝"
echo ""

echo ">>> [1/4] Security sanity (repo)"
if bash "${BASE_DIR}/scripts/andrea_security_sanity.sh"; then
  SECURITY_STATUS="passed"
else
  SECURITY_RC=$?
  SECURITY_STATUS="failed"
  exit "${SECURITY_RC}"
fi
echo ""

echo ">>> [2/4] Readiness grade + Andrea/Bob next-step contract"
echo "Full capability table (optional): python3 scripts/andrea_capabilities.py"
GRADE_RC=0
if [[ -n "${RECEIPT_READINESS_JSON}" ]]; then
  GRADE_OUT="$(python3 "${BASE_DIR}/scripts/andrea_readiness_grade.py" --json-out "${RECEIPT_READINESS_JSON}")" || GRADE_RC=$?
else
  GRADE_OUT="$(python3 "${BASE_DIR}/scripts/andrea_readiness_grade.py")" || GRADE_RC=$?
fi
printf '%s\n' "${GRADE_OUT}"
if [[ "${GRADE_RC}" -ne 0 ]]; then
  echo "Grade C — follow Next for the owner first, then Next for Andrea / Next for the coding agent (Bob)." >&2
  echo "Do not hunt a capability table, send a message, install a skill, or restart a gateway unless the owner acts." >&2
  if [[ "${SKIP_OPENCLAW}" != "1" ]]; then
    exit "${GRADE_RC}"
  fi
  echo "Continuing the offline doctor so probes and the operator recap still run." >&2
fi
echo ""

echo ">>> [3/4] Reliability probes (deterministic)"
if bash "${BASE_DIR}/scripts/andrea_reliability_probes.sh"; then
  RELIABILITY_STATUS="passed"
else
  RELIABILITY_RC=$?
  RELIABILITY_STATUS="failed"
  exit "${RELIABILITY_RC}"
fi
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
  OPENCLAW_STATUS="skipped_offline"
  echo "(Skip: offline mode / SKIP_OPENCLAW_PROBE=1)"
elif command -v openclaw >/dev/null 2>&1; then
  _ms="${OPENCLAW_PROBE_MS:-30000}"
  if ! openclaw models status --probe --probe-timeout "${_ms}" --probe-concurrency 1; then
    OPENCLAW_STATUS="failed"
    echo "WARN: openclaw probe failed — check keys / network / timeout is ms" >&2
    if [[ "${MODEL_GUARD_ON_FAIL}" == "1" ]]; then
      echo "INFO: running model guard remediation (MODEL_GUARD_ON_FAIL=1)"
      bash "${BASE_DIR}/scripts/andrea_model_guard.sh" \
        || echo "WARN: model guard remediation failed; see logs and docs/ANDREA_MODEL_POLICY.md" >&2
    fi
  else
    OPENCLAW_STATUS="passed"
  fi
else
  OPENCLAW_STATUS="not_run"
  echo "(Skip: openclaw not on PATH)"
fi
echo ""

echo "=== Operator recap (Andrea / Bob / owner) ==="
printf '%s\n' "${GRADE_OUT}" | reprint_operator_recap
echo ""

if [[ "${SKIP_OPENCLAW}" == "1" ]]; then
  echo "Offline doctor complete."
  echo "Use the Next for Andrea / Next for the coding agent (Bob) / Next for the owner lines above."
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

FINAL_RC="${GRADE_RC}"
if [[ -n "${RECEIPT_PATH}" ]]; then
  if emit_receipt "${FINAL_RC}"; then
    echo "Machine handoff receipt: ${RECEIPT_PATH}"
    if ! python3 "${BASE_DIR}/scripts/andrea_doctor_receipt.py" --summary "${RECEIPT_PATH}"; then
      echo "FAIL: doctor receipt failed verify/summary; treat as blocked." >&2
      FINAL_RC=1
    fi
  else
    echo "FAIL: could not write requested doctor receipt: ${RECEIPT_PATH}" >&2
    FINAL_RC=1
  fi
fi

exit "${FINAL_RC}"
