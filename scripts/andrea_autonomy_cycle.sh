#!/usr/bin/env bash
# Closed-loop local autonomy pass: health, regression-backed optimization,
# incident-driven repair, optional safe auto-heal branch prep, and an optional
# proactive reminder sweep.
#
# Usage:
#   export ANDREA_SYNC_URL='http://127.0.0.1:8765'   # optional but recommended
#   export ANDREA_SYNC_INTERNAL_TOKEN='...'          # required for proactive sweep
#   bash scripts/andrea_autonomy_cycle.sh
#
# Optional environment:
#   ANDREA_AUTONOMY_LIMIT=60
#   ANDREA_AUTONOMY_ANALYSIS_MODE=heuristic|openclaw_prompt|gemini_background
#   ANDREA_AUTONOMY_REGRESSION_COMMAND="python3 -m unittest discover -p 'test_*.py'"
#   ANDREA_AUTONOMY_REGRESSION_CWD="/path/to/repo/tests"
#   ANDREA_AUTONOMY_REQUIRE_SKILLS="cursor_handoff"
#   ANDREA_AUTONOMY_AUTO_APPLY_READY=1
#   ANDREA_AUTONOMY_AUTO_APPLY_LIMIT=1
#   ANDREA_AUTONOMY_BACKGROUND_IDLE_SECONDS=120
#   ANDREA_AUTONOMY_INCIDENT_REPAIR=1
#   ANDREA_AUTONOMY_INCIDENT_CURSOR_EXECUTE=0
#   ANDREA_AUTONOMY_ALLOW_DIRTY=0
#   SKIP_HEALTH=0
#   SKIP_PROACTIVE_SWEEP=0
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE_DIR"

say() { echo "[andrea_autonomy_cycle] $*"; }
warn() { echo "[andrea_autonomy_cycle] WARN: $*" >&2; }
die() { echo "[andrea_autonomy_cycle] FAIL: $*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 not on PATH"

DB_PATH="${ANDREA_SYNC_DB:-$BASE_DIR/data/andrea_sync.db}"
SYNC_URL="${ANDREA_SYNC_URL:-http://127.0.0.1:8765}"
SYNC_URL="${SYNC_URL%/}"
REPO_PATH="${ANDREA_SYNC_CURSOR_REPO:-${ANDREA_CURSOR_REPO:-$BASE_DIR}}"
REGRESSION_COMMAND="${ANDREA_AUTONOMY_REGRESSION_COMMAND:-python3 -m unittest discover -p 'test_*.py'}"
REGRESSION_CWD="${ANDREA_AUTONOMY_REGRESSION_CWD:-$BASE_DIR/tests}"
LIMIT="${ANDREA_AUTONOMY_LIMIT:-60}"
ANALYSIS_MODE="${ANDREA_AUTONOMY_ANALYSIS_MODE:-heuristic}"
REQUIRE_SKILLS="${ANDREA_AUTONOMY_REQUIRE_SKILLS:-cursor_handoff}"
AUTO_APPLY_READY="${ANDREA_AUTONOMY_AUTO_APPLY_READY:-1}"
AUTO_APPLY_LIMIT="${ANDREA_AUTONOMY_AUTO_APPLY_LIMIT:-1}"
BACKGROUND_IDLE_SECONDS="${ANDREA_AUTONOMY_BACKGROUND_IDLE_SECONDS:-120}"
INCIDENT_REPAIR="${ANDREA_AUTONOMY_INCIDENT_REPAIR:-1}"
INCIDENT_CURSOR_EXECUTE="${ANDREA_AUTONOMY_INCIDENT_CURSOR_EXECUTE:-0}"
ALLOW_DIRTY="${ANDREA_AUTONOMY_ALLOW_DIRTY:-0}"

if [[ "${AUTO_APPLY_READY}" == "1" ]] && [[ "${ALLOW_DIRTY}" != "1" ]]; then
  [[ -z "$(git status --porcelain)" ]] || die "working tree must be clean before auto-heal branch prep (set ANDREA_AUTONOMY_ALLOW_DIRTY=1 to override)"
fi

if [[ "${SKIP_HEALTH:-0}" != "1" ]]; then
  say "preflight andrea_sync health"
  ANDREA_SYNC_URL="${SYNC_URL}" python3 scripts/andrea_sync_health.py || die "andrea_sync preflight health failed"
else
  say "skip health (SKIP_HEALTH=1)"
fi

cmd=(
  python3 scripts/andrea_optimize.py
  --db "$DB_PATH"
  --repo "$REPO_PATH"
  --limit "$LIMIT"
  --analysis-mode "$ANALYSIS_MODE"
  --background-idle-seconds "$BACKGROUND_IDLE_SECONDS"
  --regression-command "$REGRESSION_COMMAND"
  --regression-cwd "$REGRESSION_CWD"
)

IFS=',' read -r -a required_skill_array <<<"$REQUIRE_SKILLS"
for raw_skill in "${required_skill_array[@]}"; do
  skill="$(printf '%s' "$raw_skill" | tr -d '[:space:]')"
  [[ -n "$skill" ]] || continue
  cmd+=(--require-skill "$skill")
done

if [[ "${AUTO_APPLY_READY}" == "1" ]]; then
  cmd+=(--auto-apply-ready --auto-apply-limit "$AUTO_APPLY_LIMIT")
else
  say "auto-apply disabled (ANDREA_AUTONOMY_AUTO_APPLY_READY=0)"
fi

say "run regression-backed optimization cycle"
"${cmd[@]}" || die "optimization cycle failed"

if [[ "${INCIDENT_REPAIR}" == "1" ]]; then
  say "run incident-driven repair cycle"
  repair_cmd=(
    python3 scripts/andrea_repair_cycle.py
    --db "$DB_PATH"
    --repo "$REPO_PATH"
  )
  if [[ "${INCIDENT_CURSOR_EXECUTE}" == "1" ]]; then
    repair_cmd+=(--cursor-execute)
  fi
  "${repair_cmd[@]}" || die "incident-driven repair cycle failed"
else
  say "skip incident-driven repair cycle (ANDREA_AUTONOMY_INCIDENT_REPAIR=0)"
fi

if [[ "${SKIP_PROACTIVE_SWEEP:-0}" != "1" ]]; then
  if [[ -n "${ANDREA_SYNC_INTERNAL_TOKEN:-}" ]]; then
    command -v curl >/dev/null 2>&1 || die "curl not on PATH"
    say "run proactive reminder sweep"
    tmp_json="$(mktemp "${TMPDIR:-/tmp}/andrea_autonomy_sweep.XXXXXX")"
    http_code="$(curl -sS -o "$tmp_json" -w "%{http_code}" -m 30 -X POST \
      "${SYNC_URL}/v1/commands" \
      -H "Authorization: Bearer ${ANDREA_SYNC_INTERNAL_TOKEN}" \
      -H "Content-Type: application/json" \
      -d '{"command_type":"RunProactiveSweep","channel":"internal","payload":{}}')" || {
        rm -f "$tmp_json"
        die "proactive sweep request failed"
      }
    [[ "$http_code" == "200" ]] || {
      body="$(python3 - "$tmp_json" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
print(path.read_text(encoding="utf-8", errors="replace")[:400])
PY
)"
      rm -f "$tmp_json"
      die "expected HTTP 200 from proactive sweep, got ${http_code}: ${body}"
    }
    python3 - "$tmp_json" <<'PY' || {
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
assert payload.get("ok") is True, payload
PY
      rm -f "$tmp_json"
      die "proactive sweep response was not ok"
    }
    rm -f "$tmp_json"
  else
    warn "skip proactive sweep (export ANDREA_SYNC_INTERNAL_TOKEN to enable)"
  fi
else
  say "skip proactive sweep (SKIP_PROACTIVE_SWEEP=1)"
fi

if [[ "${SKIP_HEALTH:-0}" != "1" ]]; then
  say "post-run andrea_sync health"
  ANDREA_SYNC_URL="${SYNC_URL}" python3 scripts/andrea_sync_health.py || die "andrea_sync post-run health failed"
fi

say "autonomy cycle completed OK"
