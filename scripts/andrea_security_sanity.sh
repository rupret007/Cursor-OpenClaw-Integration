#!/usr/bin/env bash
# Repo + host hygiene checks for secrets (no secret values printed).
# Usage: bash scripts/andrea_security_sanity.sh
#        STRICT=1 bash scripts/andrea_security_sanity.sh   # exit 1 on warnings too
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STRICT="${STRICT:-0}"
WARNINGS=0

fail() { echo "FAIL: $*" >&2; exit 1; }
warn() { echo "WARN: $*" >&2; WARNINGS=$((WARNINGS + 1)); }
pass() { echo "OK  $*"; }

cd "$BASE_DIR"

echo "======== Andrea security sanity ========"

git rev-parse --git-dir >/dev/null 2>&1 || fail "not a git repository (expected repo root)"

# Tracked env files must never hold real secrets in git
for f in .env .env.local; do
  if [[ -n "$(git ls-files "$f" 2>/dev/null || true)" ]]; then
    fail "tracked file must not be committed: $f"
  fi
done
pass "no tracked root .env / .env.local"

# OpenClaw user config must not land in this repo
if git ls-files | grep -qE '(^|/)openclaw\.json$'; then
  fail "openclaw.json must not be committed to this repository"
fi
pass "no openclaw.json in git index"

# High-signal secret patterns in tracked source (not docs/markdown)
# Adjust if you add fixtures; keep tests free of real-looking keys.
_hits="$(git grep -nE 'sk-proj-[A-Za-z0-9_-]{20,}' -- '*.py' '*.sh' '*.json' '*.toml' '*.yaml' '*.yml' 2>/dev/null | grep -vE '\.example|test_|tests/|placeholder|REDACT|xxxx' || true)"
if [[ -n "${_hits}" ]]; then
  echo "${_hits}" >&2
  fail "possible OpenAI project key in tracked code/config (use env / SecretRef)"
fi
pass "no sk-proj-* pattern in tracked code-like files"

_hits="$(git grep -nE 'ghp_[A-Za-z0-9]{20,}' -- '*.py' '*.sh' '*.json' 2>/dev/null | grep -vE '\.example|test_|tests/|placeholder|xxxx' || true)"
if [[ -n "${_hits}" ]]; then
  echo "${_hits}" >&2
  fail "possible GitHub PAT in tracked code/config"
fi
pass "no ghp_* PAT pattern in tracked code-like files"

_hits="$(git grep -nE 'AIzaSy[A-Za-z0-9_-]{20,}' -- '*.py' '*.sh' '*.json' 2>/dev/null | grep -vE '\.example|test_|tests/|placeholder|xxxx' || true)"
if [[ -n "${_hits}" ]]; then
  echo "${_hits}" >&2
  fail "possible Google API key in tracked code/config"
fi
pass "no AIzaSy* pattern in tracked code-like files"

if git grep -nF '-----BEGIN' -- '*.py' '*.sh' '*.pem' '*.key' 2>/dev/null; then
  fail "possible private key material tracked in repo"
fi
pass "no PEM private key headers in tracked files"

# Optional: OpenClaw backup noise on developer machine (warn only)
OPENCLAW_HOME="${HOME}/.openclaw"
if [[ -d "$OPENCLAW_HOME" ]]; then
  bak_count="$(find "$OPENCLAW_HOME" -maxdepth 2 -name '*.bak' -type f 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "${bak_count}" != "0" ]]; then
    # Host-local noise: must not fail release gates on developer machines (STRICT=1).
    msg="found ${bak_count} *.bak under ${OPENCLAW_HOME} — may contain secrets; prune after verifying active config"
    if [[ "$STRICT" == "1" ]]; then
      echo "NOTE: ${msg}" >&2
    else
      warn "${msg}"
    fi
  else
    pass "no *.bak files in ~/.openclaw (depth<=2)"
  fi
else
  pass "skip ~/.openclaw backup scan (directory missing)"
fi

echo "======== Andrea security sanity complete ========"
if [[ "$STRICT" == "1" ]] && [[ "$WARNINGS" -gt 0 ]]; then
  exit 1
fi
exit 0
