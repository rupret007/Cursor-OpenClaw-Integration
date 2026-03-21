#!/usr/bin/env bash
# Emergency control for Andrea lockstep + optional LaunchAgent unload (hard stop).
#
# Usage:
#   ANDREA_SYNC_INTERNAL_TOKEN=... ANDREA_SYNC_URL=http://127.0.0.1:8765 \
#     bash scripts/andrea_kill_switch.sh engage "reason text"
#   bash scripts/andrea_kill_switch.sh release
#   bash scripts/andrea_kill_switch.sh status
#   ANDREA_LAUNCHD_LABELS=com.andrea.andrea-sync,com.andrea.andrea-cloudflared \
#     bash scripts/andrea_kill_switch.sh hard-stop "reason"
#
set -euo pipefail
BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BASE_URL="${ANDREA_SYNC_URL:-http://127.0.0.1:8765}"
BASE_URL="${BASE_URL%/}"
TOKEN="${ANDREA_SYNC_INTERNAL_TOKEN:-}"

cmd="${1:-status}"
shift || true

if [[ "$cmd" != "status" && -z "$TOKEN" ]]; then
  echo "error: ANDREA_SYNC_INTERNAL_TOKEN required for $cmd" >&2
  exit 1
fi

auth_json() {
  curl -sS \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    "$@"
}

case "$cmd" in
  engage)
    reason="${*:-manual}"
    payload="$(python3 -c "import json,sys; print(json.dumps({'command_type':'KillSwitchEngage','channel':'internal','payload':{'reason':sys.argv[1]}}))" "$reason")"
    auth_json -d "$payload" "$BASE_URL/v1/commands" | python3 -m json.tool
    ;;
  release)
    auth_json -d '{"command_type":"KillSwitchRelease","channel":"internal","payload":{}}' \
      "$BASE_URL/v1/commands" | python3 -m json.tool
    ;;
  status)
    curl -sS "$BASE_URL/v1/status" | python3 -m json.tool
    ;;
  hard-stop)
    reason="${*:-hard-stop}"
    "$BASE_DIR/scripts/andrea_kill_switch.sh" engage "$reason"
    labels="${ANDREA_LAUNCHD_LABELS:-com.andrea.andrea-sync,com.andrea.andrea-cloudflared}"
    uid="$(id -u)"
    IFS=',' read -r -a arr <<< "$labels"
    for lab in "${arr[@]}"; do
      [[ -n "$lab" ]] || continue
      echo "launchctl bootout gui/$uid/$lab (ignore errors if not loaded)" >&2
      launchctl bootout "gui/$uid/$lab" 2>/dev/null || true
    done
    echo "Hard-stop: kill switch engaged + launchctl bootout attempted. OpenClaw may need manual stop." >&2
    ;;
  *)
    echo "usage: $0 engage|release|status|hard-stop [reason...]" >&2
    exit 2
    ;;
esac
