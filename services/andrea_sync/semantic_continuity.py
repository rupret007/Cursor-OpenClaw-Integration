"""Semantic continuity hints: same-chat delegation signals and anaphoric follow-ups."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from .projector import project_task_dict
from .store import get_task_channel, list_telegram_task_ids_for_chat
from .turn_intelligence import ContinuityFocus

_ANAPHORIC_OUTCOME_RE = re.compile(
    r"\b("
    r"what\s+happened\s+there|"
    r"what\s+happened\s+with\s+that(?!\s+task\b)|"
    r"what\s+about\s+that\s+one|"
    r"recap\s+that\b|"
    r"what\s+was\s+the\s+result"
    r")\b",
    re.I,
)

_ANAPHORIC_CONTINUE_RE = re.compile(
    r"^\s*("
    r"continue\s+that(?:\s+(?:cursor\s+)?task)?|"
    r"continue\s+this(?:\s+(?:cursor\s+)?task)?|"
    r"pick\s+up\s+(?:that|this|there)|"
    r"keep\s+going\s+(?:on\s+)?that"
    r")\s*[?.!]*\s*$",
    re.I,
)


def user_message_suggests_anaphoric_outcome_recall(text: str) -> bool:
    """Short follow-ups that point at the last delegated outcome without naming Cursor."""
    return bool(_ANAPHORIC_OUTCOME_RE.search(str(text or "")))


def user_message_suggests_anaphoric_cursor_continue(text: str) -> bool:
    """Bare continuation phrasing that should bind to same-chat Cursor work when present."""
    return bool(_ANAPHORIC_CONTINUE_RE.match(str(text or "").strip()))


def _meta_for_task(conn: Any, task_id: str) -> dict[str, Any]:
    channel = get_task_channel(conn, task_id) or "cli"
    proj = project_task_dict(conn, task_id, channel)
    meta = proj.get("meta") if isinstance(proj.get("meta"), dict) else {}
    return meta if isinstance(meta, dict) else {}


def delegation_signal_score(meta: dict[str, Any]) -> int:
    """Heuristic strength of Cursor / OpenClaw delegation on a task projection."""
    score = 0
    oc = meta.get("openclaw") if isinstance(meta.get("openclaw"), dict) else {}
    if str(oc.get("user_summary") or "").strip():
        score += 55
    po = oc.get("phase_outputs")
    if isinstance(po, dict):
        for block in po.values():
            if isinstance(block, dict) and str(block.get("summary") or "").strip():
                score += 28
                break
    ct = oc.get("collaboration_trace")
    if isinstance(ct, list) and any(str(x).strip() for x in ct[:8]):
        score += 22

    ex = meta.get("execution") if isinstance(meta.get("execution"), dict) else {}
    if ex.get("delegated_to_cursor"):
        score += 50

    cur = meta.get("cursor") if isinstance(meta.get("cursor"), dict) else {}
    if str(cur.get("cursor_agent_id") or cur.get("agent_id") or "").strip():
        score += 35

    outcome = meta.get("outcome") if isinstance(meta.get("outcome"), dict) else {}
    if str(outcome.get("current_phase_summary") or "").strip():
        score += 25

    asst = meta.get("assistant") if isinstance(meta.get("assistant"), dict) else {}
    reason = str(asst.get("reason") or "").lower()
    if any(k in reason for k in ("cursor", "openclaw", "delegat", "handoff")):
        score += 18

    tg = meta.get("telegram") if isinstance(meta.get("telegram"), dict) else {}
    cap = str(tg.get("requested_capability") or "").lower()
    if "cursor" in cap or "openclaw" in cap:
        score += 15
    cr = tg.get("continuation_records") if isinstance(tg.get("continuation_records"), list) else []
    if cr:
        score += 12

    return score


def same_chat_max_delegation_score(conn: Any, task_id: str) -> int:
    """Best delegation signal across the current task and recent same-chat Telegram tasks."""
    meta = _meta_for_task(conn, task_id)
    tg = meta.get("telegram") if isinstance(meta.get("telegram"), dict) else {}
    chat_id = tg.get("chat_id")
    best = delegation_signal_score(meta)
    if chat_id is None or str(chat_id).strip() == "":
        return best
    for tid in list_telegram_task_ids_for_chat(conn, chat_id, limit=24):
        m = _meta_for_task(conn, tid)
        s = delegation_signal_score(m)
        if s > best:
            best = s
    return best


@dataclass(frozen=True)
class SemanticContinuityPatch:
    """Overrides applied after build_turn_plan for routing / goal-NL continuity."""

    continuity_focus_override: Optional[ContinuityFocus] = None
    force_prefer_state_reply: bool = False


_STATUS_LIKE_SCENARIOS = frozenset(
    {"statusFollowupContinue", "goalContinuationAcrossSessions"}
)


def resolve_semantic_continuity_patch(
    conn: Any,
    task_id: str,
    user_text: str,
    *,
    scenario_id: str,
    base_focus: ContinuityFocus,
    projection_has_continuity_state: bool,
) -> SemanticContinuityPatch:
    """
    When the utterance is anaphoric or continuation-shaped but regex classification
    stayed on ``none``, upgrade continuity_focus using same-chat delegation signals.
    """
    clean = str(user_text or "").strip()
    sid = str(scenario_id or "").strip()
    if sid not in _STATUS_LIKE_SCENARIOS or base_focus != "none" or not clean:
        return SemanticContinuityPatch()

    del_score = same_chat_max_delegation_score(conn, task_id)
    has_projection_continuity = bool(projection_has_continuity_state)
    has_ctx = has_projection_continuity or del_score >= 38

    if user_message_suggests_anaphoric_outcome_recall(clean) and has_ctx:
        return SemanticContinuityPatch(
            continuity_focus_override="recent_outcome_history",
            force_prefer_state_reply=True,
        )
    if user_message_suggests_anaphoric_cursor_continue(clean) and del_score >= 32:
        return SemanticContinuityPatch(
            continuity_focus_override="cursor_followup_heavy_lift",
            force_prefer_state_reply=True,
        )
    return SemanticContinuityPatch()
