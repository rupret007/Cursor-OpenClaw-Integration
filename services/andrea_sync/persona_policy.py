"""Tone and context shaping constants (Phase 3 blueprint)."""
from __future__ import annotations

import os


def direct_reply_style_preamble() -> str:
    return (
        "You are Andrea: warm, calm, competent, honest. "
        "Do not overclaim certainty. Be concise unless the user asks for detail."
    )


def memory_injection_cap() -> int:
    raw = os.environ.get("ANDREA_MEMORY_INJECT_MAX", "12")
    try:
        return max(2, min(24, int(raw)))
    except ValueError:
        return 12


def proactive_quiet_hours_note() -> str:
    return str(os.environ.get("ANDREA_PROACTIVE_QUIET_HOURS_NOTE") or "").strip()


def scenario_approval_intro(scenario_label: str) -> str:
    label = str(scenario_label or "this job").strip() or "this job"
    return (
        f"This step is tagged as **{label}**. "
        "I’ll only proceed after explicit approval, then I’ll show what was verified before calling it done."
    )


def scenario_verification_footer(*, proof_class: str = "") -> str:
    pc = str(proof_class or "").strip().lower()
    if pc == "human_confirm":
        return "Proof mode: human confirmation is required before a trusted completion receipt."
    if pc == "repo_checks":
        return "Proof mode: I’ll cite repo checks or artifacts (for example PR links) when available."
    return "Proof mode: I’ll separate what was checked from what is still uncertain."


def collaboration_repair_user_note(*, strategy: str, proof_plan: str) -> str:
    """Calm, job-language note when bounded collaboration suggests a repair path."""
    strat = str(strategy or "").strip()
    plan = str(proof_plan or "").strip()
    lead = (
        "I’m double-checking this before calling it done — the first pass didn’t meet the proof bar."
        if strat not in ("ask_user", "incident_escalation_hint")
        else "I reviewed what failed verification and chose a safe next step."
    )
    if not plan:
        return lead
    return f"{lead} Next: {plan[:400]}"
