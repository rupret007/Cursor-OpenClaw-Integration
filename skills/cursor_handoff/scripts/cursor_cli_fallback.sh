#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'EOF'
Usage: cursor_cli_fallback.sh "<repo_path>" "<prompt>" "<read_only:true|false>" "[branch]"
       cursor_cli_fallback.sh --check-plan

Checks the current Cursor subscription receipt, then runs one Composer 2.5
invocation using the operator's existing CLI login.
EOF
  exit 0
fi

SEARCH_PATH=()
IFS=: read -r -a PATH_PARTS <<< "${PATH:-/usr/bin:/bin}"
for directory in "${PATH_PARTS[@]}"; do
  if [[ "$directory" == /* ]]; then
    SEARCH_PATH+=("$directory")
  fi
done
PATH="$(IFS=:; echo "${SEARCH_PATH[*]}")"
export PATH="${PATH:-/usr/bin:/bin}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRACKER="$SCRIPT_DIR/../../../first-officer/usage/track.py"
FILTER="$SCRIPT_DIR/cursor_handoff.py"

CLI_ENV=()
for name in HOME PATH LANG LANGUAGE LC_ALL LC_ADDRESS LC_COLLATE LC_CTYPE LC_IDENTIFICATION LC_MEASUREMENT LC_MESSAGES LC_MONETARY LC_NAME LC_NUMERIC LC_PAPER LC_TELEPHONE LC_TIME TZ XDG_CACHE_HOME XDG_CONFIG_DIRS XDG_CONFIG_HOME XDG_DATA_DIRS XDG_DATA_HOME XDG_RUNTIME_DIR XDG_STATE_HOME; do
  if [[ -v "$name" ]]; then
    CLI_ENV+=("$name=${!name}")
  fi
done

check_plan() {
  if [[ ! -f "$TRACKER" ]]; then
    echo "Cursor plan check is unavailable." >&2
    return 5
  fi
  if ! env -i "${CLI_ENV[@]}" CURSOR_PLAN_ACCOUNT="${CURSOR_PLAN_ACCOUNT:-}" \
    python3 -I -B "$TRACKER" --cursor "${CURSOR_PLAN_READING:-}" </dev/null >/dev/null; then
    echo "Cursor fallback requires a readable plan below the warning threshold." >&2
    return 5
  fi
}

if [[ "${1:-}" == "--check-plan" ]]; then
  if [[ "$#" != 1 ]]; then
    echo "The plan check takes no arguments." >&2
    exit 2
  fi
  check_plan
  exit 0
fi

REPO_PATH="${1:-}"
PROMPT_TEXT="${2:-}"
READ_ONLY="${3:-}"
BRANCH_NAME="${4:-}"

if [[ -z "$REPO_PATH" || -z "$PROMPT_TEXT" ]]; then
  echo "Repo path and prompt are required." >&2
  exit 2
fi
if [[ ! -d "$REPO_PATH" ]]; then
  echo "Repo directory is unavailable." >&2
  exit 3
fi
if [[ "$READ_ONLY" != "true" && "$READ_ONLY" != "false" ]]; then
  echo "Read-only mode must be true or false." >&2
  exit 2
fi

check_plan

if [[ -n "${CURSOR_CLI_BIN:-}" ]]; then
  if [[ "$CURSOR_CLI_BIN" == */* && "$CURSOR_CLI_BIN" != /* ]]; then
    echo "Specify a Cursor CLI command on PATH or an absolute executable path." >&2
    exit 4
  fi
  CLI_BIN="$(type -P -- "$CURSOR_CLI_BIN" || true)"
else
  CLI_BIN="$(type -P agent || type -P cursor-agent || true)"
fi
if [[ "$CLI_BIN" != /* || ! -x "$CLI_BIN" || ! -f "$CLI_BIN" ]]; then
  echo "The configured Cursor CLI executable is unavailable." >&2
  exit 4
fi
if [[ ! -r "$FILTER" ]]; then
  echo "The Cursor output privacy filter is unavailable." >&2
  exit 5
fi

CLI_ARGS=(--print --output-format text --model composer-2.5)
MODE_PREFIX="Make only the changes requested in the task."
if [[ "$READ_ONLY" == "true" ]]; then
  CLI_ARGS+=(--mode=ask)
  MODE_PREFIX="Review the task and report findings in read-only mode."
fi
if [[ -n "$BRANCH_NAME" ]]; then
  MODE_PREFIX+=$'\nTarget branch: '"$BRANCH_NAME"
fi

if ! cd "$REPO_PATH" 2>/dev/null; then
  echo "Repo directory is unavailable." >&2
  exit 3
fi
exec 3>&1 4>&2
exec env -i "${CLI_ENV[@]}" "$CLI_BIN" "${CLI_ARGS[@]}" "$MODE_PREFIX"$'\n\n'"$PROMPT_TEXT" </dev/null \
  > >(env -i "${CLI_ENV[@]}" python3 -I -B "$FILTER" --filter-cli-output >&3 3>&- 4>&- 2>/dev/null) \
  2> >(env -i "${CLI_ENV[@]}" python3 -I -B "$FILTER" --filter-cli-output >&4 3>&- 4>&- 2>/dev/null) \
  3>&- 4>&-
