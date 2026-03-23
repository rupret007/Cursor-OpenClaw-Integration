"""Action-to-verifier mapping and verdict rules for delegated execution."""
from __future__ import annotations

from typing import Any, Dict

from .tool_registry import manifest_for_lane


def verification_method_for_delegated_step(lane: str, step_kind: str) -> str:
    m = manifest_for_lane(lane if lane != "openclaw_hybrid" else "openclaw_hybrid")
    raw = str(m.get("verification_mode") or "repo_checks").strip().lower()
    if raw in {"none", "repo_checks", "command_exit", "artifact_presence", "human_confirm"}:
        return raw
    return "repo_checks"


def verification_method_for_scenario(
    lane: str,
    step_kind: str,
    scenario_proof_class: str = "",
) -> str:
    """
    Prefer scenario proof class when present; fall back to lane manifest defaults.
    """
    spc = str(scenario_proof_class or "").strip().lower()
    if spc == "human_confirm":
        return "human_confirm"
    if spc in {"provider_receipt", "citation"}:
        # Until dedicated adapters exist, require explicit human confirmation.
        return "human_confirm"
    if spc == "none":
        return "none"
    if spc == "repo_checks":
        return "repo_checks"
    return verification_method_for_delegated_step(lane, step_kind)


def evaluate_delegated_repo_outcome(
    *,
    terminal_status: str,
    pr_url: str = "",
    agent_url: str = "",
    lane: str = "",
    verification_method: str = "repo_checks",
) -> Dict[str, Any]:
    """
    Deterministic v1 verifier after Cursor/OpenClaw delegated execution reaches a terminal state.
    Returns dict: verdict (pass|fail|needs_human), summary, evidence.
    """
    ts = (terminal_status or "").strip().upper()
    pr = (pr_url or "").strip()
    agent = (agent_url or "").strip()
    evidence: Dict[str, Any] = {
        "terminal_status": ts,
        "has_pr_url": bool(pr),
        "has_agent_url": bool(agent),
        "lane": lane,
        "verification_method": verification_method,
    }
    if verification_method == "none":
        return {
            "verdict": "pass",
            "summary": "Verification skipped for this step (verification_mode=none).",
            "evidence": evidence,
        }
    if ts != "FINISHED":
        return {
            "verdict": "fail",
            "summary": f"Execution ended with non-success terminal status {ts or 'unknown'}.",
            "evidence": evidence,
        }
    if verification_method == "repo_checks":
        if pr:
            return {
                "verdict": "pass",
                "summary": "Repo verification: terminal FINISHED with an artifact/PR URL present.",
                "evidence": evidence,
            }
        if agent:
            return {
                "verdict": "needs_human",
                "summary": (
                    "Execution reported FINISHED but no PR URL was captured; "
                    "objective repo verification is weak — confirm the outcome if acceptable."
                ),
                "evidence": evidence,
            }
        return {
            "verdict": "fail",
            "summary": "FINISHED without PR or agent URL evidence for repo verification.",
            "evidence": evidence,
        }
    if verification_method == "artifact_presence":
        if pr or agent:
            return {
                "verdict": "pass",
                "summary": "Artifact presence check satisfied.",
                "evidence": evidence,
            }
        return {
            "verdict": "fail",
            "summary": "Expected artifact URL missing after execution.",
            "evidence": evidence,
        }
    if verification_method == "human_confirm":
        return {
            "verdict": "needs_human",
            "summary": "This step requires explicit human confirmation before marking verified.",
            "evidence": evidence,
        }
    return {
        "verdict": "pass" if ts == "FINISHED" else "fail",
        "summary": "Default terminal-status verification.",
        "evidence": evidence,
    }
