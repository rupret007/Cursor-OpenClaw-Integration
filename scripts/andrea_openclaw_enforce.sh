#!/usr/bin/env bash
# Enforce OpenClaw baseline for Andrea:
# - sync cursor_handoff skill from repo
# - restart gateway
# - verify required skills are visible/ready
# - run model probe with optional model-guard remediation
#
# Usage:
#   bash scripts/andrea_openclaw_enforce.sh
#   bash scripts/andrea_openclaw_enforce.sh --dry-run
#   bash scripts/andrea_openclaw_enforce.sh --required-skills "cursor_handoff,github,gh-issues,telegram"
#   bash scripts/andrea_openclaw_enforce.sh --heal-skill bluebubbles --heal-skill apple-notes --heal-skill voice-call
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_SKILLS_DIR="${HOME}/.openclaw/workspace/skills"
REPO_SKILL_DIR="${BASE_DIR}/skills/cursor_handoff"
export PATH="${HOME}/.npm-global/bin:${HOME}/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

OPENCLAW_PROBE_MS="${OPENCLAW_PROBE_MS:-30000}"
# Default includes hybrid catalog keys (always listed by OpenClaw; use ANDREA_OPENCLAW_ELIGIBLE_SKILLS for strict readiness).
_DEFAULT_REQUIRED_SKILLS="cursor_handoff,github,gh-issues,telegram,add-minimax-provider,brave-api-search,apple-notes,apple-reminders,things-mac,gog,summarize,session-logs,coding-agent,tmux,peekaboo,voice-call"
REQUIRED_SKILLS="${ANDREA_REQUIRED_OPENCLAW_SKILLS:-${_DEFAULT_REQUIRED_SKILLS}}"
ELIGIBLE_SKILLS="${ANDREA_OPENCLAW_ELIGIBLE_SKILLS:-}"
SYNC_SKILL=1
RESTART_GATEWAY=1
PROBE_MODELS=1
MODEL_GUARD_ON_FAIL=1
DRY_RUN=0
HEAL_SKILLS="${ANDREA_OPENCLAW_HEAL_SKILLS:-}"

die() { echo "FAIL: $*" >&2; exit 1; }
warn() { echo "WARN: $*" >&2; }
note() { echo "INFO: $*"; }

usage() {
  cat <<'EOF'
Andrea OpenClaw enforcer

Options:
  --required-skills "<csv>"    Required skills to validate in `openclaw skills list`
  --probe-timeout-ms N         Probe timeout in milliseconds (default: OPENCLAW_PROBE_MS or 30000)
  --no-sync                    Skip repo -> workspace cursor_handoff sync
  --no-restart                 Skip openclaw gateway restart
  --no-probe                   Skip openclaw model probe
  --no-model-guard             Skip model guard remediation when probe fails
  --heal-skill NAME           Run runtime capability self-heal for a skill before validation
  --dry-run                    Print actions only; no mutations
  -h, --help                   Show help

Environment:
  ANDREA_REQUIRED_OPENCLAW_SKILLS   CSV of skills that must appear in \`openclaw skills list\`
  ANDREA_OPENCLAW_ELIGIBLE_SKILLS   CSV; when non-empty, require each skill \`eligible: true\` (needs jq)
  ANDREA_OPENCLAW_SKILLS_CHECK      Set to 1 to print \`openclaw skills check\` (non-fatal)
  ANDREA_OPENCLAW_HEAL_SKILLS       CSV of skills to auto-heal before validation
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --required-skills) REQUIRED_SKILLS="${2:-}"; shift 2 ;;
    --probe-timeout-ms) OPENCLAW_PROBE_MS="${2:-}"; shift 2 ;;
    --no-sync) SYNC_SKILL=0; shift ;;
    --no-restart) RESTART_GATEWAY=0; shift ;;
    --no-probe) PROBE_MODELS=0; shift ;;
    --no-model-guard) MODEL_GUARD_ON_FAIL=0; shift ;;
    --heal-skill)
      HEAL_SKILLS="${HEAL_SKILLS:+${HEAL_SKILLS},}${2:-}"
      shift 2
      ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ "$OPENCLAW_PROBE_MS" =~ ^[0-9]+$ ]] || die "--probe-timeout-ms must be integer milliseconds"
[[ -n "$REQUIRED_SKILLS" ]] || die "--required-skills cannot be empty"

command -v openclaw >/dev/null 2>&1 || die "openclaw not on PATH"
[[ -d "$BASE_DIR" ]] || die "repo base missing: $BASE_DIR"

sync_skill() {
  [[ -d "$REPO_SKILL_DIR" ]] || die "missing repo skill dir: $REPO_SKILL_DIR"
  note "sync cursor_handoff skill to workspace"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    return 0
  fi
  mkdir -p "$WORKSPACE_SKILLS_DIR"
  cp -R "$REPO_SKILL_DIR" "$WORKSPACE_SKILLS_DIR/"
}

restart_gateway() {
  note "restart openclaw gateway"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    return 0
  fi
  openclaw gateway restart
}

heal_skills() {
  [[ -n "$HEAL_SKILLS" ]] || return 0
  note "run runtime capability self-heal for: $HEAL_SKILLS"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    return 0
  fi
  python3 - "$BASE_DIR" "$HEAL_SKILLS" <<'PY'
import json
import os
import sys
from pathlib import Path

repo_root = Path(sys.argv[1]).resolve()
skills = [part.strip() for part in sys.argv[2].split(",") if part.strip()]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from services.andrea_sync.optimizer import heal_runtime_capability
from services.andrea_sync.store import connect, migrate

db = Path(os.environ.get("ANDREA_SYNC_DB", str(repo_root / "data/andrea_sync.db"))).expanduser()
conn = connect(db)
try:
    migrate(conn)
    results = [heal_runtime_capability(conn, skill_key=skill, actor="script") for skill in skills]
    payload = {"ok": all(result.get("ok") for result in results), "results": results}
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if not payload["ok"]:
        raise SystemExit(1)
finally:
    conn.close()
PY
}

check_required_skills() {
  note "validate required skills list"
  local out
  out="$(openclaw skills list)"
  local missing=0
  IFS=',' read -r -a skills <<<"$REQUIRED_SKILLS"
  for raw in "${skills[@]}"; do
    local s
    s="$(echo "$raw" | tr -d '[:space:]')"
    [[ -n "$s" ]] || continue
    if echo "$out" | grep -q "$s"; then
      note "required skill present: $s"
    else
      warn "required skill not found in skills list output: $s"
      missing=$((missing + 1))
    fi
  done
  [[ "$missing" -eq 0 ]] || die "missing required skills: ${missing}"
}

check_eligible_skills() {
  [[ -n "$ELIGIBLE_SKILLS" ]] || return 0
  note "validate eligible skills (ANDREA_OPENCLAW_ELIGIBLE_SKILLS)"
  command -v jq >/dev/null 2>&1 || die "jq not on PATH (required for eligible skill checks)"
  local failed=0
  IFS=',' read -r -a elig <<<"$ELIGIBLE_SKILLS"
  for raw in "${elig[@]}"; do
    local s
    s="$(echo "$raw" | tr -d '[:space:]')"
    [[ -n "$s" ]] || continue
    local json
    json="$(openclaw skills info "$s" --json 2>/dev/null)" || {
      warn "skills info failed for: $s"
      failed=$((failed + 1))
      continue
    }
    if echo "$json" | jq -e '.eligible == true' >/dev/null 2>&1; then
      note "eligible skill: $s"
    else
      warn "skill not eligible: $s ($(echo "$json" | jq -c '.missing' 2>/dev/null || echo "{}"))"
      failed=$((failed + 1))
    fi
  done
  [[ "$failed" -eq 0 ]] || die "eligible skill checks failed: ${failed}"
}

check_acp_router_runtime() {
  note "validate ACP router runtime"
  local out
  out="$(openclaw skills list)"
  if printf '%s\n' "$out" | grep -E "✓ ready.*acp-router" >/dev/null 2>&1; then
    if command -v acpx >/dev/null 2>&1; then
      note "acpx available for acp-router"
    else
      die "acp-router is ready but acpx is missing from PATH; install with: npm install -g acpx"
    fi
  else
    note "acp-router not ready on this host; skipping acpx requirement"
  fi
}

maybe_skills_check() {
  if [[ "${ANDREA_OPENCLAW_SKILLS_CHECK:-0}" != "1" ]]; then
    return 0
  fi
  note "openclaw skills check (informational)"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    return 0
  fi
  openclaw skills check || true
}

probe_models() {
  note "probe models with timeout=${OPENCLAW_PROBE_MS}ms"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    return 0
  fi
  if openclaw models status --probe --probe-timeout "$OPENCLAW_PROBE_MS" --probe-concurrency 1; then
    note "model probe passed"
    return 0
  fi
  warn "model probe failed"
  if [[ "$MODEL_GUARD_ON_FAIL" -eq 1 ]]; then
    note "run model guard remediation"
    bash "${BASE_DIR}/scripts/andrea_model_guard.sh" --probe-timeout-ms "$OPENCLAW_PROBE_MS"
  else
    return 1
  fi
}

main() {
  cd "$BASE_DIR"
  note "openclaw enforcer start dry_run=${DRY_RUN}"
  if [[ "$SYNC_SKILL" -eq 1 ]]; then
    sync_skill
  fi
  heal_skills
  if [[ "$RESTART_GATEWAY" -eq 1 ]]; then
    restart_gateway
  fi
  check_required_skills
  maybe_skills_check
  check_eligible_skills
  check_acp_router_runtime
  if [[ "$PROBE_MODELS" -eq 1 ]]; then
    probe_models
  fi
  note "openclaw enforcer complete"
}

main
