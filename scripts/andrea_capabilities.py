#!/usr/bin/env python3
"""
Andrea runtime capability baseline: binaries, OpenClaw skills, GitHub auth drift,
and boolean-only secret presence (never prints secret values).

Usage:
  python3 scripts/andrea_capabilities.py
  python3 scripts/andrea_capabilities.py --json
  python3 scripts/andrea_capabilities.py --markdown-table
  python3 scripts/andrea_capabilities.py --strict   # exit 1 if critical capability blocked

Environment:
  ANDREA_REPO_ROOT  optional override for repo root (default: parent of scripts/)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(
    os.environ.get("ANDREA_REPO_ROOT", "")
    or Path(__file__).resolve().parent.parent
)
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from env_loader import parse_env_line  # noqa: E402

# Skills we expect when OpenClaw is healthy (substring match in `openclaw skills list` output).
EXPECTED_OPENCLAW_SKILLS = (
    "cursor_handoff",
    "github",
    "gh-issues",
    "gemini",
    "telegram",
    "brave-api-search",
    "add-minimax-provider",
)

# Optional integration binary (limits if missing); curl/git are listed explicitly below.
OPTIONAL_BINARIES = ("gemini",)

# Secrets: boolean presence only (.env file + process environment).
SECRET_KEYS = (
    "CURSOR_API_KEY",
    "OPENAI_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GEMINI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "BRAVE_SEARCH_API_KEY",
    "BRAVE_ANSWERS_API_KEY",
    "MINIMAX_API_KEY",
)

@dataclass
class Row:
    id: str
    category: str
    detail: str
    status: str
    notes: str = ""
    critical: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "detail": self.detail,
            "status": self.status,
            "notes": self.notes,
            "critical": self.critical,
        }


def _read_dotenv_keys(path: Path) -> Dict[str, str]:
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: Dict[str, str] = {}
    for line in text.splitlines():
        pair = parse_env_line(line)
        if pair:
            out[pair[0]] = pair[1]
    return out


def _which(name: str) -> bool:
    return shutil.which(name) is not None


def _run_capture(
    argv: List[str],
    *,
    timeout: float,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env or os.environ,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", "executable not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def _gh_auth_state() -> Tuple[str, str]:
    if not _which("gh"):
        return "blocked", "gh not on PATH"
    code, out, err = _run_capture(["gh", "auth", "status"], timeout=15.0)
    blob = (out + "\n" + err).lower()
    if code == 0 and ("logged in" in blob or "authenticated" in blob or "token:" in blob):
        return "ready", "gh reports authenticated session"
    token = bool(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    if token:
        return "ready_with_limits", "gh CLI session not confirmed; GH_TOKEN/GITHUB_TOKEN present"
    return "blocked", "gh not authenticated and no GH_TOKEN/GITHUB_TOKEN in environment"


def _openclaw_skills() -> Tuple[str, str, str]:
    if not _which("openclaw"):
        return "blocked", "", "openclaw not on PATH"
    code, out, err = _run_capture(["openclaw", "skills", "list"], timeout=45.0)
    if code != 0:
        return "ready_with_limits", "", f"openclaw skills list failed (exit {code}): {(err or out)[:200]}"
    return "ready", out + err, ""


def _skill_rows(skills_blob: str) -> List[Row]:
    rows: List[Row] = []
    lower = skills_blob.lower()
    for name in EXPECTED_OPENCLAW_SKILLS:
        needle = name.replace("_", " ").lower()
        found = name.lower() in lower or needle in lower
        if found:
            st = "ready"
            note = "listed by openclaw skills list (name match)"
        else:
            st = "blocked"
            note = "not detected in openclaw skills list output"
        rows.append(
            Row(
                id=f"skill:{name}",
                category="openclaw_skill",
                detail=name,
                status=st,
                notes=note,
                critical=False,
            )
        )
    return rows


def _cursor_diagnose_summary() -> Tuple[str, str]:
    """CLI health only; live API readiness is tracked on secret:CURSOR_API_KEY."""
    cli = REPO_ROOT / "scripts" / "cursor_openclaw.py"
    if not cli.is_file():
        return "blocked", "scripts/cursor_openclaw.py missing"
    env = {k: v for k, v in os.environ.items()}
    # Strip secrets for deterministic probe; CLI still loads .env if present.
    for k in SECRET_KEYS:
        env.pop(k, None)
    env.setdefault("CURSOR_API_KEY", "")
    env.setdefault("OPENAI_API_ENABLED", "0")
    code, out, err = _run_capture(
        [sys.executable, str(cli), "--json", "diagnose"],
        timeout=30.0,
        env=env,
    )
    if code != 0:
        return "blocked", f"diagnose exit {code}: {(err or out)[:300]}"
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return "blocked", "diagnose returned non-JSON"
    ok = data.get("ok") is True
    if not ok:
        return "ready_with_limits", "diagnose ok!=true; inspect cursor_openclaw output"
    present = data.get("api_key_present") is True
    if present:
        return "ready", "diagnose ok; CURSOR_API_KEY visible to CLI (env or .env)"
    return "ready", "diagnose ok; CURSOR_API_KEY not set in this probe (see secret:CURSOR_API_KEY)"


def build_matrix() -> List[Row]:
    rows: List[Row] = []
    dotenv_main = _read_dotenv_keys(REPO_ROOT / ".env")
    dotenv_skill = _read_dotenv_keys(
        Path.home() / ".openclaw" / "workspace" / "skills" / "cursor_handoff" / ".env"
    )

    def secret_in_env(key: str) -> bool:
        v = os.environ.get(key)
        if v is not None and str(v).strip() != "":
            return True
        if dotenv_main.get(key, "").strip() != "":
            return True
        if dotenv_skill.get(key, "").strip() != "":
            return True
        return False

    # Core runtime
    py_ok = _which("python3") or _which("python")
    rows.append(
        Row(
            id="binary:python",
            category="binary",
            detail=sys.executable,
            status="ready" if py_ok else "blocked",
            notes="interpreter for CLIs",
            critical=True,
        )
    )

    for name in ("openclaw", "gh", "git", "curl") + OPTIONAL_BINARIES:
        crit = name in ("openclaw", "gh")
        present = _which(name)
        if present:
            st = "ready"
            note = "on PATH"
        elif name == "gemini":
            st = "ready_with_limits"
            note = "optional Gemini CLI not required if using API-only skills"
        elif name in ("curl", "git"):
            st = "ready_with_limits"
            note = "recommended for DevOps workflows"
        else:
            st = "blocked"
            note = "missing from PATH"
        rows.append(
            Row(
                id=f"binary:{name}",
                category="binary",
                detail=name,
                status=st,
                notes=note,
                critical=crit and not present,
            )
        )

    oc_status, oc_out, oc_err = _openclaw_skills()
    rows.append(
        Row(
            id="openclaw:skills_list",
            category="openclaw",
            detail="openclaw skills list",
            status=oc_status,
            notes=oc_err or "skills enumeration",
            critical=False,
        )
    )
    if oc_status != "blocked":
        rows.extend(_skill_rows(oc_out))

    gh_st, gh_note = _gh_auth_state()
    rows.append(
        Row(
            id="github:auth",
            category="auth",
            detail="GitHub CLI / token",
            status=gh_st,
            notes=gh_note,
            critical=gh_st == "blocked",
        )
    )

    diag_st, diag_note = _cursor_diagnose_summary()
    rows.append(
        Row(
            id="cursor:diagnose",
            category="cursor_api",
            detail="cursor_openclaw diagnose",
            status=diag_st,
            notes=diag_note,
            critical=diag_st == "blocked",
        )
    )

    for key in SECRET_KEYS:
        present = secret_in_env(key)
        rows.append(
            Row(
                id=f"secret:{key}",
                category="secret_presence",
                detail=key,
                status="ready" if present else "ready_with_limits",
                notes="set in env or .env (value not shown)" if present else "absent",
                critical=key == "CURSOR_API_KEY" and not present,
            )
        )

    # OPENAI_API_ENABLED heuristic (boolean only via env files + os)
    def _truthy(val: str) -> bool:
        return val.strip().lower() in ("1", "true", "yes", "on")

    oa_key = secret_in_env("OPENAI_API_KEY")
    oa_en = os.environ.get("OPENAI_API_ENABLED", "")
    if not oa_en:
        oa_en = dotenv_main.get("OPENAI_API_ENABLED", "") or dotenv_skill.get(
            "OPENAI_API_ENABLED", ""
        )
    enabled = _truthy(str(oa_en))
    if oa_key and enabled:
        oa_status = "ready"
        oa_note = "OPENAI_API_KEY present and OPENAI_API_ENABLED truthy"
    elif oa_key and not enabled:
        oa_status = "ready_with_limits"
        oa_note = "key present but OPENAI_API_ENABLED not truthy — integrations should ignore"
    else:
        oa_status = "ready_with_limits"
        oa_note = "no OpenAI key configured (optional)"
    rows.append(
        Row(
            id="openai:integration",
            category="openai",
            detail="OPENAI_API_KEY + OPENAI_API_ENABLED",
            status=oa_status,
            notes=oa_note,
            critical=False,
        )
    )

    return rows


def _print_table(rows: List[Row]) -> None:
    headers = ("id", "category", "detail", "status", "critical", "notes")
    widths = [len(h) for h in headers]
    data = []
    for r in rows:
        line = (
            r.id,
            r.category,
            r.detail[:60],
            r.status,
            str(r.critical),
            (r.notes or "")[:80],
        )
        data.append(line)
        for i, cell in enumerate(line):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for line in data:
        print(fmt.format(*line))


def _print_markdown(rows: List[Row]) -> None:
    print("| id | category | detail | status | critical | notes |")
    print("|---|----------|--------|--------|----------|-------|")
    for r in rows:
        note = re.sub(r"\|", "\\|", r.notes or "")
        det = re.sub(r"\|", "\\|", r.detail or "")
        print(
            f"| `{r.id}` | {r.category} | {det} | **{r.status}** | {r.critical} | {note} |"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Andrea capability baseline matrix")
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    ap.add_argument("--markdown-table", action="store_true", help="Emit markdown table")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any row with critical=True is blocked",
    )
    args = ap.parse_args()
    rows = build_matrix()
    payload = {
        "ok": True,
        "repo_root": str(REPO_ROOT),
        "rows": [r.as_dict() for r in rows],
        "summary": {
            "ready": sum(1 for r in rows if r.status == "ready"),
            "ready_with_limits": sum(1 for r in rows if r.status == "ready_with_limits"),
            "blocked": sum(1 for r in rows if r.status == "blocked"),
        },
        "meta": {
            "model_policy_doc": "docs/ANDREA_MODEL_POLICY.md",
            "openclaw_probe_timeout_units": "ms",
            "readiness_grade_script": "scripts/andrea_readiness_grade.py",
            "security_sanity_script": "scripts/andrea_security_sanity.sh",
            "doctor_script": "scripts/andrea_doctor.sh",
        },
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    elif args.markdown_table:
        _print_markdown(rows)
    else:
        print(f"Andrea capability matrix (repo: {REPO_ROOT})")
        print(json.dumps(payload["summary"], indent=2))
        print()
        _print_table(rows)

    if args.strict:
        bad = [r for r in rows if r.critical and r.status == "blocked"]
        if bad:
            sys.stderr.write(
                "strict: blocked critical capabilities:\n"
                + "\n".join(f"  - {r.id}: {r.notes}" for r in bad)
                + "\n"
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
