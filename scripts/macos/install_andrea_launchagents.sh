#!/usr/bin/env bash
# Install LaunchAgents for Andrea lockstep (and optional cloudflared / OpenClaw refresh).
#
# Usage:
#   REPO_ROOT=/path/to/Cursor-OpenClaw-Integration bash scripts/macos/install_andrea_launchagents.sh
#   CLOUDFLARED_TUNNEL_TOKEN=... bash scripts/macos/install_andrea_launchagents.sh --with-cloudflared
#   bash scripts/macos/install_andrea_launchagents.sh --with-openclaw-refresh
#
set -euo pipefail
BASE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
REPO_ROOT="${REPO_ROOT:-$BASE_DIR}"
HOME_DIR="${HOME}"
PY3="${PYTHON3:-$(command -v python3)}"
CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-$(command -v cloudflared || true)}"
AGENT_DIR="${HOME_DIR}/Library/LaunchAgents"
LOG_DIR="${HOME_DIR}/Library/Logs/andrea"
WITH_CF=0
WITH_OC=0
for a in "$@"; do
  case "$a" in
    --with-cloudflared) WITH_CF=1 ;;
    --with-openclaw-refresh) WITH_OC=1 ;;
  esac
done

mkdir -p "$AGENT_DIR" "$LOG_DIR"

render() {
  local src="$1" dest="$2"
  sed \
    -e "s|__REPO_ROOT__|${REPO_ROOT//\\/\\\\}|g" \
    -e "s|__HOME__|${HOME_DIR//\\/\\\\}|g" \
    -e "s|__PYTHON3__|${PY3//\\/\\\\}|g" \
    -e "s|__CLOUDFLARED_BIN__|${CLOUDFLARED_BIN//\\/\\\\}|g" \
    -e "s|__CLOUDFLARED_TUNNEL_TOKEN__|${CLOUDFLARED_TUNNEL_TOKEN:-REPLACE_ME}|g" \
    "$src" > "$dest"
}

render "${BASE_DIR}/scripts/macos/com.andrea.andrea-sync.plist.template" \
  "${AGENT_DIR}/com.andrea.andrea-sync.plist"

echo "Installed ${AGENT_DIR}/com.andrea.andrea-sync.plist"
echo "The sync LaunchAgent sources repo .env first, then ~/andrea-lockstep.env for overrides."
echo "Put secrets/runtime overrides in ~/andrea-lockstep.env (export TELEGRAM_BOT_TOKEN=... etc.) then:"
echo "  launchctl bootstrap gui/\$(id -u) ${AGENT_DIR}/com.andrea.andrea-sync.plist"
echo "  # if updating an existing agent first run:"
echo "  launchctl bootout gui/\$(id -u) ${AGENT_DIR}/com.andrea.andrea-sync.plist || true"

if [[ "$WITH_CF" -eq 1 ]]; then
  if [[ -z "${CLOUDFLARED_TUNNEL_TOKEN:-}" ]]; then
    echo "error: set CLOUDFLARED_TUNNEL_TOKEN for named tunnel" >&2
    exit 1
  fi
  if [[ -z "${CLOUDFLARED_BIN}" ]]; then
    echo "error: cloudflared not found on PATH (set CLOUDFLARED_BIN explicitly if needed)" >&2
    exit 1
  fi
  render "${BASE_DIR}/scripts/macos/com.andrea.andrea-cloudflared.plist.template" \
    "${AGENT_DIR}/com.andrea.andrea-cloudflared.plist"
  echo "Installed cloudflared agent using ${CLOUDFLARED_BIN}."
fi

if [[ "$WITH_OC" -eq 1 ]]; then
  render "${BASE_DIR}/scripts/macos/com.andrea.openclaw-gateway-refresh.plist.template" \
    "${AGENT_DIR}/com.andrea.openclaw-gateway-refresh.plist"
  echo "Installed one-shot openclaw gateway refresh at login (not a full gateway daemon)."
fi
