#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLI="${BASE_DIR}/scripts/cursor_openclaw.py"
TEST_FILE="${BASE_DIR}/tests/test_cursor_openclaw.py"

echo "[1/10] Validate required files..."
for f in "$CLI" "$TEST_FILE" "${BASE_DIR}/README.md" "${BASE_DIR}/.env.example" "${BASE_DIR}/docs/ANDREA_OPENCLAW_HYBRID_SKILLS.md" "${BASE_DIR}/docs/ANDREA_LOCKSTEP_ARCHITECTURE.md" "${BASE_DIR}/docs/ANDREA_LOCKSTEP_REVIEW_FINDINGS.md" "${BASE_DIR}/docs/ANDREA_ALEXA_INTEGRATION.md" "${BASE_DIR}/scripts/setup_admin.sh" "${BASE_DIR}/scripts/env_loader.py" "${BASE_DIR}/scripts/cursor_api_common.py" "${BASE_DIR}/scripts/handoff_context.py" "${BASE_DIR}/scripts/exhaustive_feature_check.sh" "${BASE_DIR}/scripts/andrea_capabilities.py" "${BASE_DIR}/scripts/andrea_reliability_probes.sh" "${BASE_DIR}/scripts/dotenv_set_key.py" "${BASE_DIR}/scripts/openclaw_apply_openai_key.sh" "${BASE_DIR}/scripts/andrea_readiness_grade.py" "${BASE_DIR}/scripts/andrea_security_sanity.sh" "${BASE_DIR}/scripts/andrea_slo_check.sh" "${BASE_DIR}/scripts/andrea_doctor.sh" "${BASE_DIR}/scripts/andrea_model_guard.sh" "${BASE_DIR}/scripts/andrea_openclaw_enforce.sh" "${BASE_DIR}/scripts/andrea_release_gate.sh" "${BASE_DIR}/scripts/andrea_slo_telegram.sh" "${BASE_DIR}/scripts/andrea_slo_telegram_probe.py" "${BASE_DIR}/scripts/andrea_sync_server.py" "${BASE_DIR}/scripts/andrea_sync_health.py" "${BASE_DIR}/scripts/andrea_sync_cursor_report.py" "${BASE_DIR}/scripts/andrea_sync_publish_capabilities.py" "${BASE_DIR}/scripts/andrea_kill_switch.sh" "${BASE_DIR}/scripts/andrea_lockstep_telegram_e2e.py" "${BASE_DIR}/scripts/andrea_communication_smoke.sh" "${BASE_DIR}/scripts/macos/install_andrea_launchagents.sh"; do
  [[ -f "$f" ]] || { echo "Missing file: $f" >&2; exit 1; }
done
bash -n "${BASE_DIR}/scripts/setup_admin.sh"
bash -n "${BASE_DIR}/scripts/exhaustive_feature_check.sh"
bash -n "${BASE_DIR}/scripts/andrea_reliability_probes.sh"
bash -n "${BASE_DIR}/scripts/openclaw_apply_openai_key.sh"
bash -n "${BASE_DIR}/scripts/andrea_security_sanity.sh"
bash -n "${BASE_DIR}/scripts/andrea_slo_check.sh"
bash -n "${BASE_DIR}/scripts/andrea_doctor.sh"
bash -n "${BASE_DIR}/scripts/andrea_model_guard.sh"
bash -n "${BASE_DIR}/scripts/andrea_openclaw_enforce.sh"
bash -n "${BASE_DIR}/scripts/andrea_release_gate.sh"
bash -n "${BASE_DIR}/scripts/andrea_slo_telegram.sh"
bash -n "${BASE_DIR}/scripts/andrea_kill_switch.sh"
bash -n "${BASE_DIR}/scripts/andrea_communication_smoke.sh"
bash -n "${BASE_DIR}/scripts/macos/install_andrea_launchagents.sh"
python3 -m py_compile "${BASE_DIR}/scripts/env_loader.py"
python3 -m py_compile "${BASE_DIR}/scripts/cursor_api_common.py"
python3 -m py_compile "${BASE_DIR}/scripts/handoff_context.py"
python3 -m py_compile "${BASE_DIR}/scripts/andrea_capabilities.py"
python3 -m py_compile "${BASE_DIR}/scripts/andrea_readiness_grade.py"
python3 -m py_compile "${BASE_DIR}/scripts/andrea_slo_telegram_probe.py"
python3 -m py_compile "${BASE_DIR}/scripts/dotenv_set_key.py"
python3 -m py_compile "${BASE_DIR}/scripts/andrea_sync_server.py"
python3 -m py_compile "${BASE_DIR}/scripts/andrea_sync_health.py"
python3 -m py_compile "${BASE_DIR}/scripts/andrea_sync_cursor_report.py"
python3 -m py_compile "${BASE_DIR}/scripts/andrea_sync_publish_capabilities.py"
python3 -m py_compile "${BASE_DIR}/scripts/andrea_lockstep_telegram_e2e.py"
while IFS= read -r _py; do
  python3 -m py_compile "$_py"
done < <(find "${BASE_DIR}/services" -name "*.py" 2>/dev/null | sort)

echo "[2/10] Python syntax compile..."
python3 -m py_compile "$CLI"

echo "[3/10] Unit tests..."
python3 -m unittest discover -s "${BASE_DIR}/tests" -p "test_*.py"

echo "[4/10] Dry-run create payload..."
CURSOR_API_KEY="dummy_test_key" python3 "$CLI" \
  --json create-agent \
  --prompt "Test prompt" \
  --repository "https://github.com/foo/bar" \
  --ref main \
  --branch-name "cursor/test" \
  --dry-run >/dev/null

CURSOR_API_KEY="dummy_test_key" python3 "$CLI" \
  --json create-agent \
  --intent code-review \
  --repository "https://github.com/foo/bar" \
  --ref main \
  --branch-name "cursor/intent-only" \
  --dry-run >/dev/null

echo "[5/10] Diagnostic command..."
CURSOR_API_KEY="dummy_test_key" python3 "$CLI" --json diagnose >/dev/null

echo "[6/10] Andrea security sanity (non-strict)..."
bash "${BASE_DIR}/scripts/andrea_security_sanity.sh"

echo "[7/10] Andrea reliability probes..."
bash "${BASE_DIR}/scripts/andrea_reliability_probes.sh"

echo "[8/10] Readiness grade (informational; may be B/C on minimal env)..."
python3 "${BASE_DIR}/scripts/andrea_readiness_grade.py" || true

echo "[9/10] Exhaustive offline feature check..."
bash "${BASE_DIR}/scripts/exhaustive_feature_check.sh"

echo "[10/10] Optional live communication smoke (RUN_COMM_SMOKE=1)..."
if [[ "${RUN_COMM_SMOKE:-0}" == "1" ]]; then
  bash "${BASE_DIR}/scripts/andrea_communication_smoke.sh"
else
  echo "(Skip: set RUN_COMM_SMOKE=1 with ANDREA_SYNC_URL / TELEGRAM_BOT_TOKEN for live checks)"
fi

echo "All integration checks passed."
