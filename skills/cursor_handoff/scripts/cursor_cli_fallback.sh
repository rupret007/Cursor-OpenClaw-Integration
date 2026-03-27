#!/usr/bin/env bash
set -euo pipefail

# Safe local fallback wrapper for Cursor CLI-style agent tools.
# Usage:
#   cursor_cli_fallback.sh "<repo_path>" "<prompt>" "<read_only:true|false>" "[branch]"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'EOF'
Usage: cursor_cli_fallback.sh "<repo_path>" "<prompt>" "<read_only:true|false>" "[branch]"

Executes a best-effort handoff to local Cursor CLI tools:
  1) agent
  2) cursor-agent

The script:
  - validates repo path
  - composes a mode-aware prompt
  - tries multiple invocation styles safely
EOF
  exit 0
fi

REPO_PATH="${1:-}"
PROMPT_TEXT="${2:-}"
READ_ONLY="${3:-false}"
BRANCH_NAME="${4:-}"

if [[ -z "$REPO_PATH" || -z "$PROMPT_TEXT" ]]; then
  echo "ERROR: repo path and prompt are required" >&2
  exit 2
fi

if [[ ! -d "$REPO_PATH" ]]; then
  echo "ERROR: repo path does not exist: $REPO_PATH" >&2
  exit 3
fi

CLI_BIN=""
if [[ -n "${CURSOR_CLI_BIN:-}" ]]; then
  if command -v "$CURSOR_CLI_BIN" >/dev/null 2>&1; then
    CLI_BIN="$CURSOR_CLI_BIN"
  else
    echo "ERROR: CURSOR_CLI_BIN is set but not found in PATH: $CURSOR_CLI_BIN" >&2
    exit 4
  fi
elif command -v agent >/dev/null 2>&1; then
  CLI_BIN="agent"
elif command -v cursor-agent >/dev/null 2>&1; then
  CLI_BIN="cursor-agent"
else
  echo "ERROR: neither 'agent' nor 'cursor-agent' is installed/in PATH" >&2
  exit 4
fi

MODE_PREFIX=""
if [[ "$READ_ONLY" != "true" && "$READ_ONLY" != "false" ]]; then
  echo "ERROR: read_only must be true|false, got: $READ_ONLY" >&2
  exit 2
fi

if [[ "$READ_ONLY" == "true" ]]; then
  MODE_PREFIX="[READ-ONLY ANALYSIS MODE] Do not edit files, commit, or open PRs. Provide findings/plan only."
else
  MODE_PREFIX="[EDIT MODE] Ask to cancel all jobs."
fi

BRANCH_NOTE=""
if [[ -n "$BRANCH_NAME" ]]; then
  BRANCH_NOTE="Target branch: $BRANCH_NAME"
fi

FINAL_PROMPT="$MODE_PREFIX
$BRANCH_NOTE
$PROMPT_TEXT"

# Capture help text once for capability probing.
HELP_TEXT="$("$CLI_BIN" --help </dev/null 2>&1 || true)"

run_attempt() {
  local label="$1"
  shift
  if "$@"; then
    echo "CLI backend: $CLI_BIN"
    echo "Invocation: $label"
    return 0
  fi
  return 1
}

cd "$REPO_PATH"

# Attempt 1: common --cwd + --prompt style
if [[ "$HELP_TEXT" == *"--cwd"* && "$HELP_TEXT" == *"--prompt"* ]]; then
  run_attempt "cwd+prompt flags" "$CLI_BIN" --cwd "$REPO_PATH" --prompt "$FINAL_PROMPT" && exit 0
fi

# Attempt 2: common --repo + --prompt style
if [[ "$HELP_TEXT" == *"--repo"* && "$HELP_TEXT" == *"--prompt"* ]]; then
  run_attempt "repo+prompt flags" "$CLI_BIN" --repo "$REPO_PATH" --prompt "$FINAL_PROMPT" && exit 0
fi

# Attempt 3: prompt via positional argument
run_attempt "positional prompt" "$CLI_BIN" "$FINAL_PROMPT" && exit 0

echo "ERROR: local CLI handoff failed for binary '$CLI_BIN'" >&2
exit 5
