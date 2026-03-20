#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLI="${BASE_DIR}/scripts/cursor_openclaw.py"
TEST_FILE="${BASE_DIR}/tests/test_cursor_openclaw.py"

echo "[1/5] Validate required files..."
for f in "$CLI" "$TEST_FILE" "${BASE_DIR}/README.md" "${BASE_DIR}/.env.example"; do
  [[ -f "$f" ]] || { echo "Missing file: $f" >&2; exit 1; }
done

echo "[2/5] Python syntax compile..."
python3 -m py_compile "$CLI"

echo "[3/5] Unit tests..."
python3 -m unittest discover -s "${BASE_DIR}/tests" -p "test_*.py"

echo "[4/5] Dry-run create payload..."
CURSOR_API_KEY="dummy_test_key" python3 "$CLI" \
  --json create-agent \
  --prompt "Test prompt" \
  --repository "https://github.com/foo/bar" \
  --ref main \
  --branch-name "cursor/test" \
  --dry-run >/dev/null

echo "[5/5] Diagnostic command..."
CURSOR_API_KEY="dummy_test_key" python3 "$CLI" --json diagnose >/dev/null

echo "All integration checks passed."
