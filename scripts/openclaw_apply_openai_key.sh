#!/usr/bin/env bash
# Apply OPENAI_API_KEY from repo .env into OpenClaw using the official non-interactive
# onboarding path (OpenAI *platform* API key — not ChatGPT Plus, not Codex OAuth).
#
# Usage (from repo root):
#   bash scripts/openclaw_apply_openai_key.sh
#   bash scripts/openclaw_apply_openai_key.sh --dry-run    # only validate key shape
#
# Requires: openclaw on PATH; ./.env with OPENAI_API_KEY set (use dotenv_set_key.py).
# After success:  openclaw gateway restart
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE_DIR"

DRY_RUN=false
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=true ;;
  esac
done

if ! command -v openclaw >/dev/null 2>&1; then
  echo "ERROR: openclaw not on PATH." >&2
  exit 1
fi

KEY="$(
  REPO_ROOT="$BASE_DIR" python3 <<'PY'
import os
import sys
from pathlib import Path

ROOT = Path(os.environ["REPO_ROOT"]).resolve()
sys.path.insert(0, str(ROOT / "scripts"))
from env_loader import parse_env_line  # noqa: E402

path = ROOT / ".env"
if not path.is_file():
    print("missing .env", file=sys.stderr)
    sys.exit(1)
for line in path.read_text(encoding="utf-8").splitlines():
    p = parse_env_line(line)
    if p and p[0] == "OPENAI_API_KEY":
        v = (p[1] or "").strip()
        if v:
            print(v, end="")
            sys.exit(0)
print("OPENAI_API_KEY empty in .env", file=sys.stderr)
sys.exit(1)
PY
)"

# Basic shape check (OpenAI platform keys are typically sk-...; project keys often sk-proj-...)
case "$KEY" in
  sk-*) ;;
  *)
    echo "ERROR: OPENAI_API_KEY in .env does not look like a platform API key (expected sk-...)." >&2
    echo "Get a key from https://platform.openai.com/api-keys (billing-enabled project), not ChatGPT Plus." >&2
    exit 1
    ;;
esac

if $DRY_RUN; then
  echo "OK: OPENAI_API_KEY present in .env and looks like a platform key (not printed)."
  exit 0
fi

export OPENAI_API_KEY="$KEY"
echo "Running: openclaw onboard --openai-api-key <from .env>  (key not echoed)"
# Per https://docs.openclaw.ai/providers/openai
openclaw onboard --openai-api-key "$OPENAI_API_KEY"

echo ""
echo "Next:  openclaw gateway restart"
echo "Check: openclaw models status --probe"
