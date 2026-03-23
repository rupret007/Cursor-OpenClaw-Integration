#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
export ANDREA_REPO_ROOT="${BASE_DIR}"
# shellcheck disable=SC1091
source "${BASE_DIR}/scripts/macos/andrea_launchagent_lib.sh"

say() {
  echo "[andrea_openclaw_gateway_refresh] $*"
}

warn() {
  echo "[andrea_openclaw_gateway_refresh] WARN: $*" >&2
}

main() {
  andrea_load_runtime_env
  if [[ "${ANDREA_OPENCLAW_GATEWAY_REFRESH_ON_LOGIN:-1}" != "1" ]]; then
    say "Skip OpenClaw gateway restart (ANDREA_OPENCLAW_GATEWAY_REFRESH_ON_LOGIN!=1)"
    return 0
  fi
  if andrea_restart_openclaw_gateway_debounced "launchagent"; then
    if [[ "${ANDREA_LAST_GATEWAY_RESTART_ACTION:-}" == "skipped_recent" ]]; then
      say "OpenClaw gateway refresh skipped because bootstrap already handled it"
    else
      say "OpenClaw gateway refresh complete"
    fi
    return 0
  fi
  local rc=$?
  if [[ "${rc}" -eq 127 ]]; then
    warn "openclaw not found on PATH"
  else
    warn "OpenClaw gateway restart failed"
  fi
  return "${rc}"
}

main "$@"
