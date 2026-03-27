"""Bounded LLM-backed realization for stateful assistant answers."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Sequence

from .assistant_answer_composer import (
    _cursor_recall_output_should_force_clean_fallback,
    build_stateful_summary_bundle,
    gather_cursor_recall_evidence_pack,
    is_continuation_fallback_family_text,
    is_strict_cursor_domain_recall_question,
)
from .model_router import model_for_role
from .turn_intelligence import TurnPlan, resolve_answer_family_profile
from .user_surface import sanitize_user_surface_text

_STOP_WORDS = frozenset(
    {
        "the",
        "this",
        "that",
        "with",
        "from",
        "your",
        "there",
        "about",
        "have",
        "been",
        "into",
        "only",
        "after",
        "when",
        "what",
        "where",
        "which",
        "while",
        "would",
        "could",
        "should",
        "still",
        "just",
        "than",
        "then",
    }
)


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def stateful_realization_enabled() -> bool:
    if not _env_truthy("ANDREA_STATEFUL_REALIZATION_ENABLED", True):
        return False
    if not _env_truthy("OPENAI_API_ENABLED", False):
        return False
    return bool((os.environ.get("OPENAI_API_KEY") or "").strip())


def partial_evidence_realization_enabled() -> bool:
    return _env_truthy("ANDREA_PARTIAL_EVIDENCE_REALIZATION_ENABLED", True)


def technical_uncertainty_assist_enabled() -> bool:
    return _env_truthy("ANDREA_TECHNICAL_UNCERTAINTY_ASSIST_ENABLED", True)


def _local_brevity_profile(answer_mode: str) -> tuple[str, int]:
    """Keep aligned with semantic_answer_engine.brevity_profile_for_answer_mode (no import cycle)."""
    m = str(answer_mode or "").strip()
    if m == "strong_evidence_answer":
        return "concise_grounded_summary", 115
    if m == "partial_evidence_helpful_answer":
        return "partial_helpful_brevity", 185
    return "truthful_next_steps_brevity", 260


def _mode_for_stateful_strength(evidence_strength: int) -> tuple[str, str]:
    if int(evidence_strength or 0) >= 6:
        return "strong_evidence_answer", "clear"
    if int(evidence_strength or 0) >= 2:
        return "partial_evidence_helpful_answer", "partial"
    return "truthful_fallback_with_next_steps", "thin"


def _reply_word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", str(text or "")))


def _user_requests_action_guidance(text: str) -> bool:
    low = str(text or "").strip().lower()
    if not low:
        return False
    return bool(
        re.search(
            r"\b("
            r"what\s+should\s+i\s+do|what\s+do\s+i\s+do|"
            r"next\s+step|next\s+move|how\s+do\s+i|how\s+should\s+i|"
            r"how\s+can\s+i|how\s+to\s+unblock|how\s+to\s+fix|"
            r"what\s+now|what\s+should\s+we\s+do|what\s+is\s+the\s+best\s+next"
            r")\b",
            low,
        )
    )


def _should_include_stateful_next_steps(
    *,
    turn_domain: str,
    family: str,
    answer_mode: str,
    user_text: str,
    options: Sequence[str],
) -> bool:
    if not options:
        return False
    if _user_requests_action_guidance(user_text):
        return True
    if answer_mode == "strong_evidence_answer":
        return False
    if turn_domain in {"personal_agenda", "attention_today"}:
        return False
    if family == "blocked_state":
        return False
    return True


def _split_next_options_suffix(text: str) -> tuple[str, str]:
    t = str(text or "")
    low = t.lower()
    key = "next options:"
    i = low.find(key)
    if i < 0:
        return t.strip(), ""
    return t[:i].strip(), t[i:].strip()


def _compress_reply_to_word_budget(
    reply: str,
    *,
    max_words: int,
    evidence_lines: Sequence[str],
    required_anchors: Sequence[str],
) -> str:
    head, tail = _split_next_options_suffix(reply)
    if _reply_word_count(head) <= max_words:
        return reply.strip()
    sentences = re.split(r"(?<=[.!?])\s+", head)
    out: List[str] = []
    wc = 0
    for sent in sentences:
        chunk = sent.strip()
        if not chunk:
            continue
        sw = _reply_word_count(chunk)
        if out and wc + sw > max_words:
            break
        out.append(chunk)
        wc += sw
    new_head = " ".join(out).strip()
    if not new_head:
        return reply.strip()
    if not _evidence_anchor_overlap(new_head, evidence_lines):
        return reply.strip()
    if required_anchors and not _anchors_present_in_reply(new_head, required_anchors):
        return reply.strip()
    if tail:
        return sanitize_user_surface_text(f"{new_head}\n\n{tail}", fallback=new_head, limit=1200).strip()
    return new_head


def _allowed_sources() -> set[str]:
    raw = (os.environ.get("ANDREA_STATEFUL_REALIZATION_SOURCES") or "").strip()
    if not raw:
        return {
            "cursor_continuity_recall",
            "cursor_heavy_lift_context",
            "blocked_state_reply",
            "goal_status",
            "goal_continuity",
        }
    return {s.strip() for s in raw.split(",") if s.strip()}


def _tokenize(value: str) -> set[str]:
    out: set[str] = set()
    for tok in re.split(r"[^a-zA-Z0-9_]+", str(value or "").lower()):
        if len(tok) < 5 or tok in _STOP_WORDS:
            continue
        out.add(tok)
    return out


def _evidence_anchor_overlap(reply: str, evidence_lines: Sequence[str]) -> bool:
    r = _tokenize(reply)
    if not r:
        return False
    anchor = set()
    for ln in evidence_lines:
        anchor |= _tokenize(ln)
    if not anchor:
        return False
    return bool(r & anchor)


def _looks_fallback_shaped_reply(text: str) -> bool:
    low = str(text or "").strip().lower()
    if not low:
        return True
    if "next options:" in low:
        return False
    patterns = (
        "not finding a recent clean cursor result",
        "not finding a recent cursor workstream",
        "i do not see active tracked work right now",
        "i'm not seeing any approval requests waiting on you right now",
        "i’m not seeing any approval requests waiting on you right now",
        "status / follow-up reply",
    )
    return any(p in low for p in patterns)


def _evidence_strength(evidence_lines: Sequence[str]) -> int:
    score = 0
    for ln in evidence_lines:
        txt = str(ln or "").strip()
        if not txt:
            continue
        score += 1
        if ":" in txt:
            score += 1
        if len(txt) > 48:
            score += 1
    return score


def _openai_json_chat(*, system: str, user: str, model: str, timeout_seconds: int) -> dict[str, Any]:
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("stateful_realization_missing_key")
    payload: dict[str, Any] = {
        "model": model,
        "temperature": 0.35,
        "max_tokens": 260,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"stateful_realization_http_{err.code}:{raw[:240]}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"stateful_realization_transport:{err}") from err
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("stateful_realization_no_choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    content = str(message.get("content") or "").strip()
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise RuntimeError("stateful_realization_json_not_object")
    return parsed


@dataclass(frozen=True)
class StatefulRealizationInput:
    source: str
    deterministic_reply: str
    fallback_reply: str
    user_text: str
    turn_domain: str
    continuity_focus: str
    family: str
    required_anchors: tuple[str, ...]
    evidence_strength: int
    fallback_policy: str
    evidence_lines: tuple[str, ...]
    answer_mode: str
    uncertainty_mode: str
    next_step_options: tuple[str, ...]
    guidance_class: str = ""
    primary_finding: str = ""
    supporting_evidence_lines: tuple[str, ...] = ()
    uncertainty_boundary: str = ""
    utility_goal: str = "concise_grounded_summary"
    brevity_max_words_soft: int = 115


def _split_structured_text_lines(text: str) -> List[str]:
    out: List[str] = []
    for raw in re.split(r"[\r\n]+", str(text or "")):
        clean = str(raw or "").strip().lstrip("-* ").strip()
        if clean:
            out.append(clean)
    return out


def _bundle_evidence_for_source(
    conn: Any, task_id: str, *, source: str, user_text: str, deterministic_reply: str
) -> List[str]:
    lines: List[str] = []
    if source == "cursor_continuity_recall":
        pack = gather_cursor_recall_evidence_pack(conn, task_id, user_message=user_text)
        lines.extend(list(pack.source_truth_narrative_lines)[:4])
        lines.extend(list(pack.source_truth_receipt_lines)[:2])
        if pack.outcome_phase_summary:
            lines.append(f"Phase summary: {pack.outcome_phase_summary}")
        if pack.outcome_blocked_reason:
            lines.append(f"Blocked reason: {pack.outcome_blocked_reason}")
        lines.extend(list(pack.support_execution_lines)[:1])
    if source in {
        "cursor_continuity_recall",
        "cursor_heavy_lift_context",
        "blocked_state_reply",
        "goal_status",
        "goal_continuity",
    }:
        lines.extend(_split_structured_text_lines(deterministic_reply))
    if not lines:
        lines.extend(_split_structured_text_lines(deterministic_reply))
    if not lines:
        lines.append(deterministic_reply)
    out: List[str] = []
    seen: set[str] = set()
    for ln in lines:
        safe = sanitize_user_surface_text(ln, fallback="", limit=340)
        key = safe.lower()
        if safe and key not in seen:
            seen.add(key)
            out.append(safe)
    return out[:8]


def _merged_evidence_lines(contract: Mapping[str, Any] | None, bundled: Sequence[str]) -> tuple[str, ...]:
    contract_lines_raw = contract.get("evidence_lines") if isinstance(contract, Mapping) else None
    contract_lines = (
        [str(x).strip() for x in contract_lines_raw if str(x).strip()]
        if isinstance(contract_lines_raw, list)
        else []
    )
    merged: list[str] = []
    seen: set[str] = set()
    for ln in [*contract_lines, *list(bundled)]:
        safe = sanitize_user_surface_text(ln, fallback="", limit=340)
        key = safe.lower()
        if safe and key not in seen:
            seen.add(key)
            merged.append(safe)
    return tuple(merged[:8])


def _contract_string(contract: Mapping[str, Any] | None, key: str, limit: int = 420) -> str:
    if not isinstance(contract, Mapping):
        return ""
    return sanitize_user_surface_text(str(contract.get(key) or ""), fallback="", limit=limit).strip()


def _contract_string_tuple(contract: Mapping[str, Any] | None, key: str, limit: int = 420) -> tuple[str, ...]:
    raw = contract.get(key) if isinstance(contract, Mapping) else None
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for val in raw:
        clean = sanitize_user_surface_text(str(val or ""), fallback="", limit=limit).strip()
        if not clean:
            continue
        low = clean.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(clean)
    return tuple(out[:4])


def _contract_required_anchors(contract: Mapping[str, Any] | None) -> tuple[str, ...]:
    raw = contract.get("required_anchors") if isinstance(contract, Mapping) else None
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for val in raw:
        clean = str(val or "").strip().lower()
        if clean and clean not in out:
            out.append(clean)
    return tuple(out)


def _anchors_present_in_reply(reply: str, anchors: Sequence[str]) -> bool:
    if not anchors:
        return True
    low = str(reply or "").lower()
    for anchor in anchors:
        if anchor and anchor not in low:
            return False
    return True


def next_step_options_reflected_in_reply(reply: str, options: Sequence[str]) -> bool:
    """Public helper for eval/harness: overlap between rendered reply and approved next-step cues."""
    return _next_step_options_reflected(reply, options)


def _next_step_options_reflected(reply: str, options: Sequence[str]) -> bool:
    """True when the reply appears to carry at least half of the approved next-step cues."""
    if not options:
        return True
    r = str(reply or "").lower()
    hits = 0
    for opt in options:
        words = [w for w in re.split(r"\W+", str(opt or "").lower()) if len(w) >= 5]
        if not words:
            hits += 1
            continue
        need = max(1, len(words) // 3)
        got = sum(1 for w in words if w in r)
        if got >= need:
            hits += 1
    return hits >= max(1, (len(list(options)) + 1) // 2)


def _specific_guidance_tokens(text: str) -> set[str]:
    low = str(text or "").lower()
    mapping = {
        "traceback": ("traceback", "stack trace", "exception"),
        "error": ("error", "failed", "failure"),
        "version": ("version", "compatib", "upgrade", "downgrade"),
        "config": ("config", "flag", "yaml", "toml", "ini", "environment", "runtime"),
        "command": ("command", "output"),
        "task": ("task label", "task id", "goal id", "approval id", "workstream"),
    }
    out: set[str] = set()
    for key, needles in mapping.items():
        if any(n in low for n in needles):
            out.add(key)
    return out


def _specific_next_step_guidance_reflected(reply: str, options: Sequence[str]) -> bool:
    required = set()
    for opt in options:
        required |= _specific_guidance_tokens(opt)
    if not required:
        return True
    present = _specific_guidance_tokens(reply)
    return bool(required & present)


def _assemble_stateful_with_next_steps(body: str, options: Sequence[str]) -> str:
    b = sanitize_user_surface_text(str(body or "").strip(), fallback="", limit=1200).strip()
    opts = [sanitize_user_surface_text(str(o), fallback="", limit=420).strip() for o in options if str(o).strip()][
        :2
    ]
    if not b:
        return ""
    if not opts:
        return b
    lines = "\n".join(f"• {o}" for o in opts)
    return sanitize_user_surface_text(f"{b}\n\nNext options:\n{lines}", fallback=b, limit=1200).strip()


def maybe_realize_stateful_reply(
    conn: Any,
    task_id: str,
    *,
    source: str,
    deterministic_reply: str,
    fallback_reply: str,
    user_text: str,
    turn_plan: TurnPlan,
    turn_contract: Mapping[str, Any] | None = None,
) -> str | None:
    """Return a naturalized stateful reply or None to keep deterministic output."""
    if not stateful_realization_enabled():
        return None
    if source not in _allowed_sources():
        return None
    bundled = _bundle_evidence_for_source(
        conn, task_id, source=source, user_text=user_text, deterministic_reply=deterministic_reply
    )
    bundle_primary = ""
    bundle_supporting: tuple[str, ...] = ()
    bundle_uncertainty = ""
    if conn is not None and str(task_id or "").strip():
        try:
            sb = build_stateful_summary_bundle(
                conn,
                str(task_id),
                source=str(source),
                user_message=str(user_text or ""),
                deterministic_reply=str(deterministic_reply or ""),
            )
            if sb.primary_finding:
                bundle_primary = str(sb.primary_finding)
            if sb.secondary_evidence_lines:
                bundle_supporting = tuple(str(x) for x in sb.secondary_evidence_lines)
            if sb.uncertainty_boundary:
                bundle_uncertainty = str(sb.uncertainty_boundary)
            if sb.evidence_lines:
                bundled = [*list(sb.evidence_lines), *list(bundled)]
        except Exception:
            pass
    evidence = _merged_evidence_lines(turn_contract, bundled)
    family = resolve_answer_family_profile(str(user_text or "").strip(), turn_plan)
    contract_family = (
        str(turn_contract.get("family") or "").strip()
        if isinstance(turn_contract, Mapping)
        else ""
    )
    effective_family = contract_family or family.family
    required_anchors = _contract_required_anchors(turn_contract)
    evidence_strength = (
        int(turn_contract.get("evidence_strength") or 0)
        if isinstance(turn_contract, Mapping)
        else 0
    )
    merged_strength = _evidence_strength(evidence)
    evidence_strength = max(int(evidence_strength or 0), int(merged_strength or 0))
    fallback_policy = (
        str(turn_contract.get("fallback_policy") or "").strip()
        if isinstance(turn_contract, Mapping)
        else ""
    )
    contract_answer_mode = (
        str(turn_contract.get("answer_mode") or "").strip()
        if isinstance(turn_contract, Mapping)
        else ""
    )
    contract_uncertainty_mode = (
        str(turn_contract.get("uncertainty_mode") or "").strip()
        if isinstance(turn_contract, Mapping)
        else ""
    )
    answer_mode = contract_answer_mode or "strong_evidence_answer"
    uncertainty_mode = contract_uncertainty_mode or "clear"
    # If merged evidence is stronger than contract text-only evidence, allow mode uplift.
    contract_strength = (
        int(turn_contract.get("evidence_strength") or 0)
        if isinstance(turn_contract, Mapping)
        else 0
    )
    if merged_strength > contract_strength:
        lifted_mode, lifted_uncertainty = _mode_for_stateful_strength(evidence_strength)
        mode_order = {
            "truthful_fallback_with_next_steps": 0,
            "partial_evidence_helpful_answer": 1,
            "strong_evidence_answer": 2,
        }
        if mode_order.get(lifted_mode, 0) > mode_order.get(answer_mode, 0):
            answer_mode = lifted_mode
            uncertainty_mode = lifted_uncertainty
    next_raw = turn_contract.get("next_step_options") if isinstance(turn_contract, Mapping) else None
    next_step_options: tuple[str, ...] = ()
    if isinstance(next_raw, list):
        next_step_options = tuple(
            sanitize_user_surface_text(str(x), fallback="", limit=420).strip()
            for x in next_raw
            if str(x).strip()
        )[:2]
    utility_goal = (
        str(turn_contract.get("utility_goal") or "").strip()
        if isinstance(turn_contract, Mapping)
        else ""
    )
    brevity_max_words_soft = 0
    if isinstance(turn_contract, Mapping):
        try:
            brevity_max_words_soft = int(turn_contract.get("brevity_max_words_soft") or 0)
        except (TypeError, ValueError):
            brevity_max_words_soft = 0
    if not utility_goal or brevity_max_words_soft <= 0:
        utility_goal, brevity_max_words_soft = _local_brevity_profile(answer_mode)
    if not _should_include_stateful_next_steps(
        turn_domain=str(turn_plan.domain or ""),
        family=str(effective_family or ""),
        answer_mode=str(answer_mode or ""),
        user_text=str(user_text or ""),
        options=next_step_options,
    ):
        next_step_options = ()
    inp = StatefulRealizationInput(
        source=str(source or ""),
        deterministic_reply=str(deterministic_reply or "").strip(),
        fallback_reply=str(fallback_reply or "").strip(),
        user_text=str(user_text or "").strip(),
        turn_domain=str(turn_plan.domain or ""),
        continuity_focus=str(turn_plan.continuity_focus or ""),
        family=effective_family,
        required_anchors=required_anchors,
        evidence_strength=evidence_strength,
        fallback_policy=fallback_policy,
        evidence_lines=tuple(evidence),
        answer_mode=answer_mode,
        uncertainty_mode=uncertainty_mode,
        next_step_options=next_step_options,
        guidance_class=(
            str(turn_contract.get("guidance_class") or "").strip()
            if isinstance(turn_contract, Mapping)
            else ""
        ),
        primary_finding=_contract_string(turn_contract, "primary_finding", 420) or bundle_primary,
        supporting_evidence_lines=(
            _contract_string_tuple(turn_contract, "supporting_evidence_lines", 420)
            or bundle_supporting
        ),
        uncertainty_boundary=_contract_string(turn_contract, "uncertainty_boundary", 280)
        or bundle_uncertainty,
        utility_goal=utility_goal,
        brevity_max_words_soft=int(brevity_max_words_soft),
    )
    if not inp.deterministic_reply or not inp.evidence_lines:
        return None
    model = (os.environ.get("ANDREA_DIRECT_OPENAI_MODEL") or "gpt-4o-mini").strip()
    timeout_seconds = max(
        5,
        int((os.environ.get("ANDREA_STATEFUL_REALIZATION_TIMEOUT_SECONDS") or "18").strip()),
    )
    mode_rules = ""
    if partial_evidence_realization_enabled() and inp.answer_mode in {
        "partial_evidence_helpful_answer",
        "truthful_fallback_with_next_steps",
    }:
        mode_rules = (
            "7) ANSWER_MODE allows partial help: state only verified facts from EVIDENCE_LINES first, "
            "then briefly name the uncertainty boundary.\n"
            "8) Include NEXT_STEP_OPTIONS only if the user asked for an action/next move or uncertainty is blocking progress; "
            "if included, rephrase at most one or two naturally and do NOT add new options or new facts.\n"
        )
    brevity_rule = ""
    if inp.brevity_max_words_soft > 0:
        brevity_rule = (
            f"9) UTILITY_GOAL is {inp.utility_goal!r}: keep the main reply body under roughly "
            f"{inp.brevity_max_words_soft} words (excluding a trailing Next options list if present). "
            "Lead with the verified summary, not metadata scaffolding.\n"
        )
    system = (
        "You are Andrea. Rewrite state-backed assistant replies to sound natural and concise.\n"
        "Rules:\n"
        "1) Use ONLY facts from EVIDENCE_LINES.\n"
        "2) Do NOT invent entities, files, IDs, approvals, blockers, or outcomes.\n"
        "3) Preserve domain intent: recall asks recap; continuation asks continuation.\n"
        "3b) Lead with PRIMARY_FINDING when present; use SUPPORTING_EVIDENCE_LINES only as short corroboration.\n"
        "4) If evidence is weak, return the provided fallback.\n"
        "5) Never output runtime internals or configuration names.\n"
        "6) Preserve required anchors and family integrity.\n"
        f"{mode_rules}"
        f"{brevity_rule}"
        "Return JSON object with keys: reply (string), grounded (boolean), used_fallback (boolean), "
        "family_preserved (boolean), anchors_used (array of strings)."
    )
    user_payload = json.dumps(
        {
            "user_text": inp.user_text,
            "turn_domain": inp.turn_domain,
            "continuity_focus": inp.continuity_focus,
            "candidate_source": inp.source,
            "answer_family": inp.family,
            "answer_mode": inp.answer_mode,
            "uncertainty_mode": inp.uncertainty_mode,
            "guidance_class": inp.guidance_class,
            "primary_finding": inp.primary_finding,
            "supporting_evidence_lines": list(inp.supporting_evidence_lines),
            "uncertainty_boundary": inp.uncertainty_boundary,
            "utility_goal": inp.utility_goal,
            "brevity_max_words_soft": inp.brevity_max_words_soft,
            "next_step_options": list(inp.next_step_options),
            "deterministic_reply": inp.deterministic_reply,
            "fallback_reply": inp.fallback_reply,
            "required_anchors": list(inp.required_anchors),
            "evidence_strength": inp.evidence_strength,
            "fallback_policy": inp.fallback_policy,
            "evidence_lines": list(inp.evidence_lines),
        },
        ensure_ascii=False,
    )
    try:
        parsed = _openai_json_chat(
            system=system,
            user=user_payload,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        return None
    raw = str(parsed.get("reply") or "").strip()
    safe = sanitize_user_surface_text(raw, fallback="", limit=1200)
    if not safe:
        return None
    if source == "cursor_continuity_recall":
        if is_strict_cursor_domain_recall_question(inp.user_text):
            if _cursor_recall_output_should_force_clean_fallback(safe):
                return inp.fallback_reply or None
        if is_continuation_fallback_family_text(safe):
            return inp.fallback_reply or None
    if not bool(parsed.get("grounded")):
        return None
    if parsed.get("family_preserved") is False:
        return None
    if not _evidence_anchor_overlap(safe, inp.evidence_lines):
        return None
    if inp.required_anchors and not _anchors_present_in_reply(safe, inp.required_anchors):
        anchors_used = parsed.get("anchors_used")
        if not isinstance(anchors_used, list):
            return None
        used_low = {str(x).strip().lower() for x in anchors_used if str(x).strip()}
        if any(a not in used_low for a in inp.required_anchors):
            return None
    if inp.evidence_strength >= 5 and _looks_fallback_shaped_reply(safe):
        if (
            partial_evidence_realization_enabled()
            and inp.answer_mode == "partial_evidence_helpful_answer"
            and inp.next_step_options
        ):
            safe = _assemble_stateful_with_next_steps(inp.deterministic_reply, inp.next_step_options)
        else:
            return None
    if (
        partial_evidence_realization_enabled()
        and inp.answer_mode
        in {"partial_evidence_helpful_answer", "truthful_fallback_with_next_steps"}
        and inp.next_step_options
        and not _next_step_options_reflected(safe, inp.next_step_options)
    ):
        base = safe if _evidence_anchor_overlap(safe, inp.evidence_lines) else inp.deterministic_reply
        safe = _assemble_stateful_with_next_steps(base, inp.next_step_options)
    if (
        partial_evidence_realization_enabled()
        and inp.answer_mode in {"partial_evidence_helpful_answer", "truthful_fallback_with_next_steps"}
        and inp.next_step_options
        and not _specific_next_step_guidance_reflected(safe, inp.next_step_options)
    ):
        base = safe if _evidence_anchor_overlap(safe, inp.evidence_lines) else inp.deterministic_reply
        safe = _assemble_stateful_with_next_steps(base, inp.next_step_options)
    low_safe = safe.lower()
    low_ev = " ".join(str(x).lower() for x in inp.evidence_lines)
    if inp.family == "approval_state":
        if "approval" in low_ev and "approval" not in low_safe:
            return None
    if inp.family == "blocked_state":
        if ("blocked" in low_ev or "blocker" in low_ev) and (
            "blocked" not in low_safe and "blocker" not in low_safe and "blocking" not in low_safe
        ):
            return None
    slack = 38
    if inp.brevity_max_words_soft > 0 and _reply_word_count(safe) > inp.brevity_max_words_soft + slack:
        compressed = _compress_reply_to_word_budget(
            safe,
            max_words=inp.brevity_max_words_soft,
            evidence_lines=inp.evidence_lines,
            required_anchors=inp.required_anchors,
        )
        if compressed and _reply_word_count(compressed) + 5 < _reply_word_count(safe):
            safe = compressed
            if inp.next_step_options and not _next_step_options_reflected(safe, inp.next_step_options):
                base = safe if _evidence_anchor_overlap(safe, inp.evidence_lines) else inp.deterministic_reply
                safe = _assemble_stateful_with_next_steps(
                    sanitize_user_surface_text(base, fallback=inp.deterministic_reply, limit=1200).strip(),
                    inp.next_step_options,
                )
    return safe


def maybe_realize_grounded_technical_reply(
    *,
    user_text: str,
    answer_family: str,
    evidence_lines: Sequence[str],
    fallback_reply: str,
    required_anchors: Sequence[str] = (),
    evidence_strength: int = 0,
    answer_mode: str = "",
    uncertainty_mode: str = "",
    next_step_options: Sequence[str] = (),
    guidance_class: str = "",
    primary_finding: str = "",
    supporting_evidence_lines: Sequence[str] = (),
    uncertainty_boundary: str = "",
    retrieval_source: str = "",
    query: str = "",
) -> str | None:
    """Bounded synthesis for lookup-backed technical/research answers."""
    if not stateful_realization_enabled():
        return None
    if not _env_truthy("ANDREA_GROUNDED_RESEARCH_REALIZATION_ENABLED", True):
        return None
    safe_evidence = [sanitize_user_surface_text(str(x or ""), fallback="", limit=420) for x in evidence_lines]
    safe_evidence = [ln.strip() for ln in safe_evidence if str(ln or "").strip()]
    if not safe_evidence:
        return None
    fallback = sanitize_user_surface_text(str(fallback_reply or ""), fallback="", limit=1200).strip()
    if not fallback:
        return None
    ev_s = int(evidence_strength or _evidence_strength(safe_evidence))
    mode = str(answer_mode or "").strip() or (
        "strong_evidence_answer"
        if ev_s >= 6
        else ("partial_evidence_helpful_answer" if ev_s >= 2 else "truthful_fallback_with_next_steps")
    )
    u_mode = str(uncertainty_mode or "").strip() or (
        "clear" if mode == "strong_evidence_answer" else ("partial" if mode == "partial_evidence_helpful_answer" else "thin")
    )
    n_opts = tuple(
        sanitize_user_surface_text(str(x), fallback="", limit=420).strip()
        for x in next_step_options
        if str(x).strip()
    )[:2]
    primary = sanitize_user_surface_text(str(primary_finding or ""), fallback="", limit=420).strip()
    supporting = tuple(
        sanitize_user_surface_text(str(x), fallback="", limit=420).strip()
        for x in supporting_evidence_lines
        if str(x).strip()
    )[:4]
    uncertainty = sanitize_user_surface_text(str(uncertainty_boundary or ""), fallback="", limit=280).strip()
    utility_goal, brevity_max_words_soft = _local_brevity_profile(mode)
    model = model_for_role("worker")
    timeout_seconds = max(
        5,
        int((os.environ.get("ANDREA_STATEFUL_REALIZATION_TIMEOUT_SECONDS") or "18").strip()),
    )
    extra_mode = ""
    if partial_evidence_realization_enabled() and mode in {
        "partial_evidence_helpful_answer",
        "truthful_fallback_with_next_steps",
    }:
        extra_mode = (
            "6) If ANSWER_MODE is partial or truthful_fallback, end with one or two NEXT_STEP_OPTIONS "
            "phrased naturally—no new options or facts.\n"
        )
    brevity_line = ""
    if brevity_max_words_soft > 0:
        brevity_line = (
            f"7) UTILITY_GOAL is {utility_goal!r}: target roughly {brevity_max_words_soft} words in the main body "
            "(excluding a trailing Next options list). Avoid boilerplate about lookup mechanics.\n"
        )
    system = (
        "You are Andrea. Produce a concise grounded technical answer.\n"
        "Rules:\n"
        "1) Use ONLY facts from EVIDENCE_LINES.\n"
        "2) Lead with PRIMARY_FINDING when present; use SUPPORTING_EVIDENCE_LINES only to tighten clarity.\n"
        "3) If evidence is partial, be explicit about uncertainty and use UNCERTAINTY_BOUNDARY if provided.\n"
        "3) Do NOT invent commands, versions, files, causes, or guarantees.\n"
        "4) Keep it practical and user-facing.\n"
        "5) If evidence is weak, return the fallback.\n"
        f"{extra_mode}"
        f"{brevity_line}"
        "Return JSON with keys: reply (string), grounded (boolean), used_fallback (boolean), anchors_used (array of strings)."
    )
    payload = json.dumps(
        {
            "user_text": str(user_text or "").strip(),
            "answer_family": str(answer_family or "grounded_research").strip(),
            "answer_mode": mode,
            "uncertainty_mode": u_mode,
            "guidance_class": str(guidance_class or "").strip(),
            "primary_finding": primary,
            "supporting_evidence_lines": list(supporting),
            "uncertainty_boundary": uncertainty,
            "utility_goal": utility_goal,
            "brevity_max_words_soft": int(brevity_max_words_soft),
            "next_step_options": list(n_opts),
            "retrieval_source": str(retrieval_source or "").strip(),
            "query": str(query or "").strip(),
            "evidence_lines": safe_evidence,
            "evidence_strength": ev_s,
            "required_anchors": [str(x).strip().lower() for x in required_anchors if str(x).strip()],
            "fallback_reply": fallback,
        },
        ensure_ascii=False,
    )

    def _run_grounded_model(model_name: str) -> dict[str, Any] | None:
        try:
            return _openai_json_chat(
                system=system,
                user=payload,
                model=model_name,
                timeout_seconds=timeout_seconds,
            )
        except Exception:
            return None

    parsed = _run_grounded_model(model)
    if not isinstance(parsed, dict):
        if partial_evidence_realization_enabled() and n_opts and mode != "strong_evidence_answer":
            return _assemble_stateful_with_next_steps(fallback, n_opts)
        return None
    reply = sanitize_user_surface_text(str(parsed.get("reply") or ""), fallback="", limit=1200).strip()
    if not reply:
        if partial_evidence_realization_enabled() and n_opts and mode != "strong_evidence_answer":
            return _assemble_stateful_with_next_steps(fallback, n_opts)
        return None
    if not bool(parsed.get("grounded")):
        if partial_evidence_realization_enabled() and n_opts and mode != "strong_evidence_answer":
            return _assemble_stateful_with_next_steps(fallback, n_opts)
        return None
    if not _evidence_anchor_overlap(reply, safe_evidence):
        if (
            technical_uncertainty_assist_enabled()
            and mode in {"partial_evidence_helpful_answer", "truthful_fallback_with_next_steps"}
        ):
            assist_model = (os.environ.get("ANDREA_DIRECT_OPENAI_MODEL") or "").strip() or model_for_role("verifier")
            if assist_model and assist_model != model:
                parsed_assist = _run_grounded_model(assist_model)
                assist_reply = (
                    sanitize_user_surface_text(str((parsed_assist or {}).get("reply") or ""), fallback="", limit=1200).strip()
                    if isinstance(parsed_assist, dict)
                    else ""
                )
                if (
                    assist_reply
                    and bool((parsed_assist or {}).get("grounded"))
                    and _evidence_anchor_overlap(assist_reply, safe_evidence)
                ):
                    reply = assist_reply
                else:
                    if partial_evidence_realization_enabled() and n_opts and mode != "strong_evidence_answer":
                        return _assemble_stateful_with_next_steps(fallback, n_opts)
                    return None
            else:
                if partial_evidence_realization_enabled() and n_opts and mode != "strong_evidence_answer":
                    return _assemble_stateful_with_next_steps(fallback, n_opts)
                return None
        else:
            if partial_evidence_realization_enabled() and n_opts and mode != "strong_evidence_answer":
                return _assemble_stateful_with_next_steps(fallback, n_opts)
            return None
    req = [str(x).strip().lower() for x in required_anchors if str(x).strip()]
    if req and not _anchors_present_in_reply(reply, req):
        anchors_used = parsed.get("anchors_used")
        if not isinstance(anchors_used, list):
            if partial_evidence_realization_enabled() and n_opts and mode != "strong_evidence_answer":
                return _assemble_stateful_with_next_steps(fallback, n_opts)
            return None
        used_low = {str(x).strip().lower() for x in anchors_used if str(x).strip()}
        if any(a not in used_low for a in req):
            if partial_evidence_realization_enabled() and n_opts and mode != "strong_evidence_answer":
                return _assemble_stateful_with_next_steps(fallback, n_opts)
            return None
    if (
        partial_evidence_realization_enabled()
        and mode in {"partial_evidence_helpful_answer", "truthful_fallback_with_next_steps"}
        and n_opts
        and not _next_step_options_reflected(reply, n_opts)
    ):
        base = reply if _evidence_anchor_overlap(reply, safe_evidence) else fallback
        reply = _assemble_stateful_with_next_steps(base, n_opts)
    if (
        partial_evidence_realization_enabled()
        and mode in {"partial_evidence_helpful_answer", "truthful_fallback_with_next_steps"}
        and n_opts
        and not _specific_next_step_guidance_reflected(reply, n_opts)
    ):
        base = reply if _evidence_anchor_overlap(reply, safe_evidence) else fallback
        reply = _assemble_stateful_with_next_steps(base, n_opts)
    if (
        technical_uncertainty_assist_enabled()
        and mode in {"partial_evidence_helpful_answer", "truthful_fallback_with_next_steps"}
        and (_looks_fallback_shaped_reply(reply) or "general answer" in reply.lower())
    ):
        assist_model = (os.environ.get("ANDREA_DIRECT_OPENAI_MODEL") or "").strip() or model_for_role("verifier")
        if assist_model and assist_model != model:
            parsed_assist = _run_grounded_model(assist_model)
            assist_reply = (
                sanitize_user_surface_text(str((parsed_assist or {}).get("reply") or ""), fallback="", limit=1200).strip()
                if isinstance(parsed_assist, dict)
                else ""
            )
            if (
                assist_reply
                and bool((parsed_assist or {}).get("grounded"))
                and _evidence_anchor_overlap(assist_reply, safe_evidence)
            ):
                reply = assist_reply
                if n_opts and not _specific_next_step_guidance_reflected(reply, n_opts):
                    base = reply if _evidence_anchor_overlap(reply, safe_evidence) else fallback
                    reply = _assemble_stateful_with_next_steps(base, n_opts)
    if ev_s >= 4 and _looks_fallback_shaped_reply(reply) and mode == "partial_evidence_helpful_answer" and n_opts:
        reply = _assemble_stateful_with_next_steps(fallback, n_opts)
    slack = 42
    if brevity_max_words_soft > 0 and _reply_word_count(reply) > brevity_max_words_soft + slack:
        compressed = _compress_reply_to_word_budget(
            reply,
            max_words=brevity_max_words_soft,
            evidence_lines=safe_evidence,
            required_anchors=req,
        )
        if compressed and _reply_word_count(compressed) + 5 < _reply_word_count(reply):
            reply = compressed
            if n_opts and mode != "strong_evidence_answer" and not _next_step_options_reflected(reply, n_opts):
                base = reply if _evidence_anchor_overlap(reply, safe_evidence) else fallback
                reply = _assemble_stateful_with_next_steps(base, n_opts)
    return reply

