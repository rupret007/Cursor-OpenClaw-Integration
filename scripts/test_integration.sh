#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLI="${BASE_DIR}/scripts/cursor_openclaw.py"
TEST_FILE="${BASE_DIR}/tests/test_cursor_openclaw.py"

echo "[1/7] Validate required files..."
for f in "$CLI" "$TEST_FILE" "${BASE_DIR}/README.md" "${BASE_DIR}/.env.example" "${BASE_DIR}/scripts/setup_admin.sh" "${BASE_DIR}/scripts/env_loader.py" "${BASE_DIR}/scripts/cursor_api_common.py" "${BASE_DIR}/scripts/exhaustive_feature_check.sh" "${BASE_DIR}/scripts/andrea_capabilities.py" "${BASE_DIR}/scripts/andrea_reliability_probes.sh" "${BASE_DIR}/scripts/dotenv_set_key.py" "${BASE_DIR}/scripts/openclaw_apply_openai_key.sh"; do
  [[ -f "$f" ]] || { echo "Missing file: $f" >&2; exit 1; }
done
bash -n "${BASE_DIR}/scripts/setup_admin.sh"
bash -n "${BASE_DIR}/scripts/exhaustive_feature_check.sh"
bash -n "${BASE_DIR}/scripts/andrea_reliability_probes.sh"
bash -n "${BASE_DIR}/scripts/openclaw_apply_openai_key.sh"
python3 -m py_compile "${BASE_DIR}/scripts/env_loader.py"
python3 -m py_compile "${BASE_DIR}/scripts/cursor_api_common.py"
python3 -m py_compile "${BASE_DIR}/scripts/andrea_capabilities.py"
python3 -m py_compile "${BASE_DIR}/scripts/dotenv_set_key.py"

echo "[2/7] Python syntax compile..."
python3 -m py_compile "$CLI"

echo "[3/7] Unit tests..."
python3 -m unittest discover -s "${BASE_DIR}/tests" -p "test_*.py"

echo "[4/7] Dry-run create payload..."
CURSOR_API_KEY="dummy_test_key" python3 "$CLI" \
  --json create-agent \
  --prompt "Test prompt" \
  --repository "https://github.com/foo/bar" \
  --ref main \
  --branch-name "cursor/test" \
  --dry-run >/dev/null

echo "[5/7] Diagnostic command..."
CURSOR_API_KEY="dummy_test_key" python3 "$CLI" --json diagnose >/dev/null

echo "[6/7] Andrea reliability probes..."
bash "${BASE_DIR}/scripts/andrea_reliability_probes.sh"

echo "[7/7] Exhaustive offline feature check..."
bash "${BASE_DIR}/scripts/exhaustive_feature_check.sh"

echo "All integration checks passed."
