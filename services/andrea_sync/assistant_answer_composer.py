"""State-aware composition for direct assistant replies (ranked candidates from durable state)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .andrea_router import (
    DIRECT_AGENDA_NO_CALENDAR_REPLY,
    DIRECT_ATTENTION_NO_STATE_REPLY,
    _contextual_fallback,
    _heuristic_reply,
    _history_hint,
    is_generic_direct_reply,
)
from .goal_runtime import _APPROVAL_STATUS_PATTERNS, build_goal_continuity_reply
from .projector import project_task_dict
from .store import (
    get_task_channel,
    get_task_principal_id,
    list_recent_closure_decisions_for_task,
    list_recent_followup_recommendations_for_task,
    list_recent_open_loop_records_for_task,
    list_recent_stale_task_indicators_for_task,
    list_recent_user_outcome_receipts_for_task,
    list_upcoming_reminders_for_principal,
)
from .turn_intelligence import TurnPlan

_STATUS_SCENARIOS = frozenset({"statusFollowupContinue", "goalContinuationAcrossSessions"})

_FALSE_COMPLETION_RE = re.compile(
    r"\b("
    r"all\s+(set|done|clear)|nothing\s+pending|no\s+pending|"
    r"fully\s+complete|completed\s+successfully|"
    r"you(?:'re|\s+are)\s+all\s+caught\s+up|we(?:'re|\s+are)\s+done|"
    r"nothing\s+left\s+to\s+do|no\s+open\s+(items?|loops?)"
    r")\b",
    re.I,
)

_FT_PENDING_STATES = frozenset(
    {
        "pending",
        "open",
        "awaiting_user",
        "awaiting-user",
        "needs_user",
        "needs-user",
        "blocked_on_user",
        "blocked-on-user",
    }
)


@dataclass(frozen=True)
class AnswerCandidate:
    source: str
    text: str
    priority: int


def _projection_meta(conn: Any, task_id: str) -> Dict[str, Any]:
    channel = get_task_channel(conn, task_id) or "cli"
    projection = project_task_dict(conn, task_id, channel)
    meta = projection.get("meta") if isinstance(projection.get("meta"), dict) else {}
    return meta


def _followthrough_section(meta: Dict[str, Any]) -> Dict[str, Any]:
    ft = meta.get("followthrough")
    return ft if isinstance(ft, dict) else {}


def _daily_pack_section(meta: Dict[str, Any]) -> Dict[str, Any]:
    dp = meta.get("daily_assistant_pack")
    return dp if isinstance(dp, dict) else {}


def followthrough_needs_user_attention(followthrough: Dict[str, Any]) -> bool:
    if not followthrough:
        return False
    cstate = str(followthrough.get("last_closure_state") or "").strip().lower()
    if cstate in _FT_PENDING_STATES:
        return True
    ostate = str(followthrough.get("last_open_loop_state") or "").strip().lower()
    if ostate in _FT_PENDING_STATES:
        return True
    reason = str(followthrough.get("last_closure_reason") or "").lower()
    if any(
        tok in reason
        for tok in (
            "awaiting",
            "pending",
            "needs user",
            "user input",
            "confirmation",
            "open loop",
        )
    ):
        return True
    return False


def draft_implies_false_completion(text: str) -> bool:
    return bool(_FALSE_COMPLETION_RE.search(str(text or "")))


def followthrough_corrective_lead(
    followthrough: Dict[str, Any],
    existing_reply: str,
) -> Optional[str]:
    """
    When follow-through says work still needs the user but the draft sounds "all clear",
    return a short corrective lead line (caller may prepend and/or rebuild body).
    """
    if not followthrough_needs_user_attention(followthrough):
        return None
    if not draft_implies_false_completion(existing_reply) and not is_generic_direct_reply(
        existing_reply
    ):
        return None
    reason = str(followthrough.get("last_closure_reason") or "").strip()
    if reason:
        return f"Follow-through still shows an open item: {reason}"
    return (
        "Follow-through still shows something awaiting your input or confirmation — "
        "here is the current tracked state."
    )


def _outcome_section(meta: Dict[str, Any]) -> Dict[str, Any]:
    o = meta.get("outcome")
    return o if isinstance(o, dict) else {}


def _proactive_section(meta: Dict[str, Any]) -> Dict[str, Any]:
    p = meta.get("proactive")
    return p if isinstance(p, dict) else {}


def _telegram_section(meta: Dict[str, Any]) -> Dict[str, Any]:
    t = meta.get("telegram")
    return t if isinstance(t, dict) else {}


def collect_task_state_snippets(conn: Any, task_id: str, *, limit_each: int = 3) -> List[str]:
    """Short lines for enriching project/approval replies from durable task state."""
    lines: List[str] = []
    for row in list_recent_user_outcome_receipts_for_task(
        conn, task_id, limit=limit_each
    ):
        summ = str(row["summary"] or "").strip()[:220]
        kind = str(row["receipt_kind"] or "").strip()
        if summ:
            lines.append(
                f"Recent receipt ({kind}): {summ}" if kind else f"Recent receipt: {summ}"
            )
    for row in list_recent_open_loop_records_for_task(conn, task_id, limit=limit_each):
        lk = str(row["loop_kind"] or "").strip()
        st = str(row["open_loop_state"] or "").strip()
        ore = str(row["opened_reason"] or "").strip()[:140]
        if lk or st or ore:
            lines.append(
                "Open loop"
                + (f" ({lk})" if lk else "")
                + ": "
                + (st or ore or "open item")
            )
    for row in list_recent_stale_task_indicators_for_task(conn, task_id, limit=2):
        sk = str(row["staleness_kind"] or "").strip()
        rsn = str(row["reason"] or "").strip()[:160]
        if sk or rsn:
            lines.append(
                f"At-risk / stale signal: {sk or 'task'}{(' — ' + rsn) if rsn else ''}"
            )
    meta = _projection_meta(conn, task_id)
    tm = _telegram_section(meta)
    cr = tm.get("continuation_records") if isinstance(tm.get("continuation_records"), list) else []
    if cr:
        last = cr[-1]
        if isinstance(last, dict):
            r = str(last.get("reason") or "").strip()[:200]
            if r:
                lines.append(f"Continuation context: {r}")
    return lines[:12]


def _build_personal_runtime_reply(
    conn: Any,
    task_id: str,
    *,
    attention_first: bool,
) -> str:
    """
    Honest agenda or attention-style answer from reminders, receipts, follow-through,
    ledger rows, and projection meta. Does not substitute goal continuity for calendar data.
    """
    meta = _projection_meta(conn, task_id)
    principal_id = get_task_principal_id(conn, task_id) or ""
    lines: List[str] = []

    reminders = (
        list_upcoming_reminders_for_principal(conn, principal_id, limit=8)
        if principal_id
        else []
    )
    ledger_receipts = list_recent_user_outcome_receipts_for_task(conn, task_id, limit=5)
    loops = list_recent_open_loop_records_for_task(conn, task_id, limit=4)
    stales = list_recent_stale_task_indicators_for_task(conn, task_id, limit=3)
    closures = list_recent_closure_decisions_for_task(conn, task_id, limit=4)
    followups = list_recent_followup_recommendations_for_task(conn, task_id, limit=3)

    ft = _followthrough_section(meta)
    dp = _daily_pack_section(meta)
    outcome = _outcome_section(meta)
    proactive = _proactive_section(meta)
    tm = _telegram_section(meta)
    cont_records = (
        tm.get("continuation_records") if isinstance(tm.get("continuation_records"), list) else []
    )

    def ft_block() -> None:
        fk = str(ft.get("last_loop_kind") or "").strip()
        fos = str(ft.get("last_open_loop_state") or "").strip()
        fr = str(ft.get("last_closure_reason") or "").strip()
        if followthrough_needs_user_attention(ft) or fk or fos or fr:
            if fr:
                lines.append(f"Follow-through: {fr[:240]}")
            elif fk or fos:
                lines.append(
                    "Follow-through: "
                    + ", ".join(x for x in (fk, fos) if x)
                )

    def pack_meta() -> None:
        summary = str(dp.get("last_receipt_summary") or "").strip()
        if summary:
            lines.append(f"Latest assistant receipt snapshot: {summary}")

    def reminder_block() -> None:
        if reminders:
            lines.append("Upcoming reminders I have on file:")
            for row in reminders[:8]:
                msg = str(row.get("message") or "").strip()
                due = float(row.get("due_at") or 0.0)
                if msg:
                    lines.append(f"• {msg} (due_at={due:.0f})")

    def loop_block() -> None:
        for row in loops:
            lk = str(row["loop_kind"] or "").strip()
            st = str(row["open_loop_state"] or "").strip()
            ore = str(row["opened_reason"] or "").strip()[:140]
            if lk or st or ore:
                lines.append(
                    f"Open loop ({lk or 'item'}): {st or ore or 'see task'}"
                )

    def stale_block() -> None:
        for row in stales:
            sk = str(row["staleness_kind"] or "").strip()
            rsn = str(row["reason"] or "").strip()[:160]
            if sk or rsn:
                lines.append(
                    f"Stale / at-risk: {sk or 'signal'}{(' — ' + rsn) if rsn else ''}"
                )

    def closure_block() -> None:
        for row in closures:
            cs = str(row["closure_state"] or "").strip()
            rsn = str(row["reason"] or "").strip()[:160]
            if cs == "needs_repair" or (cs and rsn):
                lines.append(f"Closure ({cs}): {rsn or cs}")

    def followup_block() -> None:
        for row in followups:
            act = str(row["recommended_action"] or "").strip()[:200]
            why = str(row["why_now"] or "").strip()[:160]
            if act or why:
                lines.append(f"Follow-up suggestion: {act or why}".strip())
                break

    def ledger_block() -> None:
        for row in ledger_receipts[:5]:
            summ = str(row["summary"] or "").strip()[:200]
            kind = str(row["receipt_kind"] or "").strip()
            if summ:
                lines.append(
                    f"Recent receipt ({kind}): {summ}" if kind else f"Recent receipt: {summ}"
                )

    def continuation_block() -> None:
        if cont_records:
            last = cont_records[-1]
            if isinstance(last, dict):
                r = str(last.get("reason") or "").strip()[:240]
                if r:
                    lines.append(f"Latest continuation note: {r}")

    def outcome_block() -> None:
        ph = str(outcome.get("current_phase_summary") or "").strip()[:280]
        if ph:
            lines.append(f"Current phase: {ph}")
        uxf = outcome.get("ux_flags") if isinstance(outcome.get("ux_flags"), list) else []
        if "proactive_delivery_failed" in uxf:
            lines.append("Note: a recent proactive delivery may have failed.")

    def proactive_block() -> None:
        pr = int(proactive.get("pending_reminder_count") or 0)
        if pr > 0:
            lines.append(f"Pending reminders in queue: {pr}")

    if attention_first:
        ft_block()
        loop_block()
        stale_block()
        closure_block()
        followup_block()
        reminder_block()
        pack_meta()
        ledger_block()
        continuation_block()
        outcome_block()
        proactive_block()
    else:
        reminder_block()
        pack_meta()
        ledger_block()
        ft_block()
        loop_block()
        stale_block()
        closure_block()
        continuation_block()
        outcome_block()

    if lines:
        lines.append(
            "I do not have a full calendar view here—this is what I can ground on from "
            "reminders and assistant state."
        )
        return "\n".join(lines)
    if attention_first:
        return DIRECT_ATTENTION_NO_STATE_REPLY
    return DIRECT_AGENDA_NO_CALENDAR_REPLY


def build_agenda_reply_from_state(conn: Any, task_id: str) -> str:
    return _build_personal_runtime_reply(conn, task_id, attention_first=False)


def build_attention_reply_from_state(conn: Any, task_id: str) -> str:
    return _build_personal_runtime_reply(conn, task_id, attention_first=True)


def merge_goal_reply_with_followthrough(
    conn: Any,
    task_id: str,
    user_text: str,
    base_reply: str,
) -> str:
    """Prepend follow-through correction when a goal/status line would contradict ledger state."""
    if not str(base_reply or "").strip():
        return base_reply
    meta = _projection_meta(conn, task_id)
    ft = _followthrough_section(meta)
    lead = followthrough_corrective_lead(ft, base_reply)
    if not lead:
        return base_reply
    return f"{lead}\n\n{base_reply}"


def try_composer_early_short_circuit(
    conn: Any,
    task_id: str,
    user_text: str,
    turn_plan: TurnPlan,
    scenario_id: str,
    history: Sequence[Dict[str, str]] | None,
    memory_notes: List[str] | None,
) -> Optional[Tuple[str, str]]:
    """
    Deterministic direct replies that should win before the model (first slice domains).
    Returns (reply_text, reason) or None.
    """
    sid = str(scenario_id or "").strip()
    if sid not in _STATUS_SCENARIOS:
        return None
    domain = turn_plan.domain
    hist = list(history or [])
    mem = list(memory_notes or [])

    if domain == "personal_agenda":
        text = build_agenda_reply_from_state(conn, task_id)
        return text, "composer_personal_agenda_state"

    if domain == "attention_today":
        text = build_attention_reply_from_state(conn, task_id)
        return text, "composer_attention_today_state"

    if domain == "external_information":
        return (
            _heuristic_reply(str(user_text or ""), history=hist),
            "composer_external_information",
        )

    if domain == "opinion_reflection":
        hint = _history_hint(hist)
        if hint and len(hint) > 12:
            return (
                _contextual_fallback(str(user_text or ""), history=hist, memory_notes=mem),
                "composer_opinion_thread",
            )
        return None

    return None


def _model_priority(reply_text: str) -> int:
    if is_generic_direct_reply(reply_text):
        return 12
    return 48


def gather_repair_candidates(
    conn: Any,
    task_id: str,
    *,
    classify_text: str,
    turn_plan: TurnPlan,
    model_reply: str,
    history: Sequence[Dict[str, str]] | None,
    memory_notes: List[str] | None,
) -> List[AnswerCandidate]:
    """Assemble ranked candidates for a single bounded repair pass."""
    domain = str(turn_plan.domain or "")
    hist = list(history or [])
    mem = list(memory_notes or []) if turn_plan.inject_durable_memory else []
    meta = _projection_meta(conn, task_id)
    ft = _followthrough_section(meta)
    out: List[AnswerCandidate] = []

    out.append(
        AnswerCandidate(
            source="model",
            text=str(model_reply or "").strip(),
            priority=_model_priority(str(model_reply or "")),
        )
    )

    if domain == "external_information":
        out.append(
            AnswerCandidate(
                source="external_heuristic",
                text=_heuristic_reply(str(classify_text or ""), history=hist),
                priority=92,
            )
        )
        return out

    if domain == "opinion_reflection":
        out.append(
            AnswerCandidate(
                source="opinion_contextual",
                text=_contextual_fallback(
                    str(classify_text or ""), history=hist, memory_notes=mem
                ),
                priority=90,
            )
        )
        return out

    if domain == "personal_agenda":
        out.append(
            AnswerCandidate(
                source="agenda_state",
                text=build_agenda_reply_from_state(conn, task_id),
                priority=88,
            )
        )
        return out

    if domain == "attention_today":
        out.append(
            AnswerCandidate(
                source="attention_state",
                text=build_attention_reply_from_state(conn, task_id),
                priority=88,
            )
        )
        return out

    if domain in {"project_status", "approval_state"} and turn_plan.allow_goal_continuity_repair:
        goal = build_goal_continuity_reply(conn, task_id, user_text=str(classify_text or ""))
        snippets = collect_task_state_snippets(conn, task_id)
        lead = followthrough_corrective_lead(ft, str(model_reply or ""))
        if lead and goal:
            out.append(
                AnswerCandidate(
                    source="followthrough_goal",
                    text=f"{lead}\n\n{goal}",
                    priority=96,
                )
            )
        elif goal and snippets:
            merged = merge_goal_reply_with_followthrough(
                conn, task_id, str(classify_text or ""), goal
            )
            extra = "\n".join(f"• {s}" for s in snippets[:6])
            out.append(
                AnswerCandidate(
                    source="state_rich_goal",
                    text=f"{merged}\n\nAlso on file:\n{extra}",
                    priority=90,
                )
            )
        elif goal:
            merged = merge_goal_reply_with_followthrough(
                conn, task_id, str(classify_text or ""), goal
            )
            out.append(AnswerCandidate(source="goal_continuity", text=merged, priority=86))
        elif lead:
            out.append(
                AnswerCandidate(
                    source="followthrough_only",
                    text=lead,
                    priority=80,
                )
            )
        if domain == "project_status" and not goal:
            out.append(
                AnswerCandidate(
                    source="goal_continuity",
                    text=(
                        "I do not see active tracked work right now. "
                        "If you want, I can start a fresh task and track it from here."
                    ),
                    priority=72,
                )
            )
        elif domain == "approval_state" and not goal:
            if _APPROVAL_STATUS_PATTERNS.search(str(classify_text or "")):
                out.append(
                    AnswerCandidate(
                        source="goal_continuity",
                        text="There are no pending approvals right now.",
                        priority=72,
                    )
                )

    # De-dupe by text while keeping highest priority
    best: Dict[str, AnswerCandidate] = {}
    for c in out:
        t = c.text.strip()
        if not t:
            continue
        prev = best.get(t)
        if prev is None or c.priority > prev.priority:
            best[t] = c
    return sorted(best.values(), key=lambda x: x.priority, reverse=True)


def pick_repair_winner(
    candidates: Sequence[AnswerCandidate],
    *,
    model_reply: str,
    followthrough: Dict[str, Any],
    stateful_goal_ok: bool,
) -> Optional[Tuple[str, str]]:
    """
    Choose a non-model candidate when it is safe to override the draft direct reply.
    """
    if not candidates:
        return None
    model = str(model_reply or "").strip()
    ordered = sorted(candidates, key=lambda x: x.priority, reverse=True)
    winner = ordered[0]
    if winner.source == "model":
        return None
    if winner.text.strip() == model:
        return None
    generic_model = is_generic_direct_reply(model)

    if winner.source in {
        "external_heuristic",
        "opinion_contextual",
        "agenda_state",
        "attention_state",
    }:
        if generic_model or winner.priority >= 85:
            return winner.text, winner.source
        return None

    if winner.source == "followthrough_goal":
        return winner.text, winner.source

    if winner.source == "state_rich_goal":
        if generic_model or stateful_goal_ok:
            return winner.text, winner.source
        if draft_implies_false_completion(model) and followthrough_needs_user_attention(
            followthrough
        ):
            return winner.text, winner.source
        return None

    if winner.source in {"goal_continuity", "followthrough_only"}:
        if generic_model or stateful_goal_ok:
            return winner.text, winner.source
        if draft_implies_false_completion(model) and followthrough_needs_user_attention(
            followthrough
        ):
            return winner.text, winner.source
        return None

    return None


def bounded_composer_repair(
    conn: Any,
    task_id: str,
    *,
    classify_text: str,
    decision_reply: str,
    decision_reason: str,
    resolution: Any,
    turn_plan: TurnPlan | None,
    history: Sequence[Dict[str, str]] | None,
    memory_notes: List[str] | None,
    continuity_ask: bool,
    continuity_state: bool,
) -> Optional[Tuple[str, str]]:
    """
    Single bounded repair pass using ranked state candidates. Returns (text, source_tag)
    or None to keep the original reply.
    """
    if not turn_plan:
        return None
    domain = str(turn_plan.domain or "")
    reply_text = str(decision_reply or "").strip()
    if not reply_text:
        return None

    generic = is_generic_direct_reply(reply_text) or str(decision_reason or "") in {
        "short_general_request",
        "balanced_default_direct",
    }
    plan_repairs = bool(turn_plan.should_repair_generic)
    allow_goal = bool(turn_plan.allow_goal_continuity_repair)
    scenario_continuity = str(getattr(resolution, "scenario_id", "") or "") == (
        "statusFollowupContinue"
    )

    plan_prefers_state = bool(turn_plan.prefer_state_reply)
    stateful_goal_ok = (
        scenario_continuity
        or plan_prefers_state
        or continuity_ask
        or continuity_state
    )

    should_run_goal_branch = allow_goal and (
        scenario_continuity
        or plan_prefers_state
        or ((generic and plan_repairs) and (continuity_ask or continuity_state))
    )
    lane_domain = domain in {"personal_agenda", "attention_today"}
    should_run_lane_branch = (not allow_goal) and plan_repairs and (
        generic or (lane_domain and continuity_state)
    )

    if not should_run_goal_branch and not should_run_lane_branch:
        return None

    meta = _projection_meta(conn, task_id)
    ft = _followthrough_section(meta)

    cands = gather_repair_candidates(
        conn,
        task_id,
        classify_text=classify_text,
        turn_plan=turn_plan,
        model_reply=reply_text,
        history=history,
        memory_notes=memory_notes,
    )
    return pick_repair_winner(
        cands,
        model_reply=reply_text,
        followthrough=ft,
        stateful_goal_ok=stateful_goal_ok if should_run_goal_branch else True,
    )
