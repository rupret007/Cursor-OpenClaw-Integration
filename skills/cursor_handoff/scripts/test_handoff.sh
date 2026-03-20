#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_SCRIPT="${BASE_DIR}/scripts/cursor_handoff.py"
CLI_SCRIPT="${BASE_DIR}/scripts/cursor_cli_fallback.sh"
SKILL_FILE="${BASE_DIR}/SKILL.md"
README_FILE="${BASE_DIR}/README.md"
TEST_FILE="${BASE_DIR}/tests/test_cursor_handoff.py"

echo "[1/6] Checking required files..."
for f in "$PY_SCRIPT" "$CLI_SCRIPT" "$SKILL_FILE" "$README_FILE" "$TEST_FILE"; do
  if [[ ! -f "$f" ]]; then
    echo "Missing required file: $f" >&2
    exit 1
  fi
done
echo "OK: required files found"

echo "[2/6] Checking Python script help..."
python3 "$PY_SCRIPT" --help >/dev/null
echo "OK: help command works"

echo "[3/6] Running basic static checks..."
python3 -m py_compile "$PY_SCRIPT"
bash -n "$CLI_SCRIPT"
echo "OK: syntax checks passed"

echo "[4/6] Running unit tests..."
python3 -m unittest discover -s "${BASE_DIR}/tests" -p "test_*.py"
echo "OK: unit tests passed"

echo "[5/6] Running dry-run example..."
CURSOR_API_KEY="dummy_test_key" python3 "$PY_SCRIPT" \
  --repo "$BASE_DIR" \
  --prompt "Analyze repository structure and propose a plan" \
  --mode auto \
  --read-only true \
  --json \
  --dry-run >/dev/null
echo "OK: dry-run works"

echo "[5b/6] Running backend-unavailable dry-run..."
env -u CURSOR_API_KEY python3 "$PY_SCRIPT" \
  --repo "$BASE_DIR" \
  --prompt "Analyze repository structure and propose a plan" \
  --mode auto \
  --read-only true \
  --json \
  --dry-run >/dev/null
echo "OK: dry-run works without configured backend"

echo "[6/6] Running diagnostics smoke check..."
python3 "$PY_SCRIPT" --diagnose --json >/dev/null
echo "OK: diagnostics works"

echo "Next steps:"
echo "  chmod +x \"$PY_SCRIPT\" \"$CLI_SCRIPT\" \"$BASE_DIR/scripts/test_handoff.sh\""
echo "  openclaw gateway restart"
echo "  openclaw skills list"
echo "  python3 \"$PY_SCRIPT\" --repo \"$BASE_DIR\" --prompt \"Repo analysis\" --mode auto --read-only true --json --dry-run"
