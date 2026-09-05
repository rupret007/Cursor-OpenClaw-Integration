#!/usr/bin/env python3
"""
Compute an Andrea readiness grade (A/B/C) and fail-closed action plan from
andrea_capabilities JSON.

A — No blocked capabilities; optional gaps are modest.
B — No blocked rows, but many ready_with_limits (degraded / optional missing).
C — Any blocked row (especially critical ones called out in reasons).
    Critical capabilities explicitly not run offline also remain unverified/no-go.

Human and JSON output use code-owned actions rather than copying free-form
probe notes, keeping the result safe for shared agent and dashboard handoffs.

Every plan also names the next step for Andrea, the coding agent (Bob), and
the owner, plus fail-closed holds. Grade A never leaves next_action empty.
Human output leads with a marked operator recap so Andrea vs Bob vs owner is
obvious before reasons or summary JSON.

Usage:
  python3 scripts/andrea_readiness_grade.py
  python3 scripts/andrea_readiness_grade.py --json
  python3 scripts/andrea_readiness_grade.py --offline --json
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
    "binary:openclaw": 0,
    "openclaw:skills_list": 5,
    "skill:cursor_handoff": 10,
    "github:auth": 20,
    "skill:github": 30,
    "skill:gh-issues": 31,
    "skill:telegram": 40,
    "skill:brave-api-search": 50,
    "skill:add-minimax-provider": 60,
}

READINESS_HOLDS = (
    "Do not send any live message.",
    "Keep Private API off.",
    "Do not live-send BlueBubbles.",
    "Do not write credentials into the repository.",
    "Do not merge, tag, deploy, or restart a gateway unless the owner explicitly asks.",
)

ANDREA_ROLE = (
    "Andrea handles Telegram/Alexa operator replies and personal-message drafts. "
    "Send only after the exact standalone phrases send it / send it now / send now."
)
CODING_AGENT_ROLE = (
    "The coding agent (Bob) handles offline repo work, tests, and draft PRs. "
    "Bob does not install skills, mutate credentials, or send messages."
)
OWNER_ROLE = (
    "The owner alone installs skills, restores auth, approves gateway restarts, "
    "and authorizes any live send."
)

NO_OWNER_SETUP = "No owner setup is required for the current capability matrix."
REVIEW_LIMITS = "Review the ready-with-limits rows before relying on optional capabilities."
RESTORE_MATRIX = "Restore a valid capability matrix before autonomous operation."
GRADE_A_NEXT = (
    "No capability blockers. Continue the assigned offline task; do not send messages, "
    "enable Private API, or restart the gateway unless the owner asks."
)
OPERATOR_RECAP_START = "--- Operator next steps ---"
OPERATOR_RECAP_END = "--- End operator next steps ---"
WHO_ACTS_FIRST_OWNER = "owner"
WHO_ACTS_FIRST_CODING_AGENT = "coding_agent"


def _safe_capability_id(row: dict) -> str:
    value = str(row.get("id") or "capability")[:120]
    return value if re.fullmatch(r"[A-Za-z0-9_.:-]+", value) else "capability"


def _action_for_row(row: dict) -> str:
    """Return code-owned guidance without echoing probe notes or secret values."""
    capability_id = _safe_capability_id(row)
    name = capability_id.split(":", 1)[-1]
    if row.get("status") == "not_run":
        return (
            f"Capability {capability_id} was not verified in offline mode; this is not "
            "a missing-install or failed-auth diagnosis. Continue unrelated local code "
            "and tests only. The owner must approve a separate live readiness check "
            "before relying on this capability."
        )
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


def _default_next_action(grade: str) -> str:
    if grade == "C":
        return RESTORE_MATRIX
    if grade == "B":
        return REVIEW_LIMITS
    return GRADE_A_NEXT


def _andrea_next_action(grade: str) -> str:
    if grade == "C":
        return (
            "Do not run autonomous or live communication work. Keep outbound drafts "
            "pending. Wait for the owner to clear the first Next action, then rerun "
            "bash scripts/andrea_doctor.sh --offline."
        )
    if grade == "B":
        return (
            "Stay on verified lanes only. Optional or degraded capabilities are not ready. "
            "Personal messages still require the exact standalone phrases send it / "
            "send it now / send now. Do not enable Private API or live-send BlueBubbles."
        )
    return (
        "Verified lanes may be used. Personal messages still require the exact standalone "
        "phrases send it / send it now / send now. Do not enable Private API or "
        "live-send BlueBubbles unless the owner asks."
    )


def _coding_agent_next_action(grade: str) -> str:
    if grade == "C":
        return (
            "Stay offline. Do not install skills, restart OpenClaw, mutate credentials, "
            "or send messages. Report the first Next action to the owner and continue "
            "only with local code and tests that do not need the blocked lane."
        )
    if grade == "B":
        return (
            "Continue offline code and tests. Do not treat ready-with-limits rows as ready. "
            "Do not send messages, enable Private API, or change credentials."
        )
    return (
        "No capability blockers. Continue offline verification or the assigned code task. "
        "Do not send messages, enable Private API, or restart the gateway unless the owner asks."
    )


def _who_acts_first(grade: str, actions: list | None = None) -> str:
    """Name the actor who must move before anyone else treats the stack as ready."""
    if grade == "C" or actions or grade == "B":
        return WHO_ACTS_FIRST_OWNER
    return WHO_ACTS_FIRST_CODING_AGENT


def _who_acts_first_label(who: str) -> str:
    if who == WHO_ACTS_FIRST_CODING_AGENT:
        return "coding agent (Bob)"
    return "owner"


def attach_actor_contract(plan: dict, grade: str) -> dict:
    """Fill actor lanes and holds so next steps are never implied from a table."""
    actions = plan.get("actions") if isinstance(plan.get("actions"), list) else []
    next_action = plan.get("next_action") or _default_next_action(grade)
    if actions:
        owner_next = next_action
    elif grade == "A":
        owner_next = NO_OWNER_SETUP
    else:
        owner_next = next_action
    plan["next_action"] = next_action
    plan["owner_next_action"] = owner_next
    plan["andrea_next_action"] = _andrea_next_action(grade)
    plan["coding_agent_next_action"] = _coding_agent_next_action(grade)
    plan["who_acts_first"] = _who_acts_first(grade, actions)
    plan["holds"] = list(READINESS_HOLDS)
    plan["routing"] = {
        "andrea": ANDREA_ROLE,
        "coding_agent": CODING_AGENT_ROLE,
        "owner": OWNER_ROLE,
    }
    return plan


def build_readiness_plan(data: dict, grade: str) -> dict:
    rows = data.get("rows")
    if not isinstance(rows, list):
        return attach_actor_contract(
            {
                "safe_for_autonomous_ops": False,
                "blocker_count": 1,
                "next_action": RESTORE_MATRIX,
                "actions": [
                    {
                        "id": "capabilities:payload",
                        "status": "blocked",
                        "critical": True,
                        "action": RESTORE_MATRIX,
                    }
                ],
            },
            "C",
        )

    blocked = [
        row for row in rows if isinstance(row, dict)
        and (row.get("status") == "blocked"
             or (row.get("critical") and row.get("status") == "not_run"))
    ]
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
                if row.get("status") in {"ready", "ready_with_limits", "blocked", "not_run"}
                else "blocked"
            ),
            "critical": bool(row.get("critical")),
            "action": _action_for_row(row),
        }
        for row in action_rows
    ]
    next_action = actions[0]["action"] if actions else None
    if grade == "B" and next_action is None:
        next_action = REVIEW_LIMITS
    return attach_actor_contract(
        {
            "safe_for_autonomous_ops": grade != "C",
            "blocker_count": len(blocked),
            "next_action": next_action,
            "actions": actions,
        },
        grade,
    )


def run_capabilities(*, offline: bool = False) -> dict:
    proc = subprocess.run(
        [sys.executable, str(CAP), "--json", *(["--offline"] if offline else [])],
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

    unverified = [r for r in rows if r.get("critical") and r.get("status") == "not_run"]
    if unverified:
        return "C", [f"critical_not_verified:{r.get('id')}" for r in unverified]

    if limits >= SOFT_LIMITS_THRESHOLD:
        reasons.append(f"ready_with_limits_count={limits}>={SOFT_LIMITS_THRESHOLD}")

    gh_row = next((r for r in rows if r.get("id") == "github:auth"), None)
    if gh_row and gh_row.get("status") == "ready_with_limits":
        reasons.append("github:auth_degraded")

    if reasons:
        return "B", reasons
    return "A", []


def format_operator_recap(payload: dict) -> str:
    """Compact Andrea / Bob / owner block operators can read without scrolling."""
    plan = payload.get("readiness_plan") if isinstance(payload.get("readiness_plan"), dict) else {}
    grade = str(payload.get("grade") or "C")
    actions = plan.get("actions") if isinstance(plan.get("actions"), list) else []
    who = plan.get("who_acts_first") or _who_acts_first(grade, actions)
    lines = [
        OPERATOR_RECAP_START,
        f"Andrea readiness grade: {payload.get('grade')}",
        "Safe for autonomous ops: " + ("yes" if plan.get("safe_for_autonomous_ops") else "no"),
        f"Who acts first: {_who_acts_first_label(str(who))}",
        f"Next action: {plan.get('next_action') or _default_next_action(grade)}",
        f"Next for Andrea: {plan.get('andrea_next_action') or _andrea_next_action(grade)}",
        (
            "Next for the coding agent (Bob): "
            + str(plan.get("coding_agent_next_action") or _coding_agent_next_action(grade))
        ),
        f"Next for the owner: {plan.get('owner_next_action') or NO_OWNER_SETUP}",
        "Holds:",
    ]
    holds = plan.get("holds") if isinstance(plan.get("holds"), list) else list(READINESS_HOLDS)
    for hold in holds:
        lines.append(f"  - {hold}")
    lines.append(OPERATOR_RECAP_END)
    return "\n".join(lines)


def extract_operator_recap(text: str) -> str:
    """Return the first marked operator recap from doctor or grade output."""
    captured: list[str] = []
    keep = False
    for line in text.splitlines():
        if line == OPERATOR_RECAP_START:
            keep = True
            captured.append(line)
            continue
        if keep:
            captured.append(line)
            if line == OPERATOR_RECAP_END:
                break
    return "\n".join(captured)


def format_readiness_human(payload: dict) -> str:
    """Render the shared next-step contract without a clipped capability table."""
    plan = payload.get("readiness_plan") if isinstance(payload.get("readiness_plan"), dict) else {}
    lines = [format_operator_recap(payload)]
    reasons = payload.get("reasons") or []
    if reasons:
        lines.append("Grade reasons:")
        for reason in reasons:
            lines.append(f"  - {reason}")
    summary = payload.get("summary")
    if isinstance(summary, dict) and summary:
        lines.append(json.dumps(summary, indent=2))
    routing = plan.get("routing") if isinstance(plan.get("routing"), dict) else {}
    lines.append("Routing:")
    lines.append(f"  Andrea: {routing.get('andrea') or ANDREA_ROLE}")
    lines.append(f"  Coding agent (Bob): {routing.get('coding_agent') or CODING_AGENT_ROLE}")
    lines.append(f"  Owner: {routing.get('owner') or OWNER_ROLE}")
    actions = plan.get("actions") if isinstance(plan.get("actions"), list) else []
    if actions:
        lines.append("Readiness action plan:")
        for index, item in enumerate(actions, start=1):
            if not isinstance(item, dict):
                continue
            priority = "critical" if item.get("critical") else item.get("status")
            lines.append(f"  {index}. {item.get('id')} ({priority}) — {item.get('action')}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Andrea readiness grade from capability matrix")
    ap.add_argument("--json", action="store_true", help="Print machine-readable grade payload")
    ap.add_argument(
        "--offline", action="store_true",
        help="Skip external capability probes; unverified critical lanes remain no-go",
    )
    ap.add_argument(
        "--json-out",
        type=Path,
        help="Also write the same safe machine-readable payload to this file",
    )
    args = ap.parse_args()

    data = run_capabilities(offline=args.offline)
    grade, reasons = grade_from_payload(data)
    readiness_plan = build_readiness_plan(data, grade)

    payload = {
        "grade": grade,
        "reasons": reasons,
        "summary": data.get("summary") if isinstance(data.get("summary"), dict) else {},
        "repo_root": str(REPO),
        "readiness_plan": readiness_plan,
    }

    json_payload = json.dumps(payload, indent=2)
    if args.json_out:
        args.json_out.write_text(json_payload + "\n", encoding="utf-8")
        args.json_out.chmod(0o600)

    if args.json:
        print(json_payload)
    else:
        print(format_readiness_human(payload))

    return 0 if grade != "C" else 1


if __name__ == "__main__":
    raise SystemExit(main())
