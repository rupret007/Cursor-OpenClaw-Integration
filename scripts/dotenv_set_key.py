#!/usr/bin/env python3
"""
Merge one or more keys into repo-root .env (and optionally the cursor_handoff skill .env)
using the same JSON-string line format as setup_admin.sh. Does not wipe other keys.

Usage:
  python3 scripts/dotenv_set_key.py GH_TOKEN              # hidden prompt if TTY
  python3 scripts/dotenv_set_key.py GH_TOKEN --value 'ghp_...'
  printf '%s' "$GH_TOKEN" | python3 scripts/dotenv_set_key.py GH_TOKEN
  python3 scripts/dotenv_set_key.py GH_TOKEN --skill      # also ~/.openclaw/.../cursor_handoff/.env
  python3 scripts/dotenv_set_key.py OPENAI_API_KEY --enable-openai --skill   # key + OPENAI_API_ENABLED=1

For GH_TOKEN or GITHUB_TOKEN, the other name is set to the same value unless --no-github-alias.
"""
from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from pathlib import Path

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from env_loader import parse_env_line  # noqa: E402


def _env_file_has_truthy_openai_enabled(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        p = parse_env_line(line)
        if p and p[0] == "OPENAI_API_ENABLED":
            return p[1].strip().lower() in ("1", "true", "yes", "on")
    return False


def _read_value(args: argparse.Namespace) -> str:
    if args.value is not None:
        return args.value
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return getpass.getpass(f"Enter value for {args.key}: ")


def upsert_env_file(path: Path, updates: dict[str, str]) -> None:
    raw = path.read_text(encoding="utf-8") if path.is_file() else ""
    kept: list[str] = []
    replaced: set[str] = set()
    for line in raw.splitlines(keepends=True):
        p = parse_env_line(line.rstrip("\n\r"))
        if p and p[0] in updates:
            if p[0] not in replaced:
                kept.append(f"{p[0]}={json.dumps(updates[p[0]])}\n")
                replaced.add(p[0])
            continue
        kept.append(line)
    for k, v in updates.items():
        if k not in replaced:
            kept.append(f"{k}={json.dumps(v)}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(kept), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge keys into .env without full wizard overwrite")
    ap.add_argument("key", help="Environment variable name, e.g. GH_TOKEN")
    ap.add_argument(
        "--value",
        default=None,
        help="Value (omit for stdin or hidden prompt on TTY)",
    )
    ap.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=f"Target .env (default: {REPO_ROOT}/.env)",
    )
    ap.add_argument(
        "--skill",
        action="store_true",
        help="Also update ~/.openclaw/workspace/skills/cursor_handoff/.env with the same keys",
    )
    ap.add_argument(
        "--no-github-alias",
        action="store_true",
        help="Do not set GITHUB_TOKEN when setting GH_TOKEN (or vice versa)",
    )
    ap.add_argument(
        "--enable-openai",
        action="store_true",
        help="When setting OPENAI_API_KEY, also set OPENAI_API_ENABLED to 1 (required for tools that gate on it)",
    )
    args = ap.parse_args()
    key = args.key.strip()
    if not KEY_RE.match(key):
        print("Invalid key name.", file=sys.stderr)
        return 2
    val = _read_value(args).strip()
    if "\n" in val or "\r" in val:
        print("Value must be a single line.", file=sys.stderr)
        return 2
    updates = {key: val}
    if key in ("GH_TOKEN", "GITHUB_TOKEN") and not args.no_github_alias:
        other = "GITHUB_TOKEN" if key == "GH_TOKEN" else "GH_TOKEN"
        updates[other] = val
    if key == "OPENAI_API_KEY":
        if args.enable_openai:
            updates["OPENAI_API_ENABLED"] = "1"

    target = args.env_file or (REPO_ROOT / ".env")
    if (
        key == "OPENAI_API_KEY"
        and not args.enable_openai
        and sys.stderr.isatty()
        and not _env_file_has_truthy_openai_enabled(target)
    ):
        print(
            "Hint: integrations ignore OPENAI_API_KEY unless OPENAI_API_ENABLED is truthy. "
            "Re-run with --enable-openai or set OPENAI_API_ENABLED to 1.",
            file=sys.stderr,
        )
    upsert_env_file(target, updates)
    print(f"Updated {target} (mode 600). Keys: {', '.join(sorted(updates))}")

    if args.skill:
        skill_env = Path.home() / ".openclaw" / "workspace" / "skills" / "cursor_handoff" / ".env"
        upsert_env_file(skill_env, updates)
        print(f"Updated {skill_env} (mode 600).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
