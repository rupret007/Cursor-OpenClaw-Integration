#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
LOG_PREFIX="[andrea_localtunnel]"

say() {
  echo "${LOG_PREFIX} $*"
}

load_env_file() {
  local path="$1"
  [[ -f "$path" ]] || return 0
  set -a
  # shellcheck disable=SC1090
  source "$path"
  set +a
}

cd "$BASE_DIR"
load_env_file "${BASE_DIR}/.env"
load_env_file "${HOME}/andrea-lockstep.env"
if [[ -n "${ANDREA_ENV_FILE:-}" ]]; then
  load_env_file "${ANDREA_ENV_FILE}"
fi

PORT="${ANDREA_SYNC_PORT:-8765}"
SUBDOMAIN="${ANDREA_LOCALTUNNEL_SUBDOMAIN:-}"

update_public_base() {
  local public_base="$1"
  [[ -n "$public_base" ]] || return 0
  export ANDREA_SYNC_PUBLIC_BASE="$public_base"
  python3 "${BASE_DIR}/scripts/dotenv_set_key.py" \
    ANDREA_SYNC_PUBLIC_BASE \
    --env-file "${HOME}/andrea-lockstep.env" \
    --value "$public_base" >/dev/null
  say "Updated ANDREA_SYNC_PUBLIC_BASE=${public_base}"
  launchctl kickstart -k "gui/$(id -u)/com.andrea.andrea-sync" >/dev/null 2>&1 || true
  if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    sleep 2
    python3 "${BASE_DIR}/scripts/andrea_lockstep_telegram_e2e.py" set-webhook >/dev/null \
      && say "Ensured Telegram webhook for ${public_base}" \
      || say "WARN: Telegram webhook refresh failed for ${public_base}"
  fi
}

cmd=(npx --yes localtunnel --port "$PORT")
if [[ -n "$SUBDOMAIN" ]]; then
  cmd+=(--subdomain "$SUBDOMAIN")
fi

"${cmd[@]}" 2>&1 | while IFS= read -r line; do
  echo "$line"
  if [[ "$line" =~ your\ url\ is:\ (https://[^[:space:]]+) ]]; then
    update_public_base "${BASH_REMATCH[1]}"
  fi
done
