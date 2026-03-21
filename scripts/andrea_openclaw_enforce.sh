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
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_SKILLS_DIR="${HOME}/.openclaw/workspace/skills"
REPO_SKILL_DIR="${BASE_DIR}/skills/cursor_handoff"

OPENCLAW_PROBE_MS="${OPENCLAW_PROBE_MS:-30000}"
REQUIRED_SKILLS="${ANDREA_REQUIRED_OPENCLAW_SKILLS:-cursor_handoff,github,gh-issues,telegram,add-minimax-provider,brave-api-search}"
SYNC_SKILL=1
RESTART_GATEWAY=1
PROBE_MODELS=1
MODEL_GUARD_ON_FAIL=1
DRY_RUN=0

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
  --dry-run                    Print actions only; no mutations
  -h, --help                   Show help
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
  if [[ "$RESTART_GATEWAY" -eq 1 ]]; then
    restart_gateway
  fi
  check_required_skills
  if [[ "$PROBE_MODELS" -eq 1 ]]; then
    probe_models
  fi
  note "openclaw enforcer complete"
}

main
