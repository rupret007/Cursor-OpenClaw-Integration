#!/usr/bin/env bash
# shellcheck shell=bash

if [[ -n "${ANDREA_LAUNCHAGENT_LIB_SOURCED:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi
export ANDREA_LAUNCHAGENT_LIB_SOURCED=1

ANDREA_MACOS_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANDREA_REPO_ROOT="${ANDREA_REPO_ROOT:-$(cd "${ANDREA_MACOS_SCRIPTS_DIR}/../.." && pwd)}"
ANDREA_LAUNCHAGENT_DIR="${ANDREA_LAUNCHAGENT_DIR:-${HOME}/Library/LaunchAgents}"
ANDREA_LOG_DIR="${ANDREA_LOG_DIR:-${HOME}/Library/Logs/andrea}"
ANDREA_RUNTIME_STATE_DIR="${ANDREA_RUNTIME_STATE_DIR:-${HOME}/Library/Application Support/andrea/runtime}"

ANDREA_SYNC_LABEL="com.andrea.andrea-sync"
ANDREA_BOOTSTRAP_LABEL="com.andrea.andrea-post-login-bootstrap"
ANDREA_CLOUDFLARED_LABEL="com.andrea.andrea-cloudflared"
ANDREA_LOCALTUNNEL_LABEL="com.andrea.andrea-localtunnel"
ANDREA_OPENCLAW_REFRESH_LABEL="com.andrea.openclaw-gateway-refresh"
ANDREA_LAST_GATEWAY_RESTART_ACTION=""

andrea_launchctl_domain() {
  printf 'gui/%s' "$(id -u)"
}

andrea_launchagent_plist_path() {
  local label="$1"
  printf '%s/%s.plist' "${ANDREA_LAUNCHAGENT_DIR}" "${label}"
}

andrea_all_launchagent_labels() {
  printf '%s\n' \
    "${ANDREA_SYNC_LABEL}" \
    "${ANDREA_BOOTSTRAP_LABEL}" \
    "${ANDREA_CLOUDFLARED_LABEL}" \
    "${ANDREA_LOCALTUNNEL_LABEL}" \
    "${ANDREA_OPENCLAW_REFRESH_LABEL}"
}

andrea_default_stop_labels_csv() {
  printf '%s,%s,%s,%s,%s' \
    "${ANDREA_SYNC_LABEL}" \
    "${ANDREA_BOOTSTRAP_LABEL}" \
    "${ANDREA_CLOUDFLARED_LABEL}" \
    "${ANDREA_LOCALTUNNEL_LABEL}" \
    "${ANDREA_OPENCLAW_REFRESH_LABEL}"
}

andrea_tunnel_labels() {
  printf '%s\n' "${ANDREA_CLOUDFLARED_LABEL}" "${ANDREA_LOCALTUNNEL_LABEL}"
}

andrea_plist_installed() {
  local label="$1"
  [[ -f "$(andrea_launchagent_plist_path "${label}")" ]]
}

andrea_launchctl_available() {
  command -v launchctl >/dev/null 2>&1
}

andrea_label_loaded() {
  local label="$1"
  andrea_launchctl_available && launchctl print "$(andrea_launchctl_domain)/${label}" >/dev/null 2>&1
}

andrea_load_agent() {
  local label="$1"
  local plist
  plist="$(andrea_launchagent_plist_path "${label}")"
  [[ -f "${plist}" ]] || return 1
  launchctl bootstrap "$(andrea_launchctl_domain)" "${plist}"
}

andrea_bootout_agent() {
  local label="$1"
  local plist
  plist="$(andrea_launchagent_plist_path "${label}")"
  if andrea_label_loaded "${label}"; then
    launchctl bootout "$(andrea_launchctl_domain)/${label}" >/dev/null 2>&1 || true
    return 0
  fi
  if [[ -f "${plist}" ]]; then
    launchctl bootout "$(andrea_launchctl_domain)" "${plist}" >/dev/null 2>&1 || true
  fi
}

andrea_kickstart_agent() {
  local label="$1"
  launchctl kickstart -k "$(andrea_launchctl_domain)/${label}"
}

andrea_load_or_kickstart_agent() {
  local label="$1"
  if andrea_label_loaded "${label}"; then
    andrea_kickstart_agent "${label}"
    return 0
  fi
  andrea_load_agent "${label}"
}

andrea_tunnel_label() {
  local cloud_loaded=0
  local local_loaded=0
  if andrea_label_loaded "${ANDREA_CLOUDFLARED_LABEL}"; then
    cloud_loaded=1
  fi
  if andrea_label_loaded "${ANDREA_LOCALTUNNEL_LABEL}"; then
    local_loaded=1
  fi
  if [[ "${cloud_loaded}" -eq 1 && "${local_loaded}" -eq 0 ]]; then
    printf '%s' "${ANDREA_CLOUDFLARED_LABEL}"
    return 0
  fi
  if [[ "${local_loaded}" -eq 1 && "${cloud_loaded}" -eq 0 ]]; then
    printf '%s' "${ANDREA_LOCALTUNNEL_LABEL}"
    return 0
  fi
  if andrea_plist_installed "${ANDREA_CLOUDFLARED_LABEL}"; then
    printf '%s' "${ANDREA_CLOUDFLARED_LABEL}"
    return 0
  fi
  if andrea_plist_installed "${ANDREA_LOCALTUNNEL_LABEL}"; then
    printf '%s' "${ANDREA_LOCALTUNNEL_LABEL}"
    return 0
  fi
  return 1
}

andrea_load_env_file() {
  local path="$1"
  [[ -f "${path}" ]] || return 0
  set -a
  # shellcheck disable=SC1090
  source "${path}"
  set +a
}

andrea_load_runtime_env() {
  andrea_load_env_file "${ANDREA_REPO_ROOT}/.env"
  andrea_load_env_file "${HOME}/andrea-lockstep.env"
  if [[ -n "${ANDREA_ENV_FILE:-}" ]]; then
    andrea_load_env_file "${ANDREA_ENV_FILE}"
  fi
}

andrea_runtime_state_path() {
  mkdir -p "${ANDREA_RUNTIME_STATE_DIR}"
  printf '%s' "${ANDREA_RUNTIME_STATE_DIR}"
}

andrea_gateway_restart_stamp_path() {
  printf '%s/openclaw-gateway-restart.stamp' "$(andrea_runtime_state_path)"
}

andrea_file_mtime() {
  local path="$1"
  if [[ ! -e "${path}" ]]; then
    printf '0'
    return 0
  fi
  if stat -f %m "${path}" >/dev/null 2>&1; then
    stat -f %m "${path}"
    return 0
  fi
  stat -c %Y "${path}"
}

andrea_gateway_restart_debounce_seconds() {
  printf '%s' "${ANDREA_OPENCLAW_GATEWAY_RESTART_DEBOUNCE_SECONDS:-25}"
}

andrea_gateway_restart_recent() {
  local stamp now last debounce
  stamp="$(andrea_gateway_restart_stamp_path)"
  now="$(date +%s)"
  last="$(andrea_file_mtime "${stamp}")"
  debounce="$(andrea_gateway_restart_debounce_seconds)"
  [[ "${last}" -gt 0 ]] && (( now - last < debounce ))
}

andrea_record_gateway_restart() {
  local stamp
  stamp="$(andrea_gateway_restart_stamp_path)"
  : > "${stamp}"
}

andrea_restart_openclaw_gateway_debounced() {
  local context="${1:-auto}"
  local debounce
  ANDREA_LAST_GATEWAY_RESTART_ACTION=""
  debounce="$(andrea_gateway_restart_debounce_seconds)"
  if ! command -v openclaw >/dev/null 2>&1; then
    ANDREA_LAST_GATEWAY_RESTART_ACTION="missing_openclaw"
    echo "openclaw not found on PATH" >&2
    return 127
  fi
  if andrea_gateway_restart_recent; then
    ANDREA_LAST_GATEWAY_RESTART_ACTION="skipped_recent"
    echo "Skip OpenClaw gateway restart (${context}; recent restart within ${debounce}s)"
    return 0
  fi
  openclaw gateway restart
  ANDREA_LAST_GATEWAY_RESTART_ACTION="restarted"
  andrea_record_gateway_restart
}
