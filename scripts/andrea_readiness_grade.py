#!/usr/bin/env python3
"""
Compute an Andrea readiness grade (A/B/C) and fail-closed action plan from
andrea_capabilities JSON.

A — No blocked capabilities; optional gaps are modest.
B — No blocked rows, but many ready_with_limits (degraded / optional missing).
C — Any blocked row (especially critical ones called out in reasons).

Human and JSON output use code-owned actions rather than copying free-form
probe notes, keeping the result safe for shared agent and dashboard handoffs.

Usage:
  python3 scripts/andrea_readiness_grade.py
  python3 scripts/andrea_readiness_grade.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CAP = REPO / "scripts" / "andrea_capabilities.py"

# Hybrid lane adds many optional rows (skills + CLIs). Keep this high enough that a healthy
# core stack can still grade A while optional/hybrid gaps remain visible as ready_with_limits.
SOFT_LIMITS_THRESHOLD = 65
ACTION_PRIORITY = {
    "openclaw:skills_list": 0,
    "skill:cursor_handoff": 10,
    "github:auth": 20,
    "skill:github": 30,
    "skill:gh-issues": 31,
    "skill:telegram": 40,
    "skill:brave-api-search": 50,
    "skill:add-minimax-provider": 60,
}


def _safe_capability_id(row: dict) -> str:
    value = str(row.get("id") or "capability")[:120]
    return value if re.fullmatch(r"[A-Za-z0-9_.:-]+", value) else "capability"


def _action_for_row(row: dict) -> str:
    """Return code-owned guidance without echoing probe notes or secret values."""
    capability_id = _safe_capability_id(row)
    name = capability_id.split(":", 1)[-1]
    if capability_id == "skill:cursor_handoff":
        return (
            "Review docs/OPENCLAW_SKILL.md and install the checked-in cursor_handoff "
            "skill. Restart OpenClaw only when the owner approves, then rerun "
            "bash scripts/andrea_doctor.sh --offline."
        )
    if capability_id.startswith("skill:"):
        return (
            f"Install or enable the OpenClaw skill {name}, then rerun "
            "bash scripts/andrea_doctor.sh --offline before live use."
        )
    if capability_id == "openclaw:skills_list":
        return "Restore read-only openclaw skills list output before trusting any skill status."
    if capability_id.startswith("binary:"):
        return f"Install the required {name} binary, then rerun bash scripts/andrea_doctor.sh --offline."
    if capability_id == "github:auth":
        return "Run gh auth status and have the owner restore GitHub authentication before delegated GitHub work."
    if capability_id.startswith("secret:"):
        return (
            f"Have the owner configure {name} in the local environment or ignored .env; "
            "never paste or commit the value."
        )
    if capability_id.startswith("cursor:"):
        return "Run the redacted Cursor diagnose command and resolve its first reported blocker before handoff."
    return f"Inspect blocked capability {capability_id} in the capability matrix, resolve it, and rerun the offline doctor."


def build_readiness_plan(data: dict, grade: str) -> dict:
    rows = data.get("rows")
    if not isinstance(rows, list):
        return {
            "safe_for_autonomous_ops": False,
            "blocker_count": 1,
            "next_action": "Restore a valid capability matrix before autonomous operation.",
            "actions": [
                {
                    "id": "capabilities:payload",
                    "status": "blocked",
                    "critical": True,
                    "action": "Restore a valid capability matrix before autonomous operation.",
                }
            ],
        }

    blocked = [row for row in rows if isinstance(row, dict) and row.get("status") == "blocked"]
    blocked.sort(
        key=lambda row: (
            not bool(row.get("critical")),
            ACTION_PRIORITY.get(_safe_capability_id(row), 100),
            _safe_capability_id(row),
        )
    )
    action_rows = blocked
    if not action_rows:
        github_auth = next(
            (
                row
                for row in rows
                if isinstance(row, dict)
                and row.get("id") == "github:auth"
                and row.get("status") == "ready_with_limits"
            ),
            None,
        )
        action_rows = [github_auth] if github_auth else []

    actions = [
        {
            "id": _safe_capability_id(row),
            "status": (
                str(row.get("status"))
                if row.get("status") in {"ready", "ready_with_limits", "blocked"}
                else "blocked"
            ),
            "critical": bool(row.get("critical")),
            "action": _action_for_row(row),
        }
        for row in action_rows
    ]
    next_action = actions[0]["action"] if actions else None
    if grade == "B" and next_action is None:
        next_action = "Review the ready-with-limits rows before relying on optional capabilities."
    return {
        "safe_for_autonomous_ops": grade != "C",
        "blocker_count": len(blocked),
        "next_action": next_action,
        "actions": actions,
    }


def run_capabilities() -> dict:
    proc = subprocess.run(
        [sys.executable, str(CAP), "--json"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": proc.stderr.strip() or proc.stdout.strip() or "capabilities failed",
        }
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"invalid json: {e}"}


def grade_from_payload(data: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if data.get("ok") is False:
        return "C", [data.get("error", "capabilities_unavailable")]

    rows = data.get("rows")
    if not isinstance(rows, list):
        return "C", ["missing_or_invalid_rows"]
    rows = rows or []
    summary = data.get("summary") or {}
    blocked = int(summary.get("blocked") or 0)
    limits = int(summary.get("ready_with_limits") or 0)

    crit_blocked = [r for r in rows if r.get("critical") and r.get("status") == "blocked"]
    any_blocked = [r for r in rows if r.get("status") == "blocked"]

    if crit_blocked:
        for r in crit_blocked:
            reasons.append(f"critical_blocked:{r.get('id')}")
        return "C", reasons

    if any_blocked:
        for r in any_blocked[:8]:
            reasons.append(f"blocked:{r.get('id')}")
        if len(any_blocked) > 8:
            reasons.append(f"blocked:…+{len(any_blocked) - 8}_more")
        return "C", reasons

    if limits >= SOFT_LIMITS_THRESHOLD:
        reasons.append(f"ready_with_limits_count={limits}>={SOFT_LIMITS_THRESHOLD}")

    gh_row = next((r for r in rows if r.get("id") == "github:auth"), None)
    if gh_row and gh_row.get("status") == "ready_with_limits":
        reasons.append("github:auth_degraded")

    if reasons:
        return "B", reasons
    return "A", []


def main() -> int:
    ap = argparse.ArgumentParser(description="Andrea readiness grade from capability matrix")
    ap.add_argument("--json", action="store_true", help="Print machine-readable grade payload")
    args = ap.parse_args()

    data = run_capabilities()
    grade, reasons = grade_from_payload(data)
    readiness_plan = build_readiness_plan(data, grade)

    payload = {
        "grade": grade,
        "reasons": reasons,
        "summary": data.get("summary") if isinstance(data.get("summary"), dict) else {},
        "repo_root": str(REPO),
        "readiness_plan": readiness_plan,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Andrea readiness grade: {grade}")
        if reasons:
            for r in reasons:
                print(f"  - {r}")
        if payload["summary"]:
            print(json.dumps(payload["summary"], indent=2))
        print(
            "Safe for autonomous ops: "
            + ("yes" if readiness_plan["safe_for_autonomous_ops"] else "no")
        )
        if readiness_plan["next_action"]:
            print(f"Next action: {readiness_plan['next_action']}")
        if readiness_plan["actions"]:
            print("Readiness action plan:")
            for index, item in enumerate(readiness_plan["actions"], start=1):
                priority = "critical" if item["critical"] else item["status"]
                print(f"  {index}. {item['id']} ({priority}) — {item['action']}")

    return 0 if grade != "C" else 1


if __name__ == "__main__":
    raise SystemExit(main())
