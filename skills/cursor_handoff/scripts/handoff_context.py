#!/usr/bin/env python3
"""
Shared intent templates and pre-handoff repo triage for Cursor handoff flows.

Used by:
  - skills/cursor_handoff/scripts/cursor_handoff.py
  - scripts/cursor_openclaw.py (create-agent)
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

INTENT_IDS = ("code-review", "refactor", "release-notes", "brief")

# {details} = operator free-form; may be empty
INTENT_TEMPLATES: Dict[str, str] = {
    "code-review": """## Intent: code review (read-only bias)
Focus on correctness, security, tests, API contracts, and maintainability.
Deliver: prioritized findings (severity), suggested patches or file-level notes, and test gaps.
Do not merge or deploy; analysis and recommendations only unless explicitly asked later.

Operator context:
{details}
""",
    "refactor": """## Intent: refactor / cleanup
Preserve behavior unless the operator requests functional change.
Deliver: small safe steps, migration notes, and tests to run after each step.
Prefer minimal diffs; call out risky areas before editing.

Operator context:
{details}
""",
    "release-notes": """## Intent: release notes
From the diff / recent commits / current branch, produce user-facing release notes.
Include: highlights, breaking changes, migration notes, and version-suggestion if obvious.
Keep tone concise and scannable (bullets).

Operator context:
{details}
""",
    "brief": """## Intent: creative / product brief
Turn the operator request into a structured brief: goals, audience, constraints, success metrics, and a phased plan.
Ask clarifying questions only if blocking; otherwise state assumptions explicitly.

Operator context:
{details}
""",
}


def expand_intent(intent_id: str, user_details: str) -> str:
    if intent_id not in INTENT_TEMPLATES:
        raise ValueError(f"Unknown intent {intent_id!r}. Use one of: {', '.join(INTENT_IDS)}")
    details = user_details.strip() if user_details.strip() else "(no additional details provided)"
    return INTENT_TEMPLATES[intent_id].format(details=details)


def build_repo_triage(repo_path: Path, max_entries: int = 40) -> str:
    """Collect non-secret repo snapshot for agent context (best-effort)."""
    root = repo_path.resolve()
    lines = ["## Pre-handoff repo triage", f"Path: {root}"]

    import subprocess

    def _run(cmd: list[str]) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            return proc.returncode, (proc.stdout or proc.stderr or "").strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            return 99, str(e)

    code, _ = _run(["git", "rev-parse", "--is-inside-work-tree"])
    if code != 0:
        lines.append("Git: not a git repository (triage limited to directory listing).")
    else:
        _, branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        _, remote = _run(["git", "remote", "get-url", "origin"])
        _, st = _run(["git", "status", "--short", "-uno"])
        lines.append(f"Branch: {branch or '?'}")
        lines.append(f"Origin: {remote or '(none)'}")
        lines.append("Git status (short):")
        lines.append(st if st else "(clean)")

    # Top-level entries (no content, no secrets)
    try:
        names = sorted(p.name for p in root.iterdir() if p.name != ".git")[:max_entries]
        lines.append("Top-level entries:")
        lines.append(", ".join(names) if names else "(empty)")
    except OSError as e:
        lines.append(f"Listing error: {e}")

    markers = []
    for name in ("pyproject.toml", "setup.py", "package.json", "go.mod", "Cargo.toml", "Makefile"):
        if (root / name).is_file():
            markers.append(name)
    if markers:
        lines.append("Project markers: " + ", ".join(markers))

    test_globs = list(root.glob("tests/test_*.py")) + list(root.glob("test/test_*.py"))
    if test_globs:
        lines.append(f"Sample tests found: {len(test_globs)} files under tests/ (Python)")

    lines.append("---")
    return "\n".join(lines)


def compose_handoff_body(
    user_prompt: str,
    intent: Optional[str],
    triage_repo: Optional[Path],
) -> str:
    """
    Build inner prompt text for API/CLI.
    Require at least one of: non-empty user_prompt, intent, or triage_repo.
    """
    has_user = bool(user_prompt.strip())
    if not intent and not has_user and triage_repo is None:
        raise ValueError("Provide --prompt text and/or --intent, and/or --triage with a local repo.")

    chunks: list[str] = []
    if triage_repo is not None:
        chunks.append(build_repo_triage(triage_repo))
    if intent:
        chunks.append(expand_intent(intent, user_prompt))
    elif has_user:
        chunks.append(user_prompt.strip())
    return "\n\n".join(chunks)
