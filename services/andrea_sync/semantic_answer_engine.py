"""Semantic answer selection for bounded conversational/stateful turns."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Dict, Optional, Sequence

from .assistant_answer_composer import (
    CONTINUATION_NO_VIABLE_WORKSTREAM_FALLBACK,
    RECALL_NO_CLEAN_CURSOR_RECAP_FALLBACK,
    build_blocked_state_reply_from_state,
    build_recent_outcome_history_reply_from_state,
    cursor_followup_context_reply_with_fallback,
    derive_stateful_next_step_options,
)
from .stateful_answer_realization import maybe_realize_stateful_reply
from .semantic_continuity import user_message_suggests_anaphoric_cursor_continue
from .goal_runtime import build_goal_continuity_reply, try_goal_status_nl_reply
from .turn_intelligence import TurnPlan, build_turn_plan, resolve_answer_family_profile
from .user_surface import sanitize_user_surface_text

_TOOLING_IDENTITY_Q_RE = re.compile(
    r"^\s*(?:"
    r"is\s+this\s+openclaw|is\s+this\s+cursor|"
    r"what\s+is\s+openclaw|what\s+is\s+cursor|"
    r"are\s+you\s+openclaw|are\s+you\s+cursor"
    r")\s*\??\s*$",
    re.I,
)


@dataclass(frozen=True)
class TurnInterpretation:
    scenario_id: str
    domain: str
    continuity_focus: str
    prefer_state_reply: bool
    force_delegate: bool
    confidence: float = 0.85


@dataclass(frozen=True)
class SemanticAnswerResult:
    reply_text: str
    reason: str
    source: str
    interpretation: TurnInterpretation
    score: int
    family: str = "general_status"
    turn_contract: Dict[str, Any] | None = None

    def to_metadata(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "source": self.source,
            "score": self.score,
            "reason": self.reason,
            "scenario_id": self.interpretation.scenario_id,
            "domain": self.interpretation.domain,
            "continuity_focus": self.interpretation.continuity_focus,
            "prefer_state_reply": self.interpretation.prefer_state_reply,
            "force_delegate": self.interpretation.force_delegate,
            "confidence": self.interpretation.confidence,
            "answer_family": self.family,
        }
        if isinstance(self.turn_contract, dict) and self.turn_contract:
            out["turn_contract"] = dict(self.turn_contract)
        return out


@dataclass(frozen=True)
class SemanticTurnContract:
    family: str
    source: str
    allowed_sources: tuple[str, ...]
    required_anchors: tuple[str, ...]
    evidence_lines: tuple[str, ...]
    evidence_strength: int
    min_score: int
    fallback_policy: str
    binding_reason: str = ""
    answer_mode: str = "strong_evidence_answer"
    uncertainty_mode: str = "clear"
    next_step_options: tuple[str, ...] = ()

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "family": self.family,
            "source": self.source,
            "allowed_sources": list(self.allowed_sources),
            "required_anchors": list(self.required_anchors),
            "evidence_lines": list(self.evidence_lines),
            "evidence_strength": int(self.evidence_strength),
            "min_score": int(self.min_score),
            "fallback_policy": self.fallback_policy,
            "binding_reason": self.binding_reason,
            "answer_mode": self.answer_mode,
            "uncertainty_mode": self.uncertainty_mode,
            "next_step_options": list(self.next_step_options),
        }


def _looks_thin_cursor_recap(text: str) -> bool:
    low = str(text or "").strip().lower()
    if not low:
        return True
    return "not finding a strong stored summary" in low


def _looks_metadata_led(text: str) -> bool:
    low = str(text or "").strip().lower()
    if not low:
        return True
    if low.startswith("where things stand:"):
        return True
    lead_tokens = ("task status", "phase:", "result:", "delegated execution")
    return any(low.startswith(tok) for tok in lead_tokens)


def _narrative_richness(text: str) -> int:
    t = str(text or "").strip()
    if not t:
        return 0
    richness = 0
    if "\n" in t:
        richness += 4
    if "cursor recap:" in t.lower():
        richness += 6
    if "next step:" in t.lower():
        richness += 2
    richness += min(8, len(t) // 80)
    return richness


def _score_candidate(source: str, text: str) -> int:
    base = {
        "cursor_continuity_recall": 96,
        "cursor_heavy_lift_context": 92,
        "blocked_state_reply": 90,
        "goal_status": 82,
        "goal_continuity": 76,
    }.get(source, 40)
    score = base + _narrative_richness(text)
    if source == "cursor_continuity_recall" and _looks_thin_cursor_recap(text):
        score -= 38
    if _looks_metadata_led(text):
        score -= 12
    return score


def _split_structured_text_lines(text: str) -> list[str]:
    out: list[str] = []
    for raw in re.split(r"[\r\n]+", str(text or "")):
        clean = str(raw or "").strip().lstrip("-* ").strip()
        if clean:
            out.append(clean)
    return out


def _evidence_strength(lines: Sequence[str]) -> int:
    score = 0
    for ln in lines:
        txt = str(ln or "").strip()
        if not txt:
            continue
        score += 1
        if ":" in txt:
            score += 1
        if len(txt) > 48:
            score += 1
    return score


def _required_anchors_for(family: str, source: str, text: str) -> tuple[str, ...]:
    low = str(text or "").lower()
    anchors: list[str] = []
    if family == "approval_state":
        anchors.append("approval")
    elif family == "blocked_state":
        anchors.append("blocked")
    elif family == "cursor_recall":
        anchors.append("cursor")
        if "latest useful result:" in low:
            anchors.append("latest useful result")
    if source == "goal_status" and "pending approvals" in low and "approval" not in anchors:
        anchors.append("approval")
    seen: set[str] = set()
    out: list[str] = []
    for a in anchors:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return tuple(out)


def _contract_is_admissible(
    contract: SemanticTurnContract,
    *,
    candidate_text: str,
) -> bool:
    low = str(candidate_text or "").lower()
    if not low.strip():
        return False
    if contract.required_anchors and contract.evidence_strength >= 3:
        for anchor in contract.required_anchors:
            if anchor and anchor not in low:
                return False
    return True


def _classify_answer_mode(
    *,
    source: str,
    evidence_strength: int,
    candidate_text: str,
) -> tuple[str, str]:
    thin_cursor = source == "cursor_continuity_recall" and _looks_thin_cursor_recap(candidate_text)
    if evidence_strength >= 6 and not thin_cursor:
        return "strong_evidence_answer", "clear"
    if evidence_strength >= 2 or thin_cursor:
        return "partial_evidence_helpful_answer", "partial"
    return "truthful_fallback_with_next_steps", "thin"


def _build_turn_contract(
    *,
    family: str,
    allowed_sources: Sequence[str],
    source: str,
    candidate_text: str,
    min_score: int,
    binding_reason: str = "",
    conn: Any = None,
    task_id: str = "",
    user_text: str = "",
) -> SemanticTurnContract:
    evidence_lines = tuple(_split_structured_text_lines(candidate_text)[:8]) or (candidate_text,)
    ev_strength = _evidence_strength(evidence_lines)
    answer_mode, uncertainty_mode = _classify_answer_mode(
        source=source, evidence_strength=ev_strength, candidate_text=candidate_text
    )
    next_step_options: tuple[str, ...] = ()
    if conn is not None and str(task_id or "").strip():
        if answer_mode in {"partial_evidence_helpful_answer", "truthful_fallback_with_next_steps"}:
            next_step_options = derive_stateful_next_step_options(
                conn,
                str(task_id),
                source=str(source),
                user_text=str(user_text or ""),
            )
    fallback_policy = (
        "allow_truthful_fallback_when_evidence_thin"
        if source in {"cursor_continuity_recall", "cursor_heavy_lift_context"}
        else "prefer_grounded_specifics_then_truthful_fallback"
    )
    return SemanticTurnContract(
        family=family,
        source=source,
        allowed_sources=tuple(str(x) for x in allowed_sources),
        required_anchors=_required_anchors_for(family, source, candidate_text),
        evidence_lines=evidence_lines,
        evidence_strength=ev_strength,
        min_score=int(min_score),
        fallback_policy=fallback_policy,
        binding_reason=str(binding_reason or "").strip(),
        answer_mode=answer_mode,
        uncertainty_mode=uncertainty_mode,
        next_step_options=next_step_options,
    )


def choose_semantic_state_reply(
    conn: Any,
    task_id: str,
    *,
    user_text: str,
    turn_plan: TurnPlan,
    scenario_id: str,
    family_override: str = "",
    allowed_sources_override: Sequence[str] = (),
    stateful_allowed: bool | None = None,
    binding_reason: str = "",
) -> Optional[SemanticAnswerResult]:
    """
    Choose a direct state-backed answer for bounded conversational status turns.
    Returns None when semantic state selection should not override legacy routing.
    """
    interpretation = TurnInterpretation(
        scenario_id=str(scenario_id or ""),
        domain=str(turn_plan.domain or ""),
        continuity_focus=str(turn_plan.continuity_focus or ""),
        prefer_state_reply=bool(turn_plan.prefer_state_reply),
        force_delegate=bool(turn_plan.force_delegate),
    )
    if interpretation.force_delegate:
        return None
    if stateful_allowed is False:
        return None
    # Family integrity guard: if text itself classifies to a non-stateful domain,
    # abstain even when upstream context nudges a stateful turn_plan.
    text = str(user_text or "")
    raw_text_plan = build_turn_plan(
        text,
        scenario_id=str(scenario_id or ""),
        projection_has_continuity_state=False,
    )
    if raw_text_plan.domain not in {"project_status", "approval_state"}:
        return None
    if interpretation.domain not in {"project_status", "approval_state"}:
        return None
    if not bool(turn_plan.allow_goal_continuity_repair):
        return None

    if _TOOLING_IDENTITY_Q_RE.match(text.strip()):
        return None
    family = resolve_answer_family_profile(text, turn_plan)
    effective_family = str(family_override or "").strip() or family.family
    effective_allowed_sources = (
        tuple(str(x).strip() for x in allowed_sources_override if str(x).strip())
        or family.allowed_sources
    )
    candidates: Dict[str, str] = {}

    if interpretation.continuity_focus == "blocked_state":
        candidates["blocked_state_reply"] = build_blocked_state_reply_from_state(conn, task_id)
    elif interpretation.continuity_focus == "recent_outcome_history":
        candidates["cursor_continuity_recall"] = build_recent_outcome_history_reply_from_state(
            conn, task_id, user_message=text
        )
    elif interpretation.continuity_focus == "cursor_followup_heavy_lift":
        candidates["cursor_heavy_lift_context"] = cursor_followup_context_reply_with_fallback(
            conn, task_id, user_message=text
        )

    goal_status = try_goal_status_nl_reply(conn, task_id, text)
    if goal_status:
        candidates["goal_status"] = goal_status
    goal_continuity = build_goal_continuity_reply(conn, task_id, user_text=text)
    if goal_continuity:
        candidates["goal_continuity"] = goal_continuity

    if interpretation.continuity_focus == "cursor_followup_heavy_lift" and user_message_suggests_anaphoric_cursor_continue(
        text
    ):
        candidates = {k: v for k, v in candidates.items() if k == "cursor_heavy_lift_context"}
    if effective_allowed_sources:
        allowed = set(effective_allowed_sources)
        candidates = {k: v for k, v in candidates.items() if k in allowed}

    min_score = max(58, int(family.min_score))
    best: Optional[SemanticAnswerResult] = None
    for source, raw_text in candidates.items():
        cleaned = sanitize_user_surface_text(str(raw_text or "").strip(), limit=1200)
        if not cleaned:
            continue
        contract = _build_turn_contract(
            family=effective_family,
            allowed_sources=effective_allowed_sources,
            source=source,
            candidate_text=cleaned,
            min_score=min_score,
            binding_reason=binding_reason,
            conn=conn,
            task_id=task_id,
            user_text=text,
        )
        if not _contract_is_admissible(contract, candidate_text=cleaned):
            continue
        fallback = cleaned
        if source == "cursor_continuity_recall":
            fallback = RECALL_NO_CLEAN_CURSOR_RECAP_FALLBACK
        elif source == "cursor_heavy_lift_context":
            fallback = CONTINUATION_NO_VIABLE_WORKSTREAM_FALLBACK
        realized = maybe_realize_stateful_reply(
            conn,
            task_id,
            source=source,
            deterministic_reply=cleaned,
            fallback_reply=fallback,
            user_text=text,
            turn_plan=turn_plan,
            turn_contract=contract.to_metadata(),
        )
        candidate_text = sanitize_user_surface_text(
            str(realized or cleaned).strip(), fallback=cleaned, limit=1200
        )
        if not candidate_text:
            continue
        score = _score_candidate(source, candidate_text)
        if best is None or score > best.score:
            best = SemanticAnswerResult(
                reply_text=candidate_text,
                reason=f"semantic_state_{source}",
                source=source,
                interpretation=interpretation,
                score=score,
                family=effective_family,
                turn_contract=contract.to_metadata(),
            )
    if best is None:
        return None
    if best.score < min_score:
        return None
    return best
