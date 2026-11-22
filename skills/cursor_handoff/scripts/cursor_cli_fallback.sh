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
  - checks the fresh subscription reading in CURSOR_PLAN_READING
  - runs one Composer 2.5 invocation using the existing Cursor login
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRACKER="$SCRIPT_DIR/../../../first-officer/usage/track.py"
if [[ ! -f "$TRACKER" ]]; then
  echo "ERROR: Cursor plan check is unavailable" >&2
  exit 5
fi
if ! usage_report="$(python3 -B "$TRACKER" --cursor "${CURSOR_PLAN_READING:-}")"; then
  printf '%s\n' "$usage_report" >&2
  echo "ERROR: Cursor fallback requires a readable plan below the warning threshold" >&2
  exit 5
fi

cd "$REPO_PATH"

# Use the existing subscription login and a first-party model. A failed launch
# can have consumed usage already, so do not retry it with other arguments.
unset CURSOR_API_KEY
if "$CLI_BIN" --model composer-2.5 "$FINAL_PROMPT"; then
  echo "CLI backend: $CLI_BIN"
  exit 0
fi

echo "ERROR: local CLI handoff failed for binary '$CLI_BIN'" >&2
exit 5
