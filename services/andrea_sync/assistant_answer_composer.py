"""State-aware composition for direct assistant replies (ranked candidates from durable state)."""
from __future__ import annotations

import re
import time
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
from .delegated_lifecycle import build_delegated_lifecycle_contract
from .execution_runtime import summarize_execution_attempt_for_user
from .goal_runtime import _APPROVAL_STATUS_PATTERNS, build_goal_continuity_reply
from .projector import project_task_dict
from .semantic_continuity import user_message_suggests_anaphoric_cursor_continue
from .store import (
    get_active_execution_attempt_for_task,
    get_task_channel,
    get_task_principal_id,
    get_task_updated_at,
    list_pending_goal_approvals_for_task,
    list_recent_closure_decisions_for_task,
    list_recent_followup_recommendations_for_task,
    list_recent_open_loop_records_for_task,
    list_recent_stale_task_indicators_for_task,
    list_recent_user_outcome_receipts_for_task,
    list_telegram_task_ids_for_chat,
    list_upcoming_reminders_for_principal,
)
from .user_surface import (
    sanitize_user_surface_text,
    strip_conversational_soft_failure_boilerplate,
    surface_similarity_key,
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


def _assistant_section(meta: Dict[str, Any]) -> Dict[str, Any]:
    a = meta.get("assistant")
    return a if isinstance(a, dict) else {}


def _projection_full(conn: Any, task_id: str) -> Dict[str, Any]:
    channel = get_task_channel(conn, task_id) or "cli"
    return project_task_dict(conn, task_id, channel)


_CURSOR_THREAD_RECALL_RE = re.compile(
    r"\b("
    r"what\s+did\s+cursor\s+(?:say|do)|"
    r"what\s+happened\s+in\s+(?:the\s+)?cursor\s+thread|"
    r"what\s+happened\s+there|what\s+happened\s+with\s+that(?!\s+task\b)|"
    r"what\s+about\s+that\s+one|what\s+was\s+the\s+result|"
    r"what\s+did\s+openclaw\s+do|"
    r"what\s+did\s+it\s+do"
    r")\b",
    re.I,
)

_METADATA_SCAFFOLD_HINTS = (
    "task status",
    "result:",
    "result kind",
    "phase:",
    "where things stand",
    "current phase",
)


def draft_should_force_continuity_repair(draft: str, user_question: str) -> bool:
    """
    True when the direct reply is echoey or mostly bold metadata scaffolding so bounded
    repair should prefer conductor-style state candidates.
    """
    d = str(draft or "").strip()
    if not d:
        return False
    q = str(user_question or "").strip()
    if q and _is_echo_of_user_question(d, q):
        return True
    low = d.lower()
    bold_chunks = re.findall(r"\*\*[^*]+\*\*", d)
    if len(bold_chunks) >= 4:
        meta_hits = sum(1 for tok in _METADATA_SCAFFOLD_HINTS if tok in low)
        if meta_hits >= 3 and len(d.split()) < 120:
            return True
    return False

_GRACE_CURSOR_CONTINUITY = (
    "I'm not finding a strong stored summary from the recent Cursor work yet. "
    "I can check the latest tracked state or start a fresh heavy-lift pass from the last known context."
)
_GRACE_GENERIC_HISTORY = (
    "I don't have enough recorded history on the current task to say that confidently. "
    "I can still check the latest linked goal or start tracking the next step explicitly."
)

_AMBIGUOUS_CURSOR_CONTINUE_REPLY = (
    "I see more than one recent Cursor workstream on this thread that could fit. "
    "Which one should I continue—the newest run, or the earlier task?"
)

_EXPLICIT_STATUS_ASK_RE = re.compile(
    r"\b("
    r"where\s+are\s+we|status|progress|update|what'?s\s+the\s+state|"
    r"blocked|blocker|stuck|where\s+things\s+stand"
    r")\b",
    re.I,
)


def is_cursor_thread_recall_question(text: str) -> bool:
    """True for Cursor/OpenClaw thread recall phrasing (used for graceful copy)."""
    return bool(_CURSOR_THREAD_RECALL_RE.search(str(text or "")))


def _normalize_echo_key(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _strip_answer_wrappers(text: str) -> str:
    """Strip common lead-in prefixes so echo detection compares the actual content."""
    t = str(text or "").strip()
    low = t.lower()
    for prefix in (
        "recorded summary:",
        "last assistant update on this task:",
        "last assistant update:",
    ):
        if low.startswith(prefix):
            return t.split(":", 1)[-1].strip()
    return t


def _is_echo_of_user_question(assistant_text: str, user_message: str) -> bool:
    u_raw = str(user_message or "").strip()
    a_raw = _strip_answer_wrappers(assistant_text)
    if not u_raw or not a_raw:
        return False
    if surface_similarity_key(u_raw) == surface_similarity_key(a_raw):
        return True
    u = _normalize_echo_key(u_raw)
    a = _normalize_echo_key(a_raw)
    if len(u) >= 6 and len(a) >= 6:
        return a == u or (u in a and len(a) - len(u) < 20)
    return False


def _telegram_chat_id_for_task(conn: Any, task_id: str) -> Any:
    meta = _projection_meta(conn, task_id)
    tg = _telegram_section(meta)
    return tg.get("chat_id")


def _task_has_delegated_continuity_signal(meta: Dict[str, Any]) -> bool:
    oc = meta.get("openclaw") if isinstance(meta.get("openclaw"), dict) else {}
    ex = meta.get("execution") if isinstance(meta.get("execution"), dict) else {}
    cur = meta.get("cursor") if isinstance(meta.get("cursor"), dict) else {}
    outcome = meta.get("outcome") if isinstance(meta.get("outcome"), dict) else {}
    if str(oc.get("user_summary") or "").strip():
        return True
    po = oc.get("phase_outputs")
    if isinstance(po, dict):
        for v in po.values():
            if isinstance(v, dict) and str(v.get("summary") or "").strip():
                return True
    ct = oc.get("collaboration_trace")
    if isinstance(ct, list) and any(str(x).strip() for x in ct[:8]):
        return True
    if ex.get("delegated_to_cursor"):
        return True
    if str(cur.get("cursor_agent_id") or cur.get("agent_id") or "").strip():
        return True
    if str(outcome.get("current_phase_summary") or "").strip():
        return True
    return False


def _ordered_cursor_chat_candidates(
    conn: Any,
    current_task_id: str,
    chat_id: Any,
    *,
    limit: int = 18,
) -> List[str]:
    """Current task first (tie-break), then same-chat tasks by recency."""
    if chat_id is None or str(chat_id).strip() == "":
        return [current_task_id]
    ordered: List[str] = []
    seen: set[str] = set()
    if current_task_id not in seen:
        ordered.append(current_task_id)
        seen.add(current_task_id)
    for tid in list_telegram_task_ids_for_chat(conn, chat_id, limit=limit):
        if tid in seen:
            continue
        seen.add(tid)
        ordered.append(tid)
    return ordered


def _user_message_asks_explicit_status(user_message: str) -> bool:
    return bool(_EXPLICIT_STATUS_ASK_RE.search(str(user_message or "")))


def _cursor_recall_rank_adjustment(conn: Any, task_id: str) -> int:
    """Recency + active-attempt boost for same-chat Cursor ranking (complements base recall score)."""
    boost = 0
    if get_active_execution_attempt_for_task(conn, task_id):
        boost += 320
    ts = get_task_updated_at(conn, task_id)
    if ts is not None:
        age = max(0.0, time.time() - float(ts))
        boost += int(max(0, 220 - min(age / 3600.0, 120.0) * 1.85))
    return boost


def _continuation_candidate_score(conn: Any, task_id: str, user_message: str) -> Tuple[int, bool]:
    base, meaningful = _score_cursor_recall_candidate(conn, task_id, user_message)
    return base + _cursor_recall_rank_adjustment(conn, task_id), meaningful


def _cursor_recall_composition_is_metadata_led(text: str) -> bool:
    """True when recall-shaped output leads with execution scaffolding instead of human recap."""
    t = str(text or "").strip()
    if not t:
        return False
    low = t.lower()
    recap_markers = (
        "latest useful result:",
        "recent receipt (",
        "recent receipt:",
        "phase synthesis:",
        "phase execution:",
        "phase critique:",
        "phase plan:",
        "collaboration note:",
        "continuation context:",
        "last assistant update on this task:",
        "recorded summary:",
    )
    first_recap = min((low.find(m) for m in recap_markers if low.find(m) >= 0), default=10**9)
    idx_where = low.find("where things stand:")
    if idx_where >= 0 and first_recap < idx_where:
        return False

    first_line = low.split("\n")[0][:220]
    if "delegated execution (tracked)" in first_line:
        return True
    if idx_where >= 0 and idx_where <= 320 and first_recap == 10**9:
        return True
    return False


def _score_cursor_recall_candidate(
    conn: Any,
    task_id: str,
    user_message: str,
) -> Tuple[int, bool]:
    """
    Narrative-first score for ranking Cursor recall / delegated selection.
    Second return is True when there is durable human-readable signal beyond thin metadata.
    """
    channel = get_task_channel(conn, task_id) or "cli"
    proj = project_task_dict(conn, task_id, channel)
    meta = proj.get("meta") if isinstance(proj.get("meta"), dict) else {}
    um = str(user_message or "").strip()

    score = 0
    narrative_units = 0

    oc = meta.get("openclaw") if isinstance(meta.get("openclaw"), dict) else {}
    us = str(oc.get("user_summary") or "").strip()
    if us:
        narrative_units += 1
        score += 520
        if _is_echo_of_user_question(us, um):
            score -= 480

    po = oc.get("phase_outputs")
    if isinstance(po, dict):
        for phase in ("synthesis", "execution", "critique", "plan"):
            block = po.get(phase)
            if not isinstance(block, dict):
                continue
            s = str(block.get("summary") or "").strip()
            if not s:
                continue
            narrative_units += 1
            score += 200
            if _is_echo_of_user_question(s, um):
                score -= 170

    ct = oc.get("collaboration_trace")
    if isinstance(ct, list) and any(str(x).strip() for x in ct[:8]):
        narrative_units += 1
        score += 140

    for row in list_recent_user_outcome_receipts_for_task(conn, task_id, limit=3):
        try:
            s = str(row["summary"] or "").strip()
        except (KeyError, TypeError, IndexError):
            s = ""
        if s:
            narrative_units += 1
            score += 150
            if _is_echo_of_user_question(s, um):
                score -= 120

    tm = _telegram_section(meta)
    cr = tm.get("continuation_records") if isinstance(tm.get("continuation_records"), list) else []
    if cr:
        last = cr[-1]
        if isinstance(last, dict) and str(last.get("reason") or "").strip():
            narrative_units += 1
            score += 170

    outcome = _outcome_section(meta)
    ps = str(outcome.get("current_phase_summary") or "").strip()
    if ps:
        score += 210
        narrative_units += 1
        if _is_echo_of_user_question(ps, um):
            score -= 190

    br = str(outcome.get("blocked_reason") or "").strip()
    if br and len(br) > 6:
        score += 95

    ex = meta.get("execution") if isinstance(meta.get("execution"), dict) else {}
    if ex.get("delegated_to_cursor"):
        score += 230

    cur = meta.get("cursor") if isinstance(meta.get("cursor"), dict) else {}
    if str(cur.get("cursor_agent_id") or cur.get("agent_id") or "").strip():
        score += 85

    att = summarize_execution_attempt_for_user(conn, task_id)
    if isinstance(att, dict) and att.get("ok"):
        score += 130

    asst = _assistant_section(meta)
    lr = str(asst.get("last_reply") or "").strip()
    if lr and len(lr) > 12 and not _is_echo_of_user_question(lr, um):
        narrative_units += 1
        score += 125

    summ = str(proj.get("summary") or "").strip()
    if summ and len(summ) > 12 and not _is_echo_of_user_question(summ, um):
        narrative_units += 1
        score += 95

    has_meaningful = narrative_units >= 1 and score >= 120
    if not _task_has_delegated_continuity_signal(meta) and narrative_units == 0:
        return 0, False

    return score, has_meaningful


def select_best_task_for_cursor_recall(
    conn: Any,
    current_task_id: str,
    user_message: str,
) -> str:
    """Pick the strongest same-chat delegated Cursor task for recall-style answers."""
    cid = _telegram_chat_id_for_task(conn, current_task_id)
    cands = _ordered_cursor_chat_candidates(conn, current_task_id, cid)
    rows: List[Tuple[int, bool, int, str]] = []
    for idx, tid in enumerate(cands):
        base, meaningful = _score_cursor_recall_candidate(conn, tid, user_message)
        total = base + _cursor_recall_rank_adjustment(conn, tid)
        rows.append((total, meaningful, idx, tid))
    if not rows:
        return current_task_id
    rows.sort(key=lambda r: (-r[0], 0 if r[1] else 1, r[2]))
    best_total, best_meaningful, _, best_id = rows[0]
    if not best_meaningful:
        for total, meaningful, idx, tid in rows[1:]:
            if meaningful and (best_total - total) <= 200:
                return tid
    return best_id


def find_recent_delegated_cursor_task_id(
    conn: Any,
    current_task_id: str,
    *,
    chat_id: Any,
    limit: int = 18,
    user_message: str = "",
    continuation_boost: bool = True,
) -> Optional[str]:
    """
    Same-chat Telegram task (excluding current) with delegated Cursor/OpenClaw continuity,
    ranked by narrative strength and optional active-attempt boost.
    """
    if chat_id is None or str(chat_id).strip() == "":
        return None
    best_tid: Optional[str] = None
    best_score = -10**9
    best_idx = 10**9
    for idx, tid in enumerate(list_telegram_task_ids_for_chat(conn, chat_id, limit=limit)):
        if tid == current_task_id:
            continue
        meta = _projection_meta(conn, tid)
        s, meaningful = _score_cursor_recall_candidate(conn, tid, user_message)
        if continuation_boost:
            s += _cursor_recall_rank_adjustment(conn, tid)
        hl = build_cursor_heavy_lift_context_reply(conn, tid)
        if hl is None and s < 100 and not meaningful:
            continue
        if not _task_has_delegated_continuity_signal(meta) and not meaningful:
            continue
        if s > best_score or (s == best_score and idx < best_idx):
            best_score = s
            best_idx = idx
            best_tid = tid
    if best_tid is None:
        return None
    if best_score < 55:
        return None
    return best_tid


def _openclaw_narrative_lines(meta: Dict[str, Any]) -> List[str]:
    oc = meta.get("openclaw") if isinstance(meta.get("openclaw"), dict) else {}
    lines: List[str] = []
    us = sanitize_user_surface_text(str(oc.get("user_summary") or ""), limit=900)
    if us:
        lines.append(f"Latest useful result: {us}")
    po = oc.get("phase_outputs")
    if isinstance(po, dict):
        for phase in ("synthesis", "execution", "critique", "plan"):
            block = po.get(phase)
            if not isinstance(block, dict):
                continue
            summ = sanitize_user_surface_text(str(block.get("summary") or ""), limit=360)
            if summ:
                lines.append(f"Phase {phase}: {summ}")
                if len(lines) >= 4:
                    break
    tr = oc.get("collaboration_trace")
    if isinstance(tr, list):
        for raw in tr[:3]:
            t = sanitize_user_surface_text(str(raw), limit=240)
            if t:
                lines.append(f"Collaboration note: {t}")
    br = str(oc.get("blocked_reason") or "").strip()
    if br:
        safe_br = sanitize_user_surface_text(br, limit=280)
        if safe_br:
            lines.append(f"Delegated wait state: {safe_br}")
    return lines


def _ledger_receipt_summary_is_generic_placeholder(summary: str) -> bool:
    """True for daily-pack ledger rows that repeat route labels instead of substance."""
    s = str(summary or "").strip().lower()
    if not s:
        return False
    if "status / follow-up reply" in s:
        return True
    if "goal continuation summary" in s:
        return True
    if "recent messages / lookup" in s:
        return True
    if s.startswith("assistant outcome (") and "reply" in s:
        return True
    if "note or reminder path" in s:
        return True
    return False


def _execution_recall_context_lines(conn: Any, task_id: str) -> List[str]:
    """User-safe one-liner from active execution attempt when OpenClaw narrative is thin."""
    att = summarize_execution_attempt_for_user(conn, task_id)
    if not isinstance(att, dict) or not att.get("ok"):
        return []
    st = str(att.get("status") or "").strip()
    lane = str(att.get("lane") or "").strip()
    backend = str(att.get("backend") or "").strip()
    aid = str(att.get("cursor_agent_id") or "").strip()
    if not st and not lane and not backend and not aid:
        return []
    parts: List[str] = []
    if st and st.lower() not in {"unknown", "n/a", "none", ""}:
        parts.append(f"status **{st}**")
    if lane:
        parts.append(f"lane `{lane}`")
    if backend:
        parts.append(f"backend `{backend}`")
    if aid:
        parts.append("Cursor agent attached")
    if not parts:
        return []
    return ["Delegated execution (tracked): " + ", ".join(parts) + "."]


def _strip_recall_label(text: str) -> str:
    t = str(text or "").strip()
    if not t:
        return ""
    low = t.lower()
    for prefix in (
        "latest useful result:",
        "last assistant update on this task:",
        "recorded summary:",
        "recent receipt:",
        "continuation context:",
    ):
        if low.startswith(prefix):
            return t.split(":", 1)[-1].strip()
    return t


def _pick_cursor_recap_lead(
    *,
    narrative_lines: Sequence[str],
    assistant_lines: Sequence[str],
    receipt_substantive: Sequence[str],
    cont_line: str,
) -> str:
    """One concise recap sentence that can safely lead recall-shaped answers."""
    for line in narrative_lines:
        clean = sanitize_user_surface_text(_strip_recall_label(line), limit=360)
        if clean:
            return clean
    for line in assistant_lines:
        clean = sanitize_user_surface_text(_strip_recall_label(line), limit=360)
        if clean:
            return clean
    for line in receipt_substantive:
        clean = sanitize_user_surface_text(_strip_recall_label(line), limit=360)
        if clean:
            return clean
    if cont_line:
        clean = sanitize_user_surface_text(_strip_recall_label(cont_line), limit=320)
        if clean:
            return clean
    return ""


def _state_snapshot_is_low_information_only(
    status: str,
    result_kind: str,
    phase_summary: str,
    blocked_reason: str,
) -> bool:
    if blocked_reason and len(blocked_reason.strip()) > 6:
        return False
    if phase_summary and len(phase_summary.strip()) > 14:
        return False
    st = str(status or "").strip().lower()
    rk = str(result_kind or "").strip().lower()
    trivial = {"", "created", "queued", "pending", "running", "completed"}
    return st in trivial and rk in trivial


def build_cursor_continuity_recall_reply_from_state(
    conn: Any,
    task_id: str,
    *,
    user_message: str = "",
) -> str:
    """
    Human-facing recall from delegated OpenClaw narrative, receipts, and outcome state.
    Prefers durable summaries over raw task status / result_kind metadata.
    """
    cid = _telegram_chat_id_for_task(conn, task_id)
    use_id = (
        select_best_task_for_cursor_recall(conn, task_id, user_message)
        if cid is not None
        else task_id
    )
    meta = _projection_meta(conn, use_id)
    proj = _projection_full(conn, use_id)
    outcome = _outcome_section(meta)
    asst = _assistant_section(meta)
    summary = strip_conversational_soft_failure_boilerplate(str(proj.get("summary") or "").strip())
    last_reply = strip_conversational_soft_failure_boilerplate(str(asst.get("last_reply") or "").strip())
    status = str(proj.get("status") or "").strip()
    phase_summary = sanitize_user_surface_text(
        str(outcome.get("current_phase_summary") or ""), limit=500
    )
    result_kind = str(outcome.get("result_kind") or "").strip()
    blocked_reason = sanitize_user_surface_text(str(outcome.get("blocked_reason") or ""), limit=400)

    narrative_lines = _openclaw_narrative_lines(meta)

    receipt_substantive: List[str] = []
    receipt_generic: List[str] = []
    for row in list_recent_user_outcome_receipts_for_task(conn, use_id, limit=3):
        raw_summ = str(row["summary"] or "")
        summ = sanitize_user_surface_text(raw_summ, limit=400)
        kind = str(row["receipt_kind"] or "").strip()
        if not summ:
            continue
        line = f"Recent receipt ({kind}): {summ}" if kind else f"Recent receipt: {summ}"
        if _ledger_receipt_summary_is_generic_placeholder(raw_summ):
            receipt_generic.append(line)
        else:
            receipt_substantive.append(line)

    exec_lines = _execution_recall_context_lines(conn, use_id)

    tm = _telegram_section(meta)
    cr = tm.get("continuation_records") if isinstance(tm.get("continuation_records"), list) else []
    cont_line = ""
    if cr:
        last = cr[-1]
        if isinstance(last, dict):
            r = sanitize_user_surface_text(str(last.get("reason") or ""), limit=320)
            if r:
                cont_line = f"Continuation context: {r}"

    assistant_lines: List[str] = []
    um = str(user_message or "").strip()
    recall_shaped = is_cursor_thread_recall_question(um)
    if last_reply and len(last_reply) > 12 and not _is_echo_of_user_question(last_reply, um):
        safe_lr = sanitize_user_surface_text(last_reply, limit=900)
        if safe_lr:
            assistant_lines.append(f"Last assistant update on this task: {safe_lr}")
    elif summary and len(summary) > 12 and not _is_echo_of_user_question(summary, um):
        safe_s = sanitize_user_surface_text(summary, limit=900)
        if safe_s:
            assistant_lines.append(f"Recorded summary: {safe_s}")

    state_bits: List[str] = []
    if status:
        state_bits.append(f"task status **{status}**")
    if phase_summary:
        state_bits.append(f"phase: {phase_summary}")
    if result_kind:
        state_bits.append(f"result: **{result_kind}**")
    if blocked_reason:
        state_bits.append(f"blocker: {blocked_reason}")

    low_info = _state_snapshot_is_low_information_only(
        status, result_kind, phase_summary, blocked_reason
    )
    has_substantive_recap = bool(
        narrative_lines or receipt_substantive or cont_line or assistant_lines
    )
    if recall_shaped:
        lines = []
        recap_lead = _pick_cursor_recap_lead(
            narrative_lines=narrative_lines,
            assistant_lines=assistant_lines,
            receipt_substantive=receipt_substantive,
            cont_line=cont_line,
        )
        if recap_lead:
            lines.append(f"Cursor recap: {recap_lead}")

        if cont_line and _strip_recall_label(cont_line) not in recap_lead:
            lines.append(cont_line)

        # Include at most one extra substantive corroboration line for brevity.
        for line in list(narrative_lines) + list(receipt_substantive) + list(assistant_lines):
            if len(lines) >= (3 if recap_lead else 2):
                break
            if _strip_recall_label(line) == recap_lead:
                continue
            lines.append(line)

        lines.extend(receipt_generic[:1])
        if not recap_lead or _user_message_asks_explicit_status(um):
            lines.extend(exec_lines)

        has_narrative = bool(
            narrative_lines
            or receipt_substantive
            or cont_line
            or assistant_lines
            or receipt_generic
        )
        show_where = bool(state_bits) and (
            (has_substantive_recap and bool(recap_lead))
            or _user_message_asks_explicit_status(um)
        )
        if state_bits and show_where:
            lines.append("Where things stand: " + "; ".join(state_bits) + ".")
    else:
        lines = []
        lines.extend(narrative_lines)
        lines.extend(exec_lines)
        lines.extend(receipt_substantive)
        if cont_line:
            lines.append(cont_line)
        lines.extend(assistant_lines)
        lines.extend(receipt_generic)
        has_narrative = bool(
            narrative_lines
            or receipt_substantive
            or exec_lines
            or cont_line
        )
        if state_bits and (has_narrative or not low_info):
            lines.append("Where things stand: " + "; ".join(state_bits) + ".")

    if not lines:
        if is_cursor_thread_recall_question(um):
            return _GRACE_CURSOR_CONTINUITY
        return _GRACE_GENERIC_HISTORY

    if (
        low_info
        and not has_narrative
        and not assistant_lines
        and is_cursor_thread_recall_question(um)
        and not exec_lines
    ):
        return _GRACE_CURSOR_CONTINUITY

    tail = ""
    if blocked_reason or followthrough_needs_user_attention(_followthrough_section(meta)):
        tail = " Next step: address the open item above when you’re ready."
    return ("\n".join(lines) + tail).strip()


def build_recent_outcome_history_reply_from_state(
    conn: Any,
    task_id: str,
    *,
    user_message: str = "",
) -> str:
    """Backward-compatible alias for continuity recall (prefers delegated narrative)."""
    return build_cursor_continuity_recall_reply_from_state(
        conn, task_id, user_message=user_message
    )


def build_blocked_state_reply_from_state(conn: Any, task_id: str) -> str:
    """
    Short, concrete blocker answer from outcome, approvals, follow-through, and ledger rows.
    Always returns user-facing text (including explicit no-blocker when nothing is live).
    """
    meta = _projection_meta(conn, task_id)
    outcome = _outcome_section(meta)
    ft = _followthrough_section(meta)
    blocked_reason = str(outcome.get("blocked_reason") or "").strip()
    phase_summary = str(outcome.get("current_phase_summary") or "").strip()
    phase = str(outcome.get("current_phase") or "").strip()
    result_kind = str(outcome.get("result_kind") or "").strip()

    pending = list_pending_goal_approvals_for_task(conn, task_id)
    lines: List[str] = []

    if blocked_reason:
        lines.append(f"The main blocker right now is: {blocked_reason}")
    elif pending:
        top = pending[0]
        aid = str(top.get("approval_id") or "").strip() or "approval"
        rationale = str(top.get("rationale") or "").strip()
        lines.append(
            f"The main blocker right now is a pending approval (`{aid}`)"
            + (f": {rationale}" if rationale else ".")
        )
    elif followthrough_needs_user_attention(ft):
        r = str(ft.get("last_closure_reason") or ft.get("last_open_loop_state") or "").strip()
        if r:
            lines.append(f"The main blocker right now is follow-up state: {r[:280]}")
        else:
            lines.append(
                "The main blocker right now is that something still needs your input or confirmation."
            )

    if not lines:
        for row in list_recent_open_loop_records_for_task(conn, task_id, limit=2):
            lk = str(row["loop_kind"] or "").strip()
            st = str(row["open_loop_state"] or "").strip()
            ore = str(row["opened_reason"] or "").strip()[:200]
            if lk or st or ore:
                lines.append(
                    "The main blocker right now is an open loop"
                    + (f" ({lk})" if lk else "")
                    + f": {st or ore or 'see task details'}."
                )
                break
        if not lines:
            for row in list_recent_stale_task_indicators_for_task(conn, task_id, limit=1):
                sk = str(row["staleness_kind"] or "").strip()
                rsn = str(row["reason"] or "").strip()[:200]
                if sk or rsn:
                    lines.append(
                        f"Risk signal on the task: {sk or 'stale'}{(' — ' + rsn) if rsn else ''}."
                    )
                    break

    phase_bits = phase_summary or phase
    if phase_bits:
        lines.append(f"The task is in: {phase_bits}" + (f" (result: {result_kind})" if result_kind else "."))
    elif result_kind:
        lines.append(f"Current result state: **{result_kind}**.")

    if not lines:
        return (
            "I'm not seeing a live blocker in the current tracked work right now. "
            "If you want, I can check a specific task or recap the latest outcome."
        )

    nxt = ""
    for row in list_recent_followup_recommendations_for_task(conn, task_id, limit=1):
        act = str(row["recommended_action"] or "").strip()[:220]
        if act:
            nxt = f" Next useful move: {act}"
            break
    if not nxt and pending and not blocked_reason:
        nxt = " Next useful move: review the pending approval and confirm or revise."
    elif not nxt and followthrough_needs_user_attention(ft):
        nxt = " Next useful move: reply with the decision or detail I’m waiting on."
    lead = " ".join(lines).strip()
    return f"{lead}{nxt}".strip()


def _resolve_cursor_heavy_lift_reply(
    conn: Any,
    task_id: str,
    *,
    user_message: str = "",
) -> Tuple[Optional[str], bool]:
    """
    Rank same-chat Cursor heavy-lift surfaces by recency, active execution, and narrative.
    Bare anaphoric continuation may ask for clarification when top candidates tie.
    Returns (reply_text, used_alternate_task).
    """
    um = str(user_message or "").strip()
    cid = _telegram_chat_id_for_task(conn, task_id)
    cands = (
        _ordered_cursor_chat_candidates(conn, task_id, cid)
        if cid is not None
        else [task_id]
    )
    idx_map = {tid: i for i, tid in enumerate(cands)}
    entries: List[Tuple[int, bool, str, str]] = []
    for tid in cands:
        reply = build_cursor_heavy_lift_context_reply(conn, tid)
        if not reply:
            continue
        sc, meaningful = _continuation_candidate_score(conn, tid, um)
        entries.append((sc, meaningful, tid, reply))
    if not entries:
        return None, False

    anaphoric = user_message_suggests_anaphoric_cursor_continue(um)
    entries.sort(key=lambda e: (-e[0], 0 if e[1] else 1, idx_map.get(e[2], 10**6)))

    if anaphoric and len(entries) >= 2:
        s0, m0, t0, _r0 = entries[0]
        s1, m1, t1, _r1 = entries[1]
        if m0 and m1 and (s0 - s1) < 130:
            return _AMBIGUOUS_CURSOR_CONTINUE_REPLY, False
        cur_e = next((e for e in entries if e[2] == task_id), None)
        if cur_e and t0 != task_id and m0 and cur_e[1]:
            if (s0 - cur_e[0]) < 90:
                return _AMBIGUOUS_CURSOR_CONTINUE_REPLY, False

    _best_s, _best_m, best_t, best_r = entries[0]
    used_alt = best_t != task_id
    return best_r, used_alt


def cursor_followup_context_reply_with_fallback(
    conn: Any,
    task_id: str,
    *,
    user_message: str,
) -> str:
    """
    Conductor-style continuation: current Cursor workstream, or recent same-chat delegated task,
    or an explicit boundary when nothing durable is available.
    """
    um = str(user_message or "")
    cur, used_alt = _resolve_cursor_heavy_lift_reply(conn, task_id, user_message=um)
    if cur:
        if used_alt:
            return (
                "I found the recent Cursor workstream on this thread and I’m continuing it "
                "from the latest tracked context.\n"
                f"{cur}"
            )
        return cur
    cid = _telegram_chat_id_for_task(conn, task_id)
    alt = (
        find_recent_delegated_cursor_task_id(
            conn, task_id, chat_id=cid, user_message=um
        )
        if cid
        else None
    )
    if alt:
        recap = build_cursor_continuity_recall_reply_from_state(
            conn, alt, user_message=user_message
        )
        if recap not in (_GRACE_GENERIC_HISTORY, _GRACE_CURSOR_CONTINUITY):
            return (
                "The last Cursor run on this thread already finished. "
                "I can start a new heavy-lift pass using this as the starting point:\n"
                f"{recap}"
            )
    return (
        "I’m not finding a recent Cursor workstream with enough context to safely continue, "
        "so I’d need to start a new one from your latest instruction."
    )


def build_cursor_heavy_lift_context_reply(conn: Any, task_id: str) -> Optional[str]:
    """Orchestration-style recap for in-flight or recent Cursor / OpenClaw execution."""
    meta = _projection_meta(conn, task_id)
    contract = build_delegated_lifecycle_contract(meta)
    ex = contract.get("execution") if isinstance(contract.get("execution"), dict) else {}
    cur = contract.get("cursor") if isinstance(contract.get("cursor"), dict) else {}
    oc_contract = contract.get("openclaw") if isinstance(contract.get("openclaw"), dict) else {}
    oc_meta = meta.get("openclaw") if isinstance(meta.get("openclaw"), dict) else {}
    outcome = _outcome_section(meta)
    att = summarize_execution_attempt_for_user(conn, task_id)
    oc_lines = _openclaw_narrative_lines(meta)
    has_signal = bool(
        ex.get("delegated_to_cursor")
        or cur.get("agent_id")
        or oc_contract.get("run_id")
        or str(oc_meta.get("user_summary") or "").strip()
        or oc_lines
        or str(outcome.get("current_phase_summary") or "").strip()
        or (att.get("ok") if isinstance(att, dict) else False)
    )
    if not has_signal:
        return None

    parts: List[str] = []
    us = sanitize_user_surface_text(str(oc_meta.get("user_summary") or ""), limit=700)
    if us:
        parts.append(f"Latest useful result: {us}")
    elif oc_lines:
        parts.extend(oc_lines[:3])
    phase = sanitize_user_surface_text(
        str(outcome.get("current_phase_summary") or outcome.get("current_phase") or ""),
        limit=500,
    )
    if phase:
        parts.append(f"Current heavy-lift phase: {phase}.")
    br = str(outcome.get("blocked_reason") or "").strip()
    if br:
        parts.append(f"Blocker / wait state: {sanitize_user_surface_text(br, limit=400)}.")
    if isinstance(att, dict) and att.get("ok"):
        parts.append(
            f"Execution lane `{att.get('lane') or 'n/a'}` is **{att.get('status') or 'unknown'}** "
            f"(backend: {att.get('backend') or 'n/a'})."
        )
    aid = str(cur.get("agent_id") or "").strip()
    if aid:
        parts.append("Cursor is attached for delegated execution when this lane runs.")
    rid = str(oc_contract.get("run_id") or oc_meta.get("run_id") or "").strip()
    if rid:
        parts.append(f"OpenClaw run `{rid}` is part of this workstream.")
    nacts = contract.get("recommended_next_actions") or []
    if isinstance(nacts, list) and nacts:
        parts.append(
            "Likely next moves on this workstream: "
            + ", ".join(str(x) for x in nacts[:4])
            + "."
        )
    if not parts:
        return None
    return (
        "I'm keeping this on the current heavy-lift workstream.\n"
        + "\n".join(f"• {p}" for p in parts)
    )


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

    if domain in {"project_status", "approval_state"} and turn_plan.continuity_focus == "blocked_state":
        text = build_blocked_state_reply_from_state(conn, task_id)
        return text, "composer_blocked_state"

    if domain == "project_status" and turn_plan.continuity_focus == "cursor_followup_heavy_lift":
        cur, used_alt = _resolve_cursor_heavy_lift_reply(
            conn, task_id, user_message=str(user_text or "")
        )
        if cur:
            if used_alt:
                return (
                    "I found the recent Cursor workstream on this thread and I’m continuing it "
                    "from the latest tracked context.\n"
                    f"{cur}"
                ), "composer_cursor_heavy_lift_context"
            return cur, "composer_cursor_heavy_lift_context"

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
        cf = turn_plan.continuity_focus
        if cf == "blocked_state":
            blk = build_blocked_state_reply_from_state(conn, task_id)
            out.append(AnswerCandidate(source="blocked_state_reply", text=blk, priority=99))
        elif cf == "recent_outcome_history":
            cc = build_cursor_continuity_recall_reply_from_state(
                conn, task_id, user_message=str(classify_text or "")
            )
            out.append(AnswerCandidate(source="cursor_continuity_recall", text=cc, priority=100))
        elif cf == "cursor_followup_heavy_lift":
            cur, used_alt = _resolve_cursor_heavy_lift_reply(
                conn, task_id, user_message=str(classify_text or "")
            )
            if cur:
                text = (
                    (
                        "I found the recent Cursor workstream on this thread and I’m continuing it "
                        "from the latest tracked context.\n"
                        f"{cur}"
                    )
                    if used_alt
                    else cur
                )
                out.append(
                    AnswerCandidate(source="cursor_heavy_lift_context", text=text, priority=97)
                )

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
            if cf == "recent_outcome_history":
                out.append(
                    AnswerCandidate(
                        source="goal_continuity",
                        text=(
                            "I don't have enough recorded history on the current task to say that confidently. "
                            "I can still check the latest linked goal or start tracking the next step explicitly."
                        ),
                        priority=71,
                    )
                )
            elif cf != "blocked_state":
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
    classify_text: str = "",
) -> Optional[Tuple[str, str]]:
    """
    Choose a non-model candidate when it is safe to override the draft direct reply.
    """
    if not candidates:
        return None
    model = str(model_reply or "").strip()
    ordered = sorted(candidates, key=lambda x: x.priority, reverse=True)
    generic_model = is_generic_direct_reply(model)

    for winner in ordered:
        if winner.source == "model":
            continue
        wtext = winner.text.strip()
        if not wtext or wtext == model:
            continue

        if winner.source in {
            "external_heuristic",
            "opinion_contextual",
            "agenda_state",
            "attention_state",
        }:
            if generic_model or winner.priority >= 85:
                return wtext, winner.source
            continue

        if winner.source == "cursor_continuity_recall":
            if is_cursor_thread_recall_question(classify_text) and _cursor_recall_composition_is_metadata_led(
                wtext
            ):
                continue
            return wtext, winner.source

        if winner.source in {"blocked_state_reply", "cursor_heavy_lift_context"}:
            return wtext, winner.source

        if winner.source == "followthrough_goal":
            return wtext, winner.source

        if winner.source == "state_rich_goal":
            if generic_model or stateful_goal_ok:
                return wtext, winner.source
            if draft_implies_false_completion(model) and followthrough_needs_user_attention(
                followthrough
            ):
                return wtext, winner.source
            continue

        if winner.source in {"goal_continuity", "followthrough_only"}:
            if generic_model or stateful_goal_ok:
                return wtext, winner.source
            if draft_implies_false_completion(model) and followthrough_needs_user_attention(
                followthrough
            ):
                return wtext, winner.source
            continue

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

    mechanical = draft_should_force_continuity_repair(reply_text, classify_text)
    generic = (
        is_generic_direct_reply(reply_text)
        or str(decision_reason or "") in {
            "short_general_request",
            "balanced_default_direct",
        }
        or mechanical
    )
    plan_repairs = bool(turn_plan.should_repair_generic)
    allow_goal = bool(turn_plan.allow_goal_continuity_repair)
    scenario_continuity = str(getattr(resolution, "scenario_id", "") or "") == (
        "statusFollowupContinue"
    )

    plan_prefers_state = bool(turn_plan.prefer_state_reply)
    cf = turn_plan.continuity_focus
    stateful_goal_ok = (
        scenario_continuity
        or plan_prefers_state
        or continuity_ask
        or continuity_state
        or mechanical
        or cf
        in {"blocked_state", "recent_outcome_history", "cursor_followup_heavy_lift"}
    )

    should_run_goal_branch = allow_goal and (
        scenario_continuity
        or plan_prefers_state
        or (
            (generic and plan_repairs)
            and (continuity_ask or continuity_state or mechanical)
        )
        or cf
        in {"blocked_state", "recent_outcome_history", "cursor_followup_heavy_lift"}
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
        classify_text=str(classify_text or ""),
    )
