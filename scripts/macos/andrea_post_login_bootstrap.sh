#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
export ANDREA_REPO_ROOT="${BASE_DIR}"
# shellcheck disable=SC1091
source "${BASE_DIR}/scripts/macos/andrea_launchagent_lib.sh"
LOG_PREFIX="[andrea_post_login_bootstrap]"
export PATH="${HOME}/.npm-global/bin:${HOME}/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

say() {
  echo "${LOG_PREFIX} $*"
}

warn() {
  echo "${LOG_PREFIX} WARN: $*" >&2
}

sync_cursor_handoff_skill() {
  if [[ "${ANDREA_OPENCLAW_SYNC_SKILLS_ON_LOGIN:-1}" != "1" ]]; then
    say "Skip skill sync (ANDREA_OPENCLAW_SYNC_SKILLS_ON_LOGIN!=1)"
    return 0
  fi
  local src="${BASE_DIR}/skills/cursor_handoff"
  local dest="${HOME}/.openclaw/workspace/skills/cursor_handoff"
  if [[ ! -d "$src" ]]; then
    warn "cursor_handoff skill source missing: $src"
    return 0
  fi
  mkdir -p "$(dirname "$dest")"
  rm -rf "$dest"
  cp -R "$src" "$dest"
  say "Synced cursor_handoff skill into OpenClaw workspace"
}

restart_openclaw_gateway() {
  if [[ "${ANDREA_OPENCLAW_GATEWAY_REFRESH_ON_LOGIN:-1}" != "1" ]]; then
    say "Skip OpenClaw gateway restart (ANDREA_OPENCLAW_GATEWAY_REFRESH_ON_LOGIN!=1)"
    return 0
  fi
  if andrea_restart_openclaw_gateway_debounced "post_login_bootstrap"; then
    if [[ "${ANDREA_LAST_GATEWAY_RESTART_ACTION:-}" == "skipped_recent" ]]; then
      say "OpenClaw gateway restart already handled recently"
    else
      say "Restarted OpenClaw gateway"
    fi
    return 0
  fi
  local rc=$?
  if [[ "${rc}" -eq 127 ]]; then
    warn "openclaw not found on PATH"
  else
    warn "openclaw gateway restart failed"
  fi
  return 0
}

wait_for_sync_health() {
  local base="${ANDREA_SYNC_URL:-http://127.0.0.1:8765}"
  local timeout="${ANDREA_BOOTSTRAP_SYNC_WAIT_SECONDS:-90}"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    if curl -fsS "${base}/v1/health" >/dev/null 2>&1; then
      say "Andrea sync is healthy at ${base}"
      return 0
    fi
    sleep 2
  done
  warn "Timed out waiting for Andrea sync health at ${base}"
  return 1
}

publish_capabilities() {
  if [[ -z "${ANDREA_SYNC_INTERNAL_TOKEN:-}" ]]; then
    warn "ANDREA_SYNC_INTERNAL_TOKEN unset; skipping capability publish"
    return 0
  fi
  if ! python3 "${BASE_DIR}/scripts/andrea_sync_publish_capabilities.py"; then
    warn "Capability publish failed"
    return 1
  fi
  say "Published capability snapshot"
}

ensure_telegram_webhook() {
  if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    warn "TELEGRAM_BOT_TOKEN unset; skipping webhook bootstrap"
    return 0
  fi
  if [[ -z "${ANDREA_SYNC_PUBLIC_BASE:-}" ]]; then
    warn "ANDREA_SYNC_PUBLIC_BASE unset; skipping webhook bootstrap"
    return 0
  fi
  if python3 "${BASE_DIR}/scripts/andrea_lockstep_telegram_e2e.py" webhook-info --require-match --attempts 3 --retry-delay-sec 2 >/dev/null 2>&1; then
    say "Telegram webhook already healthy"
    return 0
  fi
  if ! python3 "${BASE_DIR}/scripts/andrea_lockstep_telegram_e2e.py" set-webhook; then
    warn "Telegram webhook bootstrap failed; checking whether the existing registration is still healthy"
    if python3 "${BASE_DIR}/scripts/andrea_lockstep_telegram_e2e.py" webhook-info --require-match --attempts 3 --retry-delay-sec 2 >/dev/null 2>&1; then
      say "Telegram webhook remained healthy despite bootstrap set-webhook failure"
      return 0
    fi
    return 1
  fi
  if ! python3 "${BASE_DIR}/scripts/andrea_lockstep_telegram_e2e.py" webhook-info --require-match --attempts 3 --retry-delay-sec 2; then
    warn "Telegram webhook verification failed after bootstrap"
    return 1
  fi
  say "Ensured Telegram webhook registration"
}

main() {
  cd "$BASE_DIR"
  andrea_load_runtime_env

  sync_cursor_handoff_skill
  restart_openclaw_gateway

  if ! wait_for_sync_health; then
    warn "Post-login bootstrap incomplete: Andrea sync never became healthy"
    return 1
  fi
  publish_capabilities
  ensure_telegram_webhook
  say "Post-login bootstrap complete"
}

main "$@"
