#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ANDREA_REPO_ROOT="${BASE_DIR}"
# shellcheck disable=SC1091
source "${BASE_DIR}/scripts/macos/andrea_launchagent_lib.sh"

say() {
  echo "[andrea_services] $*"
}

warn() {
  echo "[andrea_services] WARN: $*" >&2
}

die() {
  echo "[andrea_services] FAIL: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  bash scripts/andrea_services.sh status [all|gateway|sync|tunnel|bootstrap]
  bash scripts/andrea_services.sh start [all|gateway|sync|tunnel|bootstrap]
  bash scripts/andrea_services.sh stop [all|gateway|sync|tunnel|bootstrap]
  bash scripts/andrea_services.sh restart [all|gateway|sync|tunnel|bootstrap]
  bash scripts/andrea_services.sh bootstrap
  bash scripts/andrea_services.sh labels
  bash scripts/andrea_services.sh install-launchagents [--with-cloudflared|--with-localtunnel] [--with-openclaw-refresh] [--load]

Notes:
  - The canonical auto-start model is per-user LaunchAgents in gui/$UID.
  - "all" manages the Andrea sync LaunchAgent, the preferred tunnel LaunchAgent,
    and the OpenClaw gateway service, then runs the post-login bootstrap chain.
  - install-launchagents forwards directly to scripts/macos/install_andrea_launchagents.sh.
EOF
}

andrea_load_runtime_env
export ANDREA_SYNC_URL="${ANDREA_SYNC_URL:-http://127.0.0.1:8765}"
export ANDREA_SYNC_URL="${ANDREA_SYNC_URL%/}"

command="${1:-status}"
shift || true
args=("$@")

require_launchctl() {
  andrea_launchctl_available || die "launchctl not found on PATH"
}

require_openclaw() {
  command -v openclaw >/dev/null 2>&1 || die "openclaw not found on PATH"
}

gateway_status_text() {
  openclaw gateway status 2>&1 || true
}

gateway_status_needs_repair() {
  local text="$1"
  [[ "${text}" == *"not loaded"* || "${text}" == *"Service not installed"* || "${text}" == *"Service unit not found"* ]]
}

repair_gateway_service() {
  require_openclaw
  say "Reinstalling OpenClaw gateway service"
  openclaw gateway install --force >/dev/null
}

describe_launchagent() {
  local label="$1"
  local role="$2"
  local installed="no"
  local loaded="no"
  if andrea_plist_installed "${label}"; then
    installed="yes"
  fi
  if andrea_label_loaded "${label}"; then
    loaded="yes"
  fi
  printf '%s: label=%s installed=%s loaded=%s\n' "${role}" "${label}" "${installed}" "${loaded}"
}

warn_if_multiple_tunnels_installed() {
  if andrea_plist_installed "${ANDREA_CLOUDFLARED_LABEL}" && andrea_plist_installed "${ANDREA_LOCALTUNNEL_LABEL}"; then
    warn "Both cloudflared and localtunnel LaunchAgents are installed; cloudflared is preferred until one is removed."
  fi
}

status_gateway() {
  if ! command -v openclaw >/dev/null 2>&1; then
    warn "openclaw not found on PATH"
    return 1
  fi
  local rc=0
  say "OpenClaw gateway status"
  openclaw gateway status || { warn "openclaw gateway status failed"; rc=1; }
  say "OpenClaw health"
  openclaw health || { warn "openclaw health failed"; rc=1; }
  return "${rc}"
}

status_sync_health() {
  if ! command -v curl >/dev/null 2>&1; then
    warn "curl not found on PATH"
    return 1
  fi
  local body
  if ! body="$(curl -sS -m 20 "${ANDREA_SYNC_URL}/v1/health" 2>/dev/null)"; then
    warn "Andrea sync health unavailable at ${ANDREA_SYNC_URL}"
    return 1
  fi
  say "Andrea sync health"
  printf '%s' "${body}" | python3 -c 'import json,sys; d=json.load(sys.stdin); k=d.get("kill_switch") or {}; print("ok={} capability_digest_age_seconds={} kill_switch_engaged={}".format(d.get("ok"), d.get("capability_digest_age_seconds"), k.get("engaged")))' \
    || { warn "Andrea sync health response was invalid"; return 1; }
  printf '%s' "${body}" | python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("ok") else 1)' \
    || { warn "Andrea sync health reported ok=false"; return 1; }
}

runtime_snapshot_body() {
  if ! command -v curl >/dev/null 2>&1; then
    warn "curl not found on PATH"
    return 1
  fi
  curl -sS -m 20 "${ANDREA_SYNC_URL}/v1/runtime-snapshot"
}

status_runtime_truth() {
  local body
  if ! body="$(runtime_snapshot_body 2>/dev/null)"; then
    warn "Andrea sync runtime snapshot unavailable at ${ANDREA_SYNC_URL}"
    return 1
  fi
  say "Andrea sync runtime truth"
  printf '%s' "${body}" | python3 -c 'import json,sys; d=json.load(sys.stdin); r=d.get("runtime") or {}; t=r.get("telegram") or {}; w=r.get("webhook") or {}; print("pid={} public_base={} delegate_lane={} webhook_status={} digest_status={} capability_digest_age_seconds={}".format(r.get("pid"), t.get("public_base") or "-", t.get("delegate_lane") or "-", w.get("status") or "-", r.get("capability_digest_status") or "-", r.get("capability_digest_age_seconds")))' \
    || { warn "Andrea sync runtime snapshot response was invalid"; return 1; }
  printf '%s' "${body}" | python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("ok") else 1)' \
    || { warn "Andrea sync runtime snapshot reported ok=false"; return 1; }
}

status_webhook() {
  local body
  if ! body="$(runtime_snapshot_body 2>/dev/null)"; then
    warn "Andrea sync runtime snapshot unavailable at ${ANDREA_SYNC_URL}"
    return 1
  fi
  say "Telegram webhook truth from running daemon"
  printf '%s' "${body}" | python3 -c 'import json,sys; d=json.load(sys.stdin); r=d.get("runtime") or {}; t=r.get("telegram") or {}; w=r.get("webhook") or {}; print("status={} required={} public_base={} reason={}".format(w.get("status") or "-", w.get("required"), t.get("public_base") or "-", w.get("reason") or "")); current=w.get("current_url") or ""; expected=w.get("expected_url") or ""; print("current_url={}".format(current or "-")); print("expected_url={}".format(expected or "-"))' \
    || { warn "Telegram webhook runtime snapshot response was invalid"; return 1; }
  printf '%s' "${body}" | python3 -c 'import json,sys; d=json.load(sys.stdin); r=d.get("runtime") or {}; t=r.get("telegram") or {}; w=r.get("webhook") or {}; needs=bool(w.get("required") or t.get("bot_token_configured") or t.get("public_base")); status=str(w.get("status") or ""); ok=bool(d.get("ok")); raise SystemExit(0 if ok and ((not needs and status in {"", "unconfigured"}) or status == "healthy") else 1)' \
    || { warn "Telegram webhook truth from running daemon is unhealthy"; return 1; }
}

status_all() {
  local rc=0
  warn_if_multiple_tunnels_installed
  say "LaunchAgent status"
  describe_launchagent "${ANDREA_SYNC_LABEL}" "andrea_sync"
  describe_launchagent "${ANDREA_BOOTSTRAP_LABEL}" "post_login_bootstrap"
  describe_launchagent "${ANDREA_CLOUDFLARED_LABEL}" "cloudflared"
  describe_launchagent "${ANDREA_LOCALTUNNEL_LABEL}" "localtunnel"
  describe_launchagent "${ANDREA_OPENCLAW_REFRESH_LABEL}" "openclaw_gateway_refresh_legacy"
  if tunnel_label="$(andrea_tunnel_label 2>/dev/null)"; then
    say "Preferred tunnel label: ${tunnel_label}"
  else
    say "Preferred tunnel label: none installed"
  fi
  status_gateway || rc=1
  status_sync_health || rc=1
  status_webhook || rc=1
  return "${rc}"
}

load_or_fail() {
  local label="$1"
  local role="$2"
  require_launchctl
  andrea_load_or_kickstart_agent "${label}" || die "${role} LaunchAgent not installed: $(andrea_launchagent_plist_path "${label}")"
}

start_sync() {
  say "Starting Andrea sync LaunchAgent"
  load_or_fail "${ANDREA_SYNC_LABEL}" "andrea_sync"
}

stop_sync() {
  require_launchctl
  say "Stopping Andrea sync LaunchAgent"
  andrea_bootout_agent "${ANDREA_SYNC_LABEL}"
}

start_tunnel() {
  require_launchctl
  warn_if_multiple_tunnels_installed
  local label
  if ! label="$(andrea_tunnel_label 2>/dev/null)"; then
    say "No tunnel LaunchAgent installed; skipping tunnel start"
    return 0
  fi
  say "Starting tunnel LaunchAgent ${label}"
  load_or_fail "${label}" "tunnel"
}

stop_tunnel() {
  require_launchctl
  say "Stopping tunnel LaunchAgents"
  andrea_bootout_agent "${ANDREA_CLOUDFLARED_LABEL}"
  andrea_bootout_agent "${ANDREA_LOCALTUNNEL_LABEL}"
}

start_gateway() {
  require_openclaw
  local status_out
  say "Starting OpenClaw gateway"
  openclaw gateway start >/dev/null 2>&1 || true
  status_out="$(gateway_status_text)"
  if gateway_status_needs_repair "${status_out}"; then
    warn "OpenClaw gateway service was not loaded after start; repairing install"
    repair_gateway_service
    openclaw gateway start >/dev/null
  fi
}

stop_gateway() {
  require_openclaw
  say "Stopping OpenClaw gateway"
  openclaw gateway stop
}

restart_gateway() {
  require_openclaw
  local status_out
  say "Restarting OpenClaw gateway"
  openclaw gateway restart >/dev/null 2>&1 || true
  status_out="$(gateway_status_text)"
  if gateway_status_needs_repair "${status_out}"; then
    warn "OpenClaw gateway service was not loaded after restart; repairing install"
    repair_gateway_service
    openclaw gateway restart >/dev/null
  fi
}

run_bootstrap() {
  say "Running post-login bootstrap"
  bash "${BASE_DIR}/scripts/macos/andrea_post_login_bootstrap.sh"
}

stop_bootstrap_agent() {
  require_launchctl
  say "Stopping post-login bootstrap LaunchAgent"
  andrea_bootout_agent "${ANDREA_BOOTSTRAP_LABEL}"
  andrea_bootout_agent "${ANDREA_OPENCLAW_REFRESH_LABEL}"
}

start_all() {
  start_gateway
  start_sync
  start_tunnel
  run_bootstrap
}

stop_all() {
  stop_bootstrap_agent
  stop_tunnel
  stop_sync
  stop_gateway
}

restart_all() {
  stop_bootstrap_agent
  stop_tunnel
  stop_sync
  restart_gateway
  start_sync
  start_tunnel
  run_bootstrap
}

print_labels() {
  say "Managed LaunchAgent labels"
  andrea_all_launchagent_labels
}

install_launchagents() {
  exec bash "${BASE_DIR}/scripts/macos/install_andrea_launchagents.sh" "$@"
}

case "${command}" in
  status)
    target="${args[0]:-all}"
    case "${target}" in
      all)
        status_all
        ;;
      gateway)
        status_gateway
        ;;
      sync)
        describe_launchagent "${ANDREA_SYNC_LABEL}" "andrea_sync"
        status_sync_health
        status_runtime_truth
        ;;
      tunnel)
        describe_launchagent "${ANDREA_CLOUDFLARED_LABEL}" "cloudflared"
        describe_launchagent "${ANDREA_LOCALTUNNEL_LABEL}" "localtunnel"
        ;;
      bootstrap)
        describe_launchagent "${ANDREA_BOOTSTRAP_LABEL}" "post_login_bootstrap"
        describe_launchagent "${ANDREA_OPENCLAW_REFRESH_LABEL}" "openclaw_gateway_refresh_legacy"
        ;;
      *)
        usage
        exit 2
        ;;
    esac
    ;;
  start)
    target="${args[0]:-all}"
    case "${target}" in
      all)
        start_all
        ;;
      gateway)
        start_gateway
        ;;
      sync)
        start_sync
        ;;
      tunnel)
        start_tunnel
        ;;
      bootstrap)
        run_bootstrap
        ;;
      *)
        usage
        exit 2
        ;;
    esac
    ;;
  stop)
    target="${args[0]:-all}"
    case "${target}" in
      all)
        stop_all
        ;;
      gateway)
        stop_gateway
        ;;
      sync)
        stop_sync
        ;;
      tunnel)
        stop_tunnel
        ;;
      bootstrap)
        stop_bootstrap_agent
        ;;
      *)
        usage
        exit 2
        ;;
    esac
    ;;
  restart)
    target="${args[0]:-all}"
    case "${target}" in
      all)
        restart_all
        ;;
      gateway)
        restart_gateway
        ;;
      sync)
        start_sync
        ;;
      tunnel)
        stop_tunnel
        start_tunnel
        ;;
      bootstrap)
        run_bootstrap
        ;;
      *)
        usage
        exit 2
        ;;
    esac
    ;;
  bootstrap)
    run_bootstrap "$@"
    ;;
  labels)
    print_labels
    ;;
  install-launchagents)
    install_launchagents "${args[@]}"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
