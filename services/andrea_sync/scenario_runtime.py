"""Resolve user turns to scenario contracts and shape trust-facing copy."""
from __future__ import annotations

import re
import uuid
from typing import Any, Dict, Optional, Tuple

from .scenario_registry import (
    FIRST_SUPPORTED_SCENARIO_IDS,
    SCENARIO_CATALOG,
    default_contract,
    get_contract,
)
from .scenario_schema import (
    DRAFT_ONLY,
    SUPPORTED_APPROVAL,
    SUPPORTED_AUTO,
    UNSUPPORTED,
    ScenarioContract,
    ScenarioReceipt,
    ScenarioResolution,
    scenario_blob_for_job_payload,
)
from .andrea_router import AndreaRouteDecision

_UNSAFE = re.compile(
    r"\b("
    r"hack\b|ransomware|illegal\s+surveillance|stalk\s|weapon|"
    r"ddos|credential\s+stuffing|identity\s+theft\s+help"
    r")\b",
    re.I,
)
_STATUS = re.compile(
    r"\b("
    r"what\s+happened|what\s+happened\s+there|where\s+are\s+we|status(\s+of)?|what'?s\s+the\s+status|"
    r"continue(\s+that|\s+this)?|follow[\s-]*up|any\s+update|progress(\s+so\s+far)?|"
    r"what\s+are\s+we\s+working\s+on(?:\s+right\s+now|\s+with\s+andrea)?|"
    r"working\s+on\s+right\s+now|working\s+on\s+with\s+andrea|"
    r"what'?s\s+blocked|blocked\s+right\s+now|what\s+is\s+blocking|main\s+blocker|"
    r"what\s+happened\s+with\s+(?:that\s+)?task|what\s+did\s+cursor\s+say|"
    r"needs?\s+(my|our)\s+approval|awaiting\s+(my|our)\s+approval|"
    r"pending\s+(my|our)\s+approval|waiting\s+on\s+(my|our)\s+approval|"
    r"what\s+still\s+needs\s+(my|our)\s+approval"
    r")\b",
    re.I,
)
_REMINDER = re.compile(
    r"\b(remember\s+that|remind\s+me|set\s+a\s+reminder|note\s+to\s+self|"
    r"don'?t\s+let\s+me\s+forget)\b",
    re.I,
)
_INBOX = re.compile(
    r"\b("
    r"recent\s+messages|my\s+inbox|text\s+messages|last\s+texts|bluebubbles|"
    r"imessage|sms\s+thread"
    r")\b",
    re.I,
)
_OUTBOUND = re.compile(
    r"\b("
    r"send\s+(an?\s+)?(email|e-mail|message|text|dm)|"
    r"post\s+(this|that)\s+to|schedule\s+(a\s+)?(meeting|call)|"
    r"calendar\s+invite|invite\s+everyone"
    r")\b",
    re.I,
)
_VERIFY_SENS = re.compile(
    r"\b("
    r"don'?t\s+say\s+done|do\s+not\s+say\s+done|without\s+proof|"
    r"verify\s+(before|the\s+proof)|proof\s+before|"
    r"need\s+verification\s+before|until\s+you\s+verify"
    r")\b",
    re.I,
)
_RESUME = re.compile(
    r"\b("
    r"tomorrow|next\s+session|later\s+today|pick\s+up\s+where|"
    r"resume\s+(this|that|work)|when\s+i\s+come\s+back"
    r")\b",
    re.I,
)
_RESEARCH = re.compile(
    r"\b("
    r"search\s+the\s+web|look\s+up\s+online|fresh\s+(news|headlines)|"
    r"what'?s\s+the\s+latest|what'?s\s+the\s+news|what'?s\s+in\s+the\s+news|"
    r"news\s+today|headlines?\s+today|research\s+"
    r")\b",
    re.I,
)
_TROUBLE = re.compile(
    r"\b("
    r"diagnose|root\s+cause|troubleshoot|won'?t\s+start|keeps\s+crashing|"
    r"multi-?step\s+fix"
    r")\b",
    re.I,
)
_REPO = re.compile(
    r"\b("
    r"code|repo|repository|file|branch|commit|pull\s+request|pr\b|debug|tests?\b|"
    r"implement|fix|bug|refactor|patch|github|lint|traceback"
    r")\b",
    re.I,
)
_PATH = re.compile(r"[/~][\w.\-~/]+|`[^`]+`|\b\w+\.(py|ts|tsx|js|jsx|md|sh|json|yaml|yml)\b", re.I)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


def resolve_scenario(
    user_text: str,
    *,
    goal_id: str = "",
    route_decision: Optional[AndreaRouteDecision] = None,
) -> Tuple[ScenarioResolution, ScenarioContract]:
    """
    Map natural language + routing hint to a scenario contract.
    Returns (resolution, contract).
    """
    raw = str(user_text or "")
    clean = _norm(raw)
    suggested_lane = ""
    if route_decision and route_decision.mode == "delegate":
        suggested_lane = str(route_decision.delegate_target or "openclaw_hybrid")

    if _UNSAFE.search(clean):
        c = SCENARIO_CATALOG["unsupportedOrUnsafeRequest"]
        return (
            ScenarioResolution(
                scenario_id=c.scenario_id,
                confidence=0.95,
                support_level=c.support_level,
                reason="unsafe_or_disallowed_intent",
                goal_id=goal_id,
                needs_plan=False,
                suggested_lane="",
                action_class=c.action_class,
                proof_class=c.proof_class,
                approval_mode=c.approval_mode,
            ),
            c,
        )

    if _OUTBOUND.search(clean):
        c = SCENARIO_CATALOG["approvalRequiredOutboundAction"]
        return (
            ScenarioResolution(
                scenario_id=c.scenario_id,
                confidence=0.85,
                support_level=c.support_level,
                reason="outbound_write_keywords",
                goal_id=goal_id,
                needs_plan=True,
                suggested_lane=suggested_lane,
                action_class=c.action_class,
                proof_class=c.proof_class,
                approval_mode=c.approval_mode,
            ),
            c,
        )

    if _VERIFY_SENS.search(clean):
        c = SCENARIO_CATALOG["verificationSensitiveAction"]
        return (
            ScenarioResolution(
                scenario_id=c.scenario_id,
                confidence=0.8,
                support_level=c.support_level,
                reason="explicit_proof_or_verify_language",
                goal_id=goal_id,
                needs_plan=True,
                suggested_lane=suggested_lane or "openclaw_hybrid",
                action_class=c.action_class,
                proof_class=c.proof_class,
                approval_mode=c.approval_mode,
            ),
            c,
        )

    if _STATUS.search(clean):
        c = SCENARIO_CATALOG["statusFollowupContinue"]
        return (
            ScenarioResolution(
                scenario_id=c.scenario_id,
                confidence=0.82,
                support_level=c.support_level,
                reason="status_or_followup_language",
                goal_id=goal_id,
                needs_plan=False,
                suggested_lane="direct_assistant",
                action_class=c.action_class,
                proof_class=c.proof_class,
                approval_mode=c.approval_mode,
            ),
            c,
        )

    if _REMINDER.search(clean):
        c = SCENARIO_CATALOG["noteOrReminderCapture"]
        return (
            ScenarioResolution(
                scenario_id=c.scenario_id,
                confidence=0.78,
                support_level=c.support_level,
                reason="reminder_or_memory_capture",
                goal_id=goal_id,
                needs_plan=False,
                suggested_lane="direct_assistant",
                action_class=c.action_class,
                proof_class=c.proof_class,
                approval_mode=c.approval_mode,
            ),
            c,
        )

    if _INBOX.search(clean):
        c = SCENARIO_CATALOG["recentMessagesOrInboxLookup"]
        return (
            ScenarioResolution(
                scenario_id=c.scenario_id,
                confidence=0.8,
                support_level=c.support_level,
                reason="inbox_or_messages_lookup",
                goal_id=goal_id,
                needs_plan=False,
                suggested_lane="direct_assistant",
                action_class=c.action_class,
                proof_class=c.proof_class,
                approval_mode=c.approval_mode,
            ),
            c,
        )

    if _RESUME.search(clean):
        c = SCENARIO_CATALOG["goalContinuationAcrossSessions"]
        return (
            ScenarioResolution(
                scenario_id=c.scenario_id,
                confidence=0.72,
                support_level=c.support_level,
                reason="session_resume_language",
                goal_id=goal_id,
                needs_plan=False,
                suggested_lane=suggested_lane or "direct_assistant",
                action_class=c.action_class,
                proof_class=c.proof_class,
                approval_mode=c.approval_mode,
            ),
            c,
        )

    if _RESEARCH.search(clean) and not _REPO.search(clean):
        c = SCENARIO_CATALOG["researchSummary"]
        return (
            ScenarioResolution(
                scenario_id=c.scenario_id,
                confidence=0.7,
                support_level=c.support_level,
                reason="research_or_web_language",
                goal_id=goal_id,
                needs_plan=False,
                suggested_lane=suggested_lane or "direct_assistant",
                action_class=c.action_class,
                proof_class=c.proof_class,
                approval_mode=c.approval_mode,
            ),
            c,
        )

    if _TROUBLE.search(clean):
        c = SCENARIO_CATALOG["multiStepTroubleshoot"]
        return (
            ScenarioResolution(
                scenario_id=c.scenario_id,
                confidence=0.68,
                support_level=c.support_level,
                reason="troubleshooting_language",
                goal_id=goal_id,
                needs_plan=True,
                suggested_lane=suggested_lane or "openclaw_hybrid",
                action_class=c.action_class,
                proof_class=c.proof_class,
                approval_mode=c.approval_mode,
            ),
            c,
        )

    if route_decision and route_decision.mode == "delegate":
        if _REPO.search(clean) or _PATH.search(raw):
            c = SCENARIO_CATALOG["repoHelpVerified"]
            return (
                ScenarioResolution(
                    scenario_id=c.scenario_id,
                    confidence=0.75,
                    support_level=c.support_level,
                    reason="delegated_repo_or_code_request",
                    goal_id=goal_id,
                    needs_plan=True,
                    suggested_lane=suggested_lane,
                    action_class=c.action_class,
                    proof_class=c.proof_class,
                    approval_mode=c.approval_mode,
                ),
                c,
            )

    c = default_contract()
    delegate = bool(route_decision and route_decision.mode == "delegate")
    return (
        ScenarioResolution(
            scenario_id=c.scenario_id,
            confidence=0.45,
            support_level=c.support_level,
            reason="default_mixed_or_unclassified",
            goal_id=goal_id,
            needs_plan=delegate,
            suggested_lane=suggested_lane or "direct_assistant",
            action_class=c.action_class,
            proof_class=c.proof_class,
            approval_mode=c.approval_mode,
        ),
        c,
    )


def delegate_should_be_blocked(
    contract: ScenarioContract,
    *,
    route_mode: str,
) -> bool:
    if route_mode != "delegate":
        return False
    if contract.support_level == UNSUPPORTED:
        return True
    if contract.blocks_auto_delegate:
        return True
    return False


def unsupported_user_message(contract: ScenarioContract) -> str:
    label = contract.user_facing_label or "that kind of request"
    return (
        f"I can’t help with {label} in a safe, reliable way yet. "
        "If you have a different goal (repo work, status on an active task, reminders, or reading recent messages), "
        "tell me in plain language and I’ll stay within those supported jobs."
    )


def draft_only_delegate_message(contract: ScenarioContract) -> str:
    label = contract.user_facing_label or "this job type"
    return (
        f"This looks like **{label}**, which is still **draft-only** here — I won’t auto-run delegated execution for it yet. "
        "I can help you shape a plan, capture a reminder, or switch to a supported scenario like repo changes with verification. "
        "What would you like instead?"
    )


def scenario_job_payload_fields(
    resolution: ScenarioResolution, contract: ScenarioContract
) -> Dict[str, Any]:
    return {"scenario": scenario_blob_for_job_payload(resolution, contract)}


def build_scenario_receipt(
    *,
    plan_id: str,
    scenario_id: str,
    verified: bool,
    proof_summary: str,
    remaining_risks: Optional[list] = None,
    next_safe_action: str = "",
    proof_items: Optional[list] = None,
) -> ScenarioReceipt:
    return ScenarioReceipt(
        receipt_id=f"rcpt_{uuid.uuid4().hex[:16]}",
        scenario_id=scenario_id,
        plan_id=plan_id,
        verified=verified,
        proof_items=list(proof_items or []),
        user_summary=proof_summary[:2000],
        remaining_risks=[str(x) for x in (remaining_risks or [])][:12],
        next_safe_action=(next_safe_action or "")[:1200],
    )


def normalize_execution_lane_for_scenario(lane: str) -> str:
    """Map runtime lane aliases to catalog lane ids used in ``allowed_lanes``."""
    l = str(lane or "").strip()
    if l in ("direct_cursor", "cursor_direct"):
        return "cursor"
    return l


def lane_allowed_for_scenario(contract: ScenarioContract, lane: str) -> bool:
    """True when ``lane`` is allowed for delegated work under this contract."""
    norm = normalize_execution_lane_for_scenario(lane)
    if not contract.allowed_lanes:
        return False
    allowed = {normalize_execution_lane_for_scenario(x) for x in contract.allowed_lanes}
    return norm in allowed


def scenario_lane_mismatch_message(contract: ScenarioContract, lane: str) -> str:
    label = contract.user_facing_label or contract.scenario_id
    norm = normalize_execution_lane_for_scenario(lane) or str(lane or "").strip() or "unknown"
    if contract.allowed_lanes:
        allowed_txt = ", ".join(sorted({normalize_execution_lane_for_scenario(x) for x in contract.allowed_lanes}))
    else:
        allowed_txt = "none (this scenario does not support delegated execution)"
    return (
        f"This scenario (**{label}**) can’t run on execution lane `{norm}`. "
        f"Allowed lanes: {allowed_txt}. "
        "Try a different routing hint or rephrase as a supported task."
    )


def stored_plan_kind_for_delegate_contract(contract: ScenarioContract) -> str:
    """Persist semantic plan kind from the scenario contract (not always delegated_repo_task)."""
    raw = str(contract.default_plan_kind or "").strip()
    if raw and raw != "none":
        return raw
    return "delegated_repo_task"


def proof_signals_satisfied_for_trusted_completion(
    *,
    verification_verdict: str,
    verification_method: str,
    pr_url: str,
    agent_url: str,
) -> bool:
    """
    Conservative proof bar for ``trusted_receipt_allowed`` on verification-sensitive scenarios.
    """
    verdict = str(verification_verdict or "").strip().lower()
    method = str(verification_method or "").strip().lower()
    pr = bool(str(pr_url or "").strip())
    ag = bool(str(agent_url or "").strip())
    if verdict == "pass":
        if method == "repo_checks":
            return pr
        if method == "human_confirm":
            return pr
        if method == "none":
            return True
        return pr or ag
    if verdict == "needs_human":
        return pr
    return False


def trusted_receipt_allowed(
    contract: ScenarioContract,
    *,
    verification_verdict: str,
    has_required_proof: bool,
) -> bool:
    """
    For verification-sensitive scenarios, block 'trusted complete' until proof policy satisfied.
    """
    if contract.scenario_id != "verificationSensitiveAction":
        return True
    if verification_verdict in {"pass", "needs_human"} and has_required_proof:
        return True
    if verification_verdict == "pass" and contract.proof_class == "human_confirm":
        return has_required_proof
    return False
