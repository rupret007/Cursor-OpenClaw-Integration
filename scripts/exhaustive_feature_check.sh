#!/usr/bin/env bash
# Exercise every CLI surface (offline). Optional live API checks if CURSOR_API_KEY is set.
# Usage: bash scripts/exhaustive_feature_check.sh
#        RUN_LIVE_API=1 bash scripts/exhaustive_feature_check.sh
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLI="${BASE_DIR}/scripts/cursor_openclaw.py"
HANDOFF="${BASE_DIR}/skills/cursor_handoff/scripts/cursor_handoff.py"
RUN_LIVE_API="${RUN_LIVE_API:-0}"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "OK  $*"; }

expect_fail() {
  local desc="$1"
  shift
  if "$@"; then
    fail "$desc (expected non-zero exit)"
  fi
  pass "$desc (expected failure)"
}

cd "$BASE_DIR"

echo "======== cursor_openclaw.py ========"
python3 -m py_compile "${BASE_DIR}/scripts/cursor_api_common.py" || fail "py_compile cursor_api_common"
python3 -m py_compile "$CLI" || fail "py_compile cursor_openclaw"
pass "py_compile cursor_openclaw + cursor_api_common"

python3 "$CLI" --help >/dev/null || fail "top-level --help"
pass "top-level --help"

for cmd in diagnose whoami models list-agents agent-status conversation artifacts artifact-download-url create-agent followup stop-agent delete-agent; do
  python3 "$CLI" "$cmd" --help >/dev/null || fail "help $cmd"
done
pass "all subcommand --help"

# Diagnose: explicit empty key blocks .env fill (key already in env as empty)
CURSOR_API_KEY="" python3 "$CLI" --json diagnose | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("ok") is True; assert d.get("api_key_present") is False; assert "dotenv_files_loaded" in d; assert d.get("openai_api_key_present") is False; assert d.get("openai_api_enabled") is False; assert d.get("openai_api_key_redacted") == "***"' || fail "diagnose empty key"
pass "diagnose with CURSOR_API_KEY empty"

CURSOR_API_KEY="" python3 "$CLI" diagnose --show-key | grep -q "api_key_redacted" || fail "diagnose --show-key text"
pass "diagnose --show-key (text)"

python3 "$CLI" --version | grep -q "cursor_openclaw" || fail "cursor_openclaw --version"
pass "cursor_openclaw --version"

python3 "$HANDOFF" --version | grep -q "cursor_handoff" || fail "cursor_handoff --version"
pass "cursor_handoff --version"

bash "${BASE_DIR}/skills/cursor_handoff/scripts/cursor_cli_fallback.sh" --help >/dev/null || fail "cursor_cli_fallback --help"
pass "cursor_cli_fallback --help"

# Validation errors (exit 2)
expect_fail "create-agent both repository and pr-url" \
  env CURSOR_API_KEY=dummy_test_key python3 "$CLI" --json create-agent \
  --prompt "x" \
  --repository "https://github.com/foo/bar" \
  --ref main \
  --pr-url "https://github.com/foo/bar/pull/1" \
  --branch-name "b" \
  --dry-run

expect_fail "agent-status invalid id" \
  env CURSOR_API_KEY=dummy_test_key python3 "$CLI" --json agent-status --id "../bad"

expect_fail "create-agent missing repo/pr" \
  env CURSOR_API_KEY=dummy_test_key python3 "$CLI" --json create-agent \
  --prompt "x" --branch-name "b" --dry-run

expect_fail "list-agents bad limit" \
  env CURSOR_API_KEY=dummy_test_key python3 "$CLI" --json list-agents --limit 0

expect_fail "list-agents bad limit 101" \
  env CURSOR_API_KEY=dummy_test_key python3 "$CLI" --json list-agents --limit 101

expect_fail "create-agent negative poll" \
  env CURSOR_API_KEY=dummy_test_key python3 "$CLI" --json create-agent \
  --prompt "x" --repository "https://github.com/a/b" --ref main --branch-name "c" --poll-attempts -1 --dry-run

expect_fail "create-agent branch newline" \
  env CURSOR_API_KEY=dummy_test_key python3 "$CLI" --json create-agent \
  --prompt "x" --repository "https://github.com/a/b" --ref main --branch-name $'evil\ninj' --dry-run

expect_fail "common args bad timeout" \
  env CURSOR_API_KEY=dummy_test_key python3 "$CLI" --timeout-seconds 0 --json whoami

expect_fail "invalid CURSOR_BASE_URL scheme" \
  env CURSOR_API_KEY=dummy_test_key CURSOR_BASE_URL="ftp://bad" python3 "$CLI" --json whoami

# Dry-run create (no network)
env CURSOR_API_KEY=dummy_test_key python3 "$CLI" --json create-agent \
  --prompt "Test prompt" \
  --repository "https://github.com/foo/bar" \
  --ref main \
  --branch-name "cursor/test" \
  --dry-run | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("dry_run") is True' || fail "create dry-run"
pass "create-agent --dry-run payload"

# create-agent with pr-url (mutually exclusive source)
env CURSOR_API_KEY=dummy_test_key python3 "$CLI" --json create-agent \
  --prompt "p" \
  --pr-url "https://github.com/foo/bar/pull/1" \
  --branch-name "b" \
  --auto-create-pr true \
  --dry-run | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["payload"]["source"].get("prUrl")' || fail "create pr-url dry-run"
pass "create-agent --pr-url --dry-run"

if [[ "$RUN_LIVE_API" == "1" ]]; then
  echo "-------- LIVE API (RUN_LIVE_API=1) --------"
  python3 "$CLI" --json whoami | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("ok") is True, d' || fail "live whoami"
  pass "live whoami"
  python3 "$CLI" --json models | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("ok") is True, d' || fail "live models"
  pass "live models"
  python3 "$CLI" --json list-agents --limit 1 | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("ok") is True, d' || fail "live list-agents"
  pass "live list-agents --limit 1"
else
  echo "(Skip live API: set RUN_LIVE_API=1 to hit whoami/models/list-agents)"
fi

echo "======== cursor_handoff.py ========"
python3 -m py_compile "$HANDOFF" || fail "py_compile handoff"
python3 -m py_compile "${BASE_DIR}/skills/cursor_handoff/scripts/env_loader.py" || fail "py_compile env_loader"
python3 -m py_compile "${BASE_DIR}/skills/cursor_handoff/scripts/cursor_api_common.py" || fail "py_compile cursor_api_common"

python3 "$HANDOFF" --help >/dev/null || fail "handoff --help"
pass "handoff --help"

# Diagnose text mode (not "Handoff submitted")
out="$(CURSOR_API_KEY="" python3 "$HANDOFF" --diagnose 2>&1)" || true
echo "$out" | grep -q "Diagnostics complete" || fail "handoff diagnose text header"
echo "$out" | grep -q "openai_api_key_present" || fail "handoff diagnose text openai"
echo "$out" | grep -q "Handoff submitted successfully" && fail "handoff diagnose must not say submitted" || true
pass "handoff --diagnose text output"

CURSOR_API_KEY="" python3 "$HANDOFF" --diagnose --json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("diagnose") is True; c=d.get("checks") or {}; assert c.get("openai_api_key_present") is False; assert c.get("openai_api_enabled") is False' || fail "handoff diagnose json"
pass "handoff --diagnose --json"

# Dry-run text
out="$(CURSOR_API_KEY=dummy_test_key python3 "$HANDOFF" --repo "$BASE_DIR" --prompt "x" --dry-run 2>&1)" || true
echo "$out" | grep -q "Dry run" || fail "handoff dry-run text"
pass "handoff --dry-run text"

# Backend-unavailable dry-run
out="$(CURSOR_API_KEY="" python3 "$HANDOFF" --repo "$BASE_DIR" --prompt "x" --dry-run 2>&1)" || true
echo "$out" | grep -q "Dry run" || fail "handoff dry-run no key"
pass "handoff --dry-run without API key"

expect_fail "handoff empty prompt" python3 "$HANDOFF" --repo "$BASE_DIR" --prompt ""

expect_fail "handoff invalid read_only" python3 "$HANDOFF" --repo "$BASE_DIR" --prompt "x" --read-only maybe

expect_fail "handoff invalid repo" python3 "$HANDOFF" --repo "not-a-valid-repo-!!!" --prompt "x" --dry-run

expect_fail "handoff bad timeout" python3 "$HANDOFF" --repo "$BASE_DIR" --prompt "x" --timeout-seconds 0 --dry-run

expect_fail "handoff bad poll" python3 "$HANDOFF" --repo "$BASE_DIR" --prompt "x" --poll-max-attempts -1 --dry-run

expect_fail "handoff bad cli-timeout" \
  python3 "$HANDOFF" --repo "$BASE_DIR" --prompt "x" --cli-timeout-seconds -1 --dry-run

expect_fail "handoff branch newline" \
  python3 "$HANDOFF" --repo "$BASE_DIR" --prompt "x" --branch $'evil\ninj' --dry-run

# Modes (dry-run)
for mode in api cli auto; do
  CURSOR_API_KEY=dummy_test_key python3 "$HANDOFF" --repo "$BASE_DIR" --prompt "t" --mode "$mode" --dry-run --json >/dev/null || fail "handoff mode $mode"
done
pass "handoff --mode api|cli|auto dry-run"

# URL repo + slug
CURSOR_API_KEY=dummy_test_key python3 "$HANDOFF" --repo "https://github.com/foo/bar" --prompt "z" --dry-run --json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("repo_url")' || fail "handoff url repo"
pass "handoff GitHub URL repo"

CURSOR_API_KEY=dummy_test_key python3 "$HANDOFF" --repo "foo/bar" --prompt "z" --dry-run --json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "github.com" in (d.get("repo_url") or "")' || fail "handoff slug"
pass "handoff owner/repo slug"

# PR URL in dry-run payload would need --json inspect create path - skip deep assert; just run
CURSOR_API_KEY=dummy_test_key python3 "$HANDOFF" --repo "$BASE_DIR" --prompt "z" --pr-url "https://github.com/a/b/pull/2" --dry-run --json >/dev/null || fail "handoff pr-url dry-run"
pass "handoff --pr-url dry-run"

echo "======== All exhaustive checks passed ========"
