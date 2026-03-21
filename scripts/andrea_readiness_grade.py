#!/usr/bin/env python3
"""
Compute a simple Andrea readiness grade (A/B/C) from andrea_capabilities JSON.

A — No blocked capabilities; optional gaps are modest.
B — No blocked rows, but many ready_with_limits (degraded / optional missing).
C — Any blocked row (especially critical ones called out in reasons).

Usage:
  python3 scripts/andrea_readiness_grade.py
  python3 scripts/andrea_readiness_grade.py --json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CAP = REPO / "scripts" / "andrea_capabilities.py"

SOFT_LIMITS_THRESHOLD = 12  # many optional integrations absent → still OK but grade B


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

    payload = {
        "grade": grade,
        "reasons": reasons,
        "summary": data.get("summary") if isinstance(data.get("summary"), dict) else {},
        "repo_root": str(REPO),
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

    return 0 if grade != "C" else 1


if __name__ == "__main__":
    raise SystemExit(main())
