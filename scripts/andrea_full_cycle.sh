#!/usr/bin/env bash
# One-shot operator cycle: pull, lockstep health/status, capability publish, policy probe,
# optional OpenClaw gateway restart, communication smoke, kill-switch drill, optional Telegram checks.
#
# From repo root (or any path — script cds to repo):
#   export ANDREA_SYNC_INTERNAL_TOKEN='...'
#   export ANDREA_SYNC_URL='http://127.0.0.1:8765'   # optional
#   bash scripts/andrea_full_cycle.sh
#
# Skips (all optional):
#   SKIP_GIT=1                    skip git pull
#   SKIP_GATEWAY_RESTART=1        skip openclaw gateway restart
#   SKIP_COMM_SMOKE=1             skip andrea_communication_smoke.sh
#   SKIP_KILL_DRILL=1             skip kill-switch engage/503/200 drill
#   SKIP_TELEGRAM_E2E=1           skip Telegram webhook-info / wait
#
# Telegram extras:
#   TELEGRAM_BOT_TOKEN + ANDREA_SYNC_TELEGRAM_SECRET → webhook-info
#   ANDREA_FULL_CYCLE_WAIT_TELEGRAM=1 → wait-telegram-task (45s max)
#
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ANDREA_REPO_ROOT="${BASE_DIR}"
# shellcheck disable=SC1091
source "${BASE_DIR}/scripts/macos/andrea_launchagent_lib.sh"
cd "$BASE_DIR"
andrea_load_runtime_env

say() { echo "[andrea_full_cycle] $*"; }
warn() { echo "[andrea_full_cycle] WARN: $*" >&2; }
die() { echo "[andrea_full_cycle] FAIL: $*" >&2; exit 1; }

[[ -f scripts/andrea_sync_publish_capabilities.py ]] || die "missing scripts/andrea_sync_publish_capabilities.py"
[[ -f scripts/andrea_kill_switch.sh ]] || die "missing scripts/andrea_kill_switch.sh"
[[ -f scripts/andrea_communication_smoke.sh ]] || die "missing scripts/andrea_communication_smoke.sh"
[[ -f scripts/andrea_lockstep_telegram_e2e.py ]] || die "missing scripts/andrea_lockstep_telegram_e2e.py"
[[ -f scripts/andrea_services.sh ]] || die "missing scripts/andrea_services.sh"

command -v curl >/dev/null 2>&1 || die "curl not on PATH"
command -v python3 >/dev/null 2>&1 || die "python3 not on PATH"

[[ -n "${ANDREA_SYNC_INTERNAL_TOKEN:-}" ]] || die "export ANDREA_SYNC_INTERNAL_TOKEN (Bearer for admin commands)"

export ANDREA_SYNC_URL="${ANDREA_SYNC_URL:-http://127.0.0.1:8765}"
export ANDREA_SYNC_URL="${ANDREA_SYNC_URL%/}"

say "andrea_services.sh status all"
bash scripts/andrea_services.sh status all || die "andrea_services status failed"

if [[ "${SKIP_GIT:-0}" != "1" ]]; then
  current_branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  [[ "$current_branch" == "main" ]] || die "current branch is '${current_branch:-unknown}'; switch to main or set SKIP_GIT=1"
  [[ -z "$(git status --porcelain)" ]] || die "working tree is not clean; commit/stash changes or set SKIP_GIT=1"
  say "git pull --ff-only origin main"
  git pull --ff-only origin main
else
  say "skip git (SKIP_GIT=1)"
fi

say "GET /v1/health"
h_out="$(curl -sS -m 20 "${ANDREA_SYNC_URL}/v1/health")" || die "curl health failed"
echo "$h_out" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('ok') is True, d" || die "/v1/health not ok"

say "GET /v1/status (validate JSON)"
st_out="$(curl -sS -m 20 "${ANDREA_SYNC_URL}/v1/status")" || die "curl status failed"
echo "$st_out" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('ok') is True, d" || die "/v1/status not ok"

say "GET /v1/runtime-snapshot (validate JSON)"
rt_out="$(curl -sS -m 20 "${ANDREA_SYNC_URL}/v1/runtime-snapshot")" || die "curl runtime-snapshot failed"
echo "$rt_out" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('ok') is True, d; assert isinstance((d.get('runtime') or {}).get('webhook'), dict), d" || die "/v1/runtime-snapshot not ok"

say "PublishCapabilitySnapshot via andrea_sync_publish_capabilities.py"
python3 scripts/andrea_sync_publish_capabilities.py || die "publish capabilities failed"

say "GET /v1/policy/skill-absence?skill=telegram"
# Quote URL so zsh never glob-expands '?'
pol_out="$(curl -sS -m 20 "${ANDREA_SYNC_URL}/v1/policy/skill-absence?skill=telegram")" || die "policy curl failed"
echo "$pol_out" | python3 -c "import json,sys; json.load(sys.stdin)" || die "policy response not JSON"
echo "$pol_out" | python3 -m json.tool

if [[ "${SKIP_GATEWAY_RESTART:-0}" != "1" ]] && command -v openclaw >/dev/null 2>&1; then
  say "andrea_services.sh restart gateway"
  bash scripts/andrea_services.sh restart gateway || die "gateway restart failed"
  say "andrea_services.sh bootstrap"
  bash scripts/andrea_services.sh bootstrap || die "bootstrap failed after gateway restart"
elif [[ "${SKIP_GATEWAY_RESTART:-0}" == "1" ]]; then
  say "skip gateway restart (SKIP_GATEWAY_RESTART=1)"
else
  warn "openclaw not on PATH; skip gateway restart"
fi

say "andrea_services.sh status sync"
bash scripts/andrea_services.sh status sync || die "process-authoritative runtime truth check failed"

if [[ "${SKIP_COMM_SMOKE:-0}" != "1" ]]; then
  say "andrea_communication_smoke.sh"
  bash scripts/andrea_communication_smoke.sh || die "communication smoke failed"
else
  say "skip communication smoke (SKIP_COMM_SMOKE=1)"
fi

if [[ "${SKIP_KILL_DRILL:-0}" != "1" ]]; then
  say "kill-switch engage"
  bash scripts/andrea_kill_switch.sh engage "andrea_full_cycle_drill" || die "kill_switch engage failed"

  say "expect HTTP 503 on CreateTask while kill switch engaged"
  rid="fc_${RANDOM}_${RANDOM}"
  ks_code="$(curl -sS -o "${TMPDIR:-/tmp}/andrea_fc_ks.$$" -w "%{http_code}" -m 20 -X POST \
    "${ANDREA_SYNC_URL}/v1/commands" \
    -H 'Content-Type: application/json' \
    -d "{\"command_type\":\"CreateTask\",\"channel\":\"cli\",\"external_id\":\"${rid}\",\"payload\":{\"summary\":\"kill_drill\"}}")" || true
  [[ "$ks_code" == "503" ]] || die "expected HTTP 503 during kill switch, got ${ks_code} body=$(head -c 300 "${TMPDIR:-/tmp}/andrea_fc_ks.$$" 2>/dev/null || true)"
  rm -f "${TMPDIR:-/tmp}/andrea_fc_ks.$$"

  say "kill-switch release"
  bash scripts/andrea_kill_switch.sh release || die "kill_switch release failed"

  say "expect HTTP 200 on CreateTask after release"
  rid2="fc_${RANDOM}_${RANDOM}"
  ok_code="$(curl -sS -o "${TMPDIR:-/tmp}/andrea_fc_ok.$$" -w "%{http_code}" -m 20 -X POST \
    "${ANDREA_SYNC_URL}/v1/commands" \
    -H 'Content-Type: application/json' \
    -d "{\"command_type\":\"CreateTask\",\"channel\":\"cli\",\"external_id\":\"${rid2}\",\"payload\":{\"summary\":\"after_release\"}}")" || true
  [[ "$ok_code" == "200" ]] || die "expected HTTP 200 after release, got ${ok_code} body=$(head -c 300 "${TMPDIR:-/tmp}/andrea_fc_ok.$$" 2>/dev/null || true)"
  rm -f "${TMPDIR:-/tmp}/andrea_fc_ok.$$"
else
  say "skip kill-switch drill (SKIP_KILL_DRILL=1)"
fi

if [[ "${SKIP_TELEGRAM_E2E:-0}" != "1" ]] && [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  if [[ -n "${ANDREA_SYNC_TELEGRAM_SECRET:-}" ]]; then
    if [[ -n "${ANDREA_SYNC_PUBLIC_BASE:-}" ]]; then
      say "Telegram webhook-info (require registered webhook that matches ANDREA_SYNC_PUBLIC_BASE)"
      python3 scripts/andrea_lockstep_telegram_e2e.py webhook-info --require-match --attempts 3 --retry-delay-sec 2 \
        || die "Telegram webhook is unset or drifted from ANDREA_SYNC_PUBLIC_BASE"
    else
      warn "ANDREA_SYNC_PUBLIC_BASE unset; webhook-info is informational only"
      python3 scripts/andrea_lockstep_telegram_e2e.py webhook-info || warn "webhook-info non-zero (check bot token / network)"
    fi
  else
    warn "skip webhook-info (set ANDREA_SYNC_TELEGRAM_SECRET for andrea_lockstep_telegram_e2e check-env)"
  fi
  if [[ "${ANDREA_FULL_CYCLE_WAIT_TELEGRAM:-0}" == "1" ]]; then
    say "wait-telegram-task (45s max) — send a message to your bot if this hangs"
    python3 scripts/andrea_lockstep_telegram_e2e.py wait-telegram-task --timeout-sec 45 --interval-sec 2 || warn "no telegram task within timeout (optional)"
  fi
else
  say "skip Telegram e2e (SKIP_TELEGRAM_E2E=1 or TELEGRAM_BOT_TOKEN unset)"
fi

say "full cycle completed OK"
