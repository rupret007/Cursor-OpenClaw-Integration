#!/usr/bin/env bash
# Automatic model profile remediation for OpenClaw.
# Tries profile order until probe succeeds.
#
# Usage:
#   bash scripts/andrea_model_guard.sh
#   bash scripts/andrea_model_guard.sh --order "balanced,fast,deep"
#   bash scripts/andrea_model_guard.sh --dry-run
#
# Env overrides:
#   OPENCLAW_PROBE_MS=30000
#   ANDREA_MODEL_GUARD_ORDER="balanced,fast,deep"
#   ANDREA_FAST_PRIMARY / ANDREA_FAST_FALLBACKS
#   ANDREA_BALANCED_PRIMARY / ANDREA_BALANCED_FALLBACKS
#   ANDREA_DEEP_PRIMARY / ANDREA_DEEP_FALLBACKS
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROBE_MS="${OPENCLAW_PROBE_MS:-30000}"
ORDER="${ANDREA_MODEL_GUARD_ORDER:-balanced,fast,deep}"
DRY_RUN=0
RESTART_GATEWAY=1

FAST_PRIMARY="${ANDREA_FAST_PRIMARY:-google/gemini-2.5-flash}"
FAST_FALLBACKS="${ANDREA_FAST_FALLBACKS:-openai/gpt-5.3-codex minimax/MiniMax-M2.5}"

BALANCED_PRIMARY="${ANDREA_BALANCED_PRIMARY:-google/gemini-2.5-flash}"
BALANCED_FALLBACKS="${ANDREA_BALANCED_FALLBACKS:-openai/gpt-5.3-codex minimax/MiniMax-M2.5}"

DEEP_PRIMARY="${ANDREA_DEEP_PRIMARY:-openai/gpt-5.3-codex}"
DEEP_FALLBACKS="${ANDREA_DEEP_FALLBACKS:-google/gemini-2.5-flash minimax/MiniMax-M2.5}"

die() { echo "FAIL: $*" >&2; exit 1; }
warn() { echo "WARN: $*" >&2; }
note() { echo "INFO: $*"; }

usage() {
  cat <<'EOF'
Andrea model guard

Options:
  --order "<csv>"         Profile order (default: balanced,fast,deep)
  --probe-timeout-ms N    Probe timeout in milliseconds (default from OPENCLAW_PROBE_MS or 30000)
  --dry-run               Print actions only; do not call openclaw
  --no-restart            Do not restart gateway after profile apply
  -h, --help              Show help
EOF
}

normalize_timeout_ms() {
  local raw="${1:-}"
  [[ -n "$raw" ]] || return 1
  if [[ "$raw" =~ ^[0-9]+$ ]]; then
    echo "$raw"
    return 0
  fi
  if [[ "$raw" =~ ^[0-9]+ms$ ]]; then
    echo "${raw%ms}"
    return 0
  fi
  if [[ "$raw" =~ ^[0-9]+s$ ]]; then
    echo "$(( ${raw%s} * 1000 ))"
    return 0
  fi
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --order) ORDER="${2:-}"; shift 2 ;;
    --probe-timeout-ms) PROBE_MS="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --no-restart) RESTART_GATEWAY=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

PROBE_MS="$(normalize_timeout_ms "$PROBE_MS")" || die "--probe-timeout-ms must be integer milliseconds (or Nms / Ns)"
[[ -n "$ORDER" ]] || die "--order cannot be empty"

if [[ "$DRY_RUN" -ne 1 ]] && ! command -v openclaw >/dev/null 2>&1; then
  die "openclaw not on PATH"
fi

profile_primary() {
  case "$1" in
    fast) echo "$FAST_PRIMARY" ;;
    balanced) echo "$BALANCED_PRIMARY" ;;
    deep) echo "$DEEP_PRIMARY" ;;
    *) return 1 ;;
  esac
}

profile_fallbacks() {
  case "$1" in
    fast) echo "$FAST_FALLBACKS" ;;
    balanced) echo "$BALANCED_FALLBACKS" ;;
    deep) echo "$DEEP_FALLBACKS" ;;
    *) return 1 ;;
  esac
}

apply_profile() {
  local profile="$1"
  local primary
  primary="$(profile_primary "$profile")" || return 1
  local fbs
  fbs="$(profile_fallbacks "$profile")" || return 1

  note "apply profile=${profile} primary=${primary} fallbacks=[${fbs}]"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    return 0
  fi

  openclaw models set "$primary"
  openclaw models fallbacks clear
  for fb in $fbs; do
    openclaw models fallbacks add "$fb"
  done
  if [[ "$RESTART_GATEWAY" -eq 1 ]]; then
    openclaw gateway restart
  fi
}

probe_models() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    note "(dry-run) would probe models with --probe-timeout ${PROBE_MS}"
    return 0
  fi
  openclaw models status --probe --probe-timeout "$PROBE_MS" --probe-concurrency 1
}

main() {
  cd "$BASE_DIR"
  note "model guard start order=${ORDER} probe_ms=${PROBE_MS} dry_run=${DRY_RUN}"

  IFS=',' read -r -a profiles <<<"$ORDER"
  local failures=0
  for raw in "${profiles[@]}"; do
    local p
    p="$(echo "$raw" | tr -d '[:space:]')"
    [[ -n "$p" ]] || continue

    if ! apply_profile "$p"; then
      warn "unknown profile '${p}' (allowed: fast, balanced, deep)"
      failures=$((failures + 1))
      continue
    fi

    if probe_models; then
      note "model guard success on profile=${p}"
      return 0
    fi
    warn "probe failed for profile=${p}; trying next"
    failures=$((failures + 1))
  done

  die "all profiles failed or invalid (failures=${failures})"
}

main
