"""Andrea-first routing: direct assistant reply vs Cursor delegation."""
from __future__ import annotations

import json
import os
import re
import ast
import urllib.error
import urllib.request
from dataclasses import dataclass

from .orchestration_boundary import should_answer_before_delegate
from .user_surface import is_stale_openclaw_narrative, sanitize_user_surface_text


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(1, int(raw.strip()))
    except ValueError:
        return default


def _normalize(text: str) -> str:
    raw = str(text or "").strip()
    # Normalize smart apostrophes so regexes match common mobile punctuation.
    raw = raw.replace("’", "'").replace("`", "'")
    return re.sub(r"\s+", " ", raw).lower()


GREETING_RE = re.compile(
    r"\b(hi|hello|hey|good morning|good afternoon|good evening|how are you|how're you)\b",
    re.I,
)
THANKS_RE = re.compile(r"\b(thanks|thank you|appreciate it)\b", re.I)
IDENTITY_RE = re.compile(r"\b(who are you|what can you do|what do you do)\b", re.I)
HELP_RE = re.compile(r"^(help|help me|help please|i need help)\b", re.I)
NEWS_RE = re.compile(r"\b(news|headline|headlines)\b", re.I)
MEMORY_RE = re.compile(
    r"\b(remember|before|earlier|last time|previous|our chat|our conversation|we talked|continue|resume|pick up)\b",
    re.I,
)
GENERIC_DIRECT_REPLY_RE = re.compile(
    r"(i can help with that directly|i'm here and ready to help|what would you like to do|"
    r"what would you like me to work on|tell me what you need|"
    r"bring in cursor when the task needs deeper|deeper technical work\. tell me what you need)",
    re.I,
)
GENERIC_DIRECT_REPLY_FALLBACK_RE = re.compile(
    r"\b("
    r"say a bit more about what you want|"
    r"tell me what you need|"
    r"what would you like to (?:do|work on)|"
    r"i can help with that directly"
    r")\b",
    re.I,
)
# Coordination / capability questions — not actionable "have Cursor fix X" instructions.
META_CURSOR_RE = re.compile(
    r"\b("
    r"talk to cursor(?:\s+when|\s+if needed)?|"
    r"delegate to cursor(?:\s+when|\s+if needed)?|"
    r"coordinate with cursor|"
    r"work with cursor when|"
    r"cursor when needed"
    r")\b",
    re.I,
)
# Explicit instruction to hand work to Cursor without an @mention.
CURSOR_EXPLICIT_ACTION_RE = re.compile(
    r"\b("
    r"(?:have|ask)\s+cursor\s+(?:to\s+)?(?:fix|debug|implement|change|update|refactor|patch|review|inspect)\b|"
    r"use\s+cursor\s+(?:to|for)\s+(?:fix|debug|implement|change|update|refactor|patch|review|inspect)\b"
    r")\b",
    re.I,
)
# Simple questions about the stack (OpenClaw / Cursor / who is speaking).
META_STACK_STANDALONE_RE = re.compile(
    r"^(?:" + r"|".join(
        [
            r"what (?:is|'s) openclaw\??",
            r"what (?:is|'s) cursor\??",
            r"who(?:'s| is) answering\??",
            r"who answered\??",
            r"is (?:this|that) (?:the )?openclaw\??",
            r"is openclaw (?:in there|there)\??",
            r"openclaw are you there\??",
            r"is this (?:really )?andrea\??",
        ]
    )
    + r")$",
    re.I,
)
META_STACK_INLINE_RE = re.compile(
    r"\b("
    r"are you (?:using|on|in) (?:cursor|openclaw)\b|"
    r"(?:which|what) (?:llm|model) (?:is )?(?:this|answering|replying)\b"
    r")\b",
    re.I,
)
META_OPENCLAW_RE = re.compile(
    r"^(?:"
    r"what (?:is|'s) openclaw|"
    r"is (?:this|that) (?:the )?openclaw|"
    r"is openclaw (?:in there|there)|"
    r"openclaw are you there"
    r")$",
    re.I,
)
META_CURSOR_REPLIES_RE = re.compile(
    r"^what (?:is|'s) cursor$",
    re.I,
)
META_ANSWERING_RE = re.compile(
    r"^(?:"
    r"who(?:'s| is) answering|"
    r"who answered|"
    r"is this (?:really )?andrea|"
    r"(?:which|what) (?:llm|model) (?:is )?(?:this|answering|replying)"
    r")$",
    re.I,
)
HYBRID_SKILL_RE = re.compile(
    r"\b(remind me|reminder|note|notes|calendar|schedule|todo|to-do|task list|message someone|"
    r"send a message|draft a message|email|inbox|search the web|search online|"
    r"summarize this|summarise this)\b",
    re.I,
)
DELEGATE_KEYWORDS_RE = re.compile(
    r"\b(code|repo|repository|file|files|branch|commit|pull request|pr\b|debug|test suite|tests\b|"
    r"implement|implementation|fix|bug|refactor|edit|patch|script|service|restart|reload|deploy|"
    r"traceback|stack trace|lint|unit test|integration test|github)\b",
    re.I,
)
PATH_RE = re.compile(r"[/~][\w.\-~/]+|`[^`]+`|\b\w+\.(py|ts|tsx|js|jsx|md|sh|json|yaml|yml)\b", re.I)
COLLABORATE_RE = re.compile(
    r"\b(work together|team up|collaborate|both of you|double-?check|second opinion)\b",
    re.I,
)
CASUAL_CHECKIN_RE = re.compile(
    r"^(?:how(?:'s|\s+is)\s+it\s+going|how\s+are\s+things|how(?:'s|\s+is)\s+everything)\s*[?.!]*\s*$",
    re.I,
)
AGENDA_OR_DAY_PLAN_RE = re.compile(
    r"\b("
    r"what(?:'s|s|\s+is)?\s+on\s+(?:the\s+)?agenda|"
    r"what(?:'s|s|\s+is)\s+on\s+(?:for\s+)?today|"
    r"on\s+(?:the\s+)?agenda\s+today|"
    r"anything\s+on\s+(?:the\s+)?agenda|"
    r"my\s+agenda|"
    r"(?:the\s+)?day'?s\s+plan|"
    r"plan\s+for\s+today|"
    r"what\s+are\s+my\s+plans\s+today|"
    r"what'?s\s+on\s+my\s+schedule\s+today|"
    r"what\s+is\s+on\s+my\s+schedule\s+today|"
    r"what\s+do\s+i\s+have\s+today"
    r")\b",
    re.I,
)
# Shared copy for agenda guardrails (router + server repair).
DIRECT_AGENDA_NO_CALENDAR_REPLY = (
    "I don't have a connected calendar view in this chat, so I can't see your real schedule here. "
    "Tell me what you're trying to get done today, or ask for reminders or status on something specific."
)
# Attention/triage lane: honest no-signal copy (router + composer empty-state).
DIRECT_ATTENTION_NO_STATE_REPLY = (
    "Nothing urgent is surfacing from current reminders and follow-through right now. "
    "I can check a specific project or help you plan the rest of today."
)

OPINION_OR_TAKE_RE = re.compile(
    r"\b("
    r"what(?:'s|s|\s+do)\s+you\s+think|"
    r"what(?:'s|s|\s+is)\s+your\s+take|"
    r"your\s+(?:opinion|view)\b|"
    r"how\s+do\s+you\s+feel\s+about"
    r")\b",
    re.I,
)


def _meta_stack_question(clean: str, original: str) -> bool:
    """True for short identity/stack questions; false when a path/file reference implies repo work."""
    if PATH_RE.search(str(original or "")):
        return False
    trimmed = clean.rstrip("?.! ").strip()
    if META_STACK_INLINE_RE.search(clean):
        return True
    return bool(META_STACK_STANDALONE_RE.match(trimmed))


def _strip_social_prefix(text: str) -> str:
    trimmed = _normalize(text)
    patterns = (
        r"^(?:hi|hello|hey)\b[\s,!.:-]*",
        r"^(?:good morning|good afternoon|good evening)\b[\s,!.:-]*",
    )
    for _ in range(3):
        original = trimmed
        for pattern in patterns:
            trimmed = re.sub(pattern, "", trimmed, count=1).strip(" ,.!?:;-")
        trimmed = re.sub(r"^(?:@?andrea)\b[\s,!.:-]*", "", trimmed, count=1).strip(" ,.!?:;-")
        if trimmed == original:
            break
    return trimmed


def _is_greeting_only(text: str) -> bool:
    clean = _normalize(text)
    if not GREETING_RE.search(clean) or MEMORY_RE.search(clean):
        return False
    remainder = _strip_social_prefix(clean)
    if remainder in {"", "andrea", "how are you", "how're you"}:
        return True
    if (
        (remainder.startswith("how are you") or remainder.startswith("how're you"))
        and len(remainder.split()) <= 4
    ):
        return True
    return False


def is_standalone_casual_social_turn(text: str) -> bool:
    """
    Short greeting or casual check-in that should stay on the direct conversational path
    and must not merge onto an active Telegram task via continuation.
    """
    clean = _normalize(text)
    if not clean or MEMORY_RE.search(clean):
        return False
    if len(clean.split()) > 8:
        return False
    if _is_greeting_only(clean):
        return True
    trimmed = clean.rstrip("?.! ").strip()
    return bool(CASUAL_CHECKIN_RE.match(trimmed))


@dataclass
class AndreaRouteDecision:
    mode: str
    reason: str
    reply_text: str = ""
    delegate_target: str = ""
    collaboration_mode: str = "auto"


def _default_delegate_target() -> str:
    """OpenClaw-first delegation; Cursor is escalated inside the hybrid lane or via @Cursor."""
    return "openclaw_hybrid"


def classify_route(
    text: str,
    *,
    routing_hint: str = "auto",
    collaboration_mode: str = "auto",
    preferred_model_family: str = "",
) -> tuple[str, str, str, str]:
    clean = _normalize(text)
    word_count = len(clean.split())
    hint = str(routing_hint or "auto").strip().lower() or "auto"
    collab = str(collaboration_mode or "auto").strip().lower() or "auto"
    andrea_preferred = hint == "andrea"
    if hint == "cursor":
        return "delegate", "explicit_cursor_mention", "openclaw_hybrid", "cursor_primary"
    if hint == "collaborate":
        return "delegate", "explicit_collaboration_mention", "openclaw_hybrid", "collaborative"
    if not clean:
        return "direct", "explicit_andrea_mention" if andrea_preferred else "empty_or_whitespace", "", "andrea_primary" if andrea_preferred else collab
    if is_standalone_casual_social_turn(text):
        return "direct", "explicit_andrea_mention" if andrea_preferred else "greeting_or_social", "", "andrea_primary" if andrea_preferred else collab
    if CURSOR_EXPLICIT_ACTION_RE.search(clean):
        if collab == "auto" and COLLABORATE_RE.search(clean):
            collab = "collaborative"
        if andrea_preferred:
            return "delegate", "explicit_andrea_mention_delegate", "openclaw_hybrid", "cursor_primary"
        if collab == "auto":
            collab = "cursor_primary"
        return "delegate", "explicit_cursor_work_request", "openclaw_hybrid", collab
    if _meta_stack_question(clean, text):
        return (
            "direct",
            "explicit_andrea_mention" if andrea_preferred else "stack_or_tooling_question",
            "",
            "andrea_primary" if andrea_preferred else collab,
        )
    if META_CURSOR_RE.search(clean):
        return "direct", "explicit_andrea_mention" if andrea_preferred else "cursor_coordination_question", "", "andrea_primary" if andrea_preferred else collab
    if HYBRID_SKILL_RE.search(clean):
        if collab == "auto" and COLLABORATE_RE.search(clean):
            collab = "collaborative"
        if andrea_preferred:
            return "delegate", "explicit_andrea_mention_delegate", "openclaw_hybrid", "andrea_primary"
        return "delegate", "openclaw_hybrid_request", "openclaw_hybrid", collab
    # Status/history follow-ups stay direct even when repo-ish keywords appear later.
    if should_answer_before_delegate(text):
        return (
            "direct",
            "explicit_andrea_mention" if andrea_preferred else "answer_before_delegate",
            "",
            "andrea_primary" if andrea_preferred else collab,
        )
    if DELEGATE_KEYWORDS_RE.search(clean):
        if collab == "auto" and COLLABORATE_RE.search(clean):
            collab = "collaborative"
        if andrea_preferred:
            return "delegate", "explicit_andrea_mention_delegate", "openclaw_hybrid", "andrea_primary"
        return "delegate", "technical_or_repo_request", _default_delegate_target(), collab
    if PATH_RE.search(text):
        if collab == "auto" and COLLABORATE_RE.search(clean):
            collab = "collaborative"
        if andrea_preferred:
            return "delegate", "explicit_andrea_mention_delegate", "openclaw_hybrid", "andrea_primary"
        return "delegate", "path_or_code_reference", _default_delegate_target(), collab
    if THANKS_RE.search(clean):
        return "direct", "explicit_andrea_mention" if andrea_preferred else "greeting_or_social", "", "andrea_primary" if andrea_preferred else collab
    if IDENTITY_RE.search(clean):
        return "direct", "explicit_andrea_mention" if andrea_preferred else "assistant_identity", "", "andrea_primary" if andrea_preferred else collab
    if HELP_RE.search(clean) and word_count <= 6:
        return "direct", "explicit_andrea_mention" if andrea_preferred else "short_help_request", "", "andrea_primary" if andrea_preferred else collab
    if preferred_model_family:
        return "delegate", "explicit_model_mention", "openclaw_hybrid", "andrea_primary" if collab == "auto" else collab
    if word_count <= 18:
        return "direct", "explicit_andrea_mention" if andrea_preferred else "short_general_request", "", "andrea_primary" if andrea_preferred else collab
    if word_count >= 45:
        if collab == "auto" and COLLABORATE_RE.search(clean):
            collab = "collaborative"
        if andrea_preferred:
            return "delegate", "explicit_andrea_mention_delegate", "openclaw_hybrid", "andrea_primary"
        return "delegate", "longer_multi_step_request", _default_delegate_target(), collab
    return "direct", "explicit_andrea_mention" if andrea_preferred else "balanced_default_direct", "", "andrea_primary" if andrea_preferred else collab


def _finalize_direct_surface_reply(
    text: str,
    *,
    user_seed: str,
    history: list[dict[str, str]] | None = None,
) -> str:
    """Last-line guard: never return stale/runtime/provider chatter from direct or fallback paths."""
    raw = str(text or "").strip()
    safe = sanitize_user_surface_text(raw, fallback="", limit=2000)
    if safe and not is_stale_openclaw_narrative(raw):
        return safe
    return _heuristic_reply(user_seed, history=history)


def _clip(text: str, limit: int = 220) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _history_hint(history: list[dict[str, str]] | None) -> str:
    if not history:
        return ""
    for turn in reversed(history):
        if (
            turn.get("role") == "assistant"
            and turn.get("content")
            and not GENERIC_DIRECT_REPLY_RE.search(str(turn.get("content") or ""))
        ):
            cleaned = sanitize_user_surface_text(
                _clip(turn["content"], 180), fallback="", limit=180
            )
            if cleaned:
                return cleaned
    for turn in reversed(history):
        if turn.get("role") == "user" and turn.get("content"):
            cleaned = sanitize_user_surface_text(
                _clip(turn["content"], 180), fallback="", limit=180
            )
            if cleaned:
                return cleaned
    for turn in reversed(history):
        if turn.get("role") == "assistant" and turn.get("content"):
            cleaned = sanitize_user_surface_text(
                _clip(turn["content"], 180), fallback="", limit=180
            )
            if cleaned:
                return cleaned
    return ""


def _memory_hint(memory_notes: list[str] | None) -> str:
    if not memory_notes:
        return ""
    for note in memory_notes:
        cleaned = sanitize_user_surface_text(_clip(note, 180), fallback="", limit=180)
        if cleaned:
            return cleaned
    return ""


def _contextual_fallback(
    text: str,
    history: list[dict[str, str]] | None = None,
    memory_notes: list[str] | None = None,
) -> str:
    clean = _normalize(text)
    hint = _history_hint(history)
    memory_hint = _memory_hint(memory_notes)
    if MEMORY_RE.search(clean):
        if hint and memory_hint:
            reply = (
                "Yes. I remember the recent conversation and I also have a durable note for this principal. "
                f"Recent thread: {hint} Durable note: {memory_hint}"
            )
            safe = sanitize_user_surface_text(reply, fallback="", limit=2000)
            return safe or _heuristic_reply(text, history=history)
        if hint:
            reply = (
                "Yes, I remember the recent conversation in this chat. "
                f"The latest useful context I have is: {hint} What would you like to continue?"
            )
            safe = sanitize_user_surface_text(reply, fallback="", limit=2000)
            return safe or _heuristic_reply(text, history=history)
        if memory_hint:
            reply = (
                "Yes. I have a durable note for this principal, even if this specific chat thread is light. "
                f"The strongest saved note I have is: {memory_hint}"
            )
            safe = sanitize_user_surface_text(reply, fallback="", limit=2000)
            return safe or _heuristic_reply(text, history=history)
        return (
            "I can remember the recent conversation in this chat once we build a little history together. "
            "Tell me what you want to continue, and I'll pick it up from there."
        )
    if "anything else" in clean or "say more" in clean or "tell me more" in clean:
        if hint:
            reply = (
                "Yes. Building on what we were just discussing, "
                f"the most relevant recent context is: {hint}"
            )
            safe = sanitize_user_surface_text(reply, fallback="", limit=2000)
            return safe or _heuristic_reply(text, history=history)
        return _heuristic_reply(text, history=history)
    if OPINION_OR_TAKE_RE.search(clean) and (
        bool(re.search(r"\bthat\b", clean)) or bool(re.search(r"\bthis\b", clean))
    ):
        if hint and len(hint) > 12:
            reply = (
                f"Given what we were just discussing ({hint[:160]}), I'd take it seriously—"
                "want me to go sharper or more cautious?"
            )
            safe = sanitize_user_surface_text(reply, fallback="", limit=2000)
            return safe or _heuristic_reply(text, history=history)
        reply = "Which part do you mean—something we were just discussing, or something else?"
        safe = sanitize_user_surface_text(reply, fallback="", limit=2000)
        return safe or _heuristic_reply(text, history=history)
    return _heuristic_reply(text, history=history)


def _heuristic_reply(text: str, history: list[dict[str, str]] | None = None) -> str:
    clean = _normalize(text)
    trimmed = clean.rstrip("?.! ").strip()
    if CASUAL_CHECKIN_RE.match(trimmed):
        return (
            "Pretty good, thanks for asking. How are you doing?"
        )
    if _is_greeting_only(clean):
        if "how are you" in clean or "how're you" in clean:
            return (
                "Hi! I'm doing well, thanks for asking. What's on your mind?"
            )
        return "Hi! Good to hear from you. How can I help?"
    if AGENDA_OR_DAY_PLAN_RE.search(clean):
        return DIRECT_AGENDA_NO_CALENDAR_REPLY
    lightweight_convo = _lightweight_conversational_reply(clean)
    if lightweight_convo:
        return lightweight_convo
    if OPINION_OR_TAKE_RE.search(clean):
        h = _history_hint(history)
        if h and len(h) > 12 and (
            bool(re.search(r"\bthat\b", clean)) or bool(re.search(r"\bthis\b", clean))
        ):
            return (
                f"Given what we were discussing ({h[:160]}), I'd weigh it carefully—happy to go deeper on any angle."
            )
        if bool(re.search(r"\bthat\b", clean)) or bool(re.search(r"\bthis\b", clean)):
            return (
                "Which part should I weigh in on—something from our last exchange, or a new topic?"
            )
        return (
            "Happy to share a take—give me the topic or a bit of context and I'll keep it concise."
        )
    if THANKS_RE.search(clean):
        return "You're welcome. I'm ready for the next thing whenever you are."
    if META_CURSOR_RE.search(clean):
        return (
            "Yes. I can bring Cursor in when the work needs heavier repo or coding help. "
            "If you use @Cursor, I handle that routing for you behind the scenes, and you do not need "
            "to manage session keys, labels, or other runtime details."
        )
    if META_CURSOR_REPLIES_RE.match(trimmed):
        return (
            "Cursor is the execution lane I use for heavier repo and coding work. "
            "I keep lightweight questions with Andrea directly, and I only bring Cursor in when the task "
            "needs deeper technical changes."
        )
    if META_OPENCLAW_RE.match(trimmed):
        return (
            "You're talking with Andrea. OpenClaw is the collaboration layer I can use when deeper "
            "reasoning helps, but lightweight questions like this stay direct with me."
        )
    if META_ANSWERING_RE.match(trimmed):
        return (
            "Right now, Andrea is answering you directly. "
            "I only bring OpenClaw or Cursor in when the task needs deeper reasoning or heavier repo execution."
        )
    if NEWS_RE.search(clean):
        return (
            "For live headlines I need the web-news lane your setup uses; if that's enabled, ask the same "
            "in your usual chat and I'll pull a short grounded update. "
            "Otherwise tell me a topic and I can give a quick general snapshot with the caveat it may not be live."
        )
    if _meta_stack_question(clean, text):
        return (
            "You're talking with Andrea. OpenClaw is a collaboration layer I can use when deeper "
            "reasoning helps, and Cursor is the heavy execution lane for repo and coding work. "
            "I keep the internal plumbing behind the scenes, and lightweight questions like this stay direct."
        )
    if HELP_RE.search(clean) and len(clean.split()) <= 6:
        return (
            "Absolutely—what would you like to tackle first?"
        )
    if IDENTITY_RE.search(clean):
        return (
            "I'm Andrea, your assistant here. I answer everyday questions directly and can help with "
            "reminders, messages, quick lookups, and heavier projects when you need that."
        )
    return (
        "I'm here. Say a bit more about what you want and I'll take it from there."
    )


def _safe_eval_arithmetic(expr: str) -> float | None:
    allowed_binops = (ast.Add, ast.Sub, ast.Mult, ast.Div)
    allowed_unary = (ast.UAdd, ast.USub)
    try:
        tree = ast.parse(expr, mode="eval")
    except Exception:
        return None

    def _walk(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _walk(node.body)
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, allowed_binops):
                raise ValueError("unsupported-op")
            left = _walk(node.left)
            right = _walk(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            return left / right
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, allowed_unary):
                raise ValueError("unsupported-unary")
            val = _walk(node.operand)
            return val if isinstance(node.op, ast.UAdd) else -val
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        raise ValueError("unsupported-node")

    try:
        return _walk(tree)
    except Exception:
        return None


def _format_numeric_reply(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text else str(value)


def _simple_direct_utility_reply(text: str) -> str:
    clean = _normalize(text).strip()
    if not clean:
        return ""
    math_match = re.match(
        r"^\s*(?:what(?:'s|\s+is)\s+)?((?:-?\d+(?:\.\d+)?\s*[\+\-\*/]\s*)+-?\d+(?:\.\d+)?)\s*\??\s*$",
        clean,
        re.I,
    )
    if math_match:
        value = _safe_eval_arithmetic(math_match.group(1))
        if value is not None:
            return _format_numeric_reply(value)

    gb_from_mb = re.search(
        r"\bhow\s+many\s+(?:gigs?|gb)\s+are\s+in\s+(\d+(?:\.\d+)?)\s*(?:mb|mib)\b",
        clean,
        re.I,
    )
    if gb_from_mb:
        mb = float(gb_from_mb.group(1))
        gb = mb / 1024.0
        return f"{_format_numeric_reply(gb)} GB"

    mb_to_gb = re.search(
        r"\bconvert\s+(\d+(?:\.\d+)?)\s*(?:mb|mib)\s+to\s+(?:gigs?|gb)\b",
        clean,
        re.I,
    )
    if mb_to_gb:
        mb = float(mb_to_gb.group(1))
        gb = mb / 1024.0
        return f"{_format_numeric_reply(gb)} GB"
    return ""


def _lightweight_conversational_reply(text: str) -> str:
    clean = _normalize(text).strip()
    if not clean:
        return ""
    if re.search(
        r"\b(what(?:'s|\s+is)\s+(?:the\s+)?(?:meaning|purpose)\s+of\s+life|why\s+do\s+we\s+exist)\b",
        clean,
        re.I,
    ):
        return "42."
    return ""


def _scrub_history_for_direct(history: list[dict[str, str]] | None) -> list[dict[str, str]]:
    """Drop poisoned assistant turns and strip internal lines before OpenAI direct."""
    if not history:
        return []
    out: list[dict[str, str]] = []
    for turn in history:
        raw = str(turn.get("content") or "").strip()
        if not raw:
            continue
        role = "assistant" if turn.get("role") == "assistant" else "user"
        if is_stale_openclaw_narrative(raw):
            continue
        if role == "assistant":
            content = sanitize_user_surface_text(raw, fallback="", limit=2000)
            if not content:
                continue
        else:
            content = raw
        out.append({"role": role, "content": content})
    return out


def _openai_direct_reply(
    text: str,
    history: list[dict[str, str]] | None = None,
    memory_notes: list[str] | None = None,
    *,
    turn_domain: str = "",
    context_boundary: str = "",
    inject_durable_memory: bool = True,
) -> str:
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key or not _env_truthy("OPENAI_API_ENABLED", False):
        raise RuntimeError("openai_direct_disabled")
    model = (os.environ.get("ANDREA_DIRECT_OPENAI_MODEL") or "gpt-4o-mini").strip()
    history_turns = _env_int("ANDREA_DIRECT_HISTORY_TURNS", 6)
    timeout_seconds = max(
        5,
        int((os.environ.get("ANDREA_DIRECT_OPENAI_TIMEOUT_SECONDS") or "25").strip()),
    )
    clean_user = _normalize(str(text).strip())
    news_extra = ""
    if NEWS_RE.search(clean_user):
        news_extra = (
            " The user is asking about current news or headlines: do not answer by recycling unrelated "
            "past assistant messages about projects, sprints, or tool handoffs; answer briefly from general "
            "knowledge or ask what topic or region they want."
        )
    boundary_extra = ""
    domain = str(turn_domain or "").strip()
    if domain == "external_information":
        boundary_extra = (
            " This turn is external-information domain: avoid project status, approval queue, receipts, "
            "or personal memory unless the user explicitly asks for those."
        )
    elif domain in {"project_status", "approval_state"}:
        boundary_extra = (
            " This turn is project continuity domain: prefer concrete status, blockers, and next actions "
            "over generic assistant fallback language."
        )
    elif domain == "casual_conversation":
        boundary_extra = (
            " This turn is casual conversation: keep the answer warm, brief, and avoid runtime or repo details."
        )
    elif domain == "personal_agenda":
        boundary_extra = (
            " This turn is personal-agenda or schedule: do not substitute project goals, sprint status, "
            "or approvals for the user's calendar or day plan; if you lack schedule data, say so clearly."
        )
    elif domain == "attention_today":
        boundary_extra = (
            " This turn is attention/triage for today: prioritize user-action items, due or at-risk work, "
            "and reminders over passive project summaries; do not substitute unrelated memory or history."
        )
    elif domain == "opinion_reflection":
        boundary_extra = (
            " This turn asks for an opinion or reflection: ground the answer in the recent thread when "
            "the user refers to 'that' or 'this', not in unrelated project continuity."
        )
    if context_boundary:
        boundary_extra += f" Context boundary: {context_boundary}."
    messages = [
        {
            "role": "system",
            "content": (
                "You are Andrea, a warm and capable personal assistant. "
                "Answer directly, naturally, and concisely. "
                "Do not mention Cursor unless the user asks. "
                "Never mention session IDs, session labels, tool configuration flags, or runtime internals. "
                "Never echo embedding-quota, memory-quota, vector-store, or active-context errors from prior "
                "turns as if they were facts about the user's question. "
                "If the user asks about Cursor or collaboration, explain it in product terms instead. "
                "Use the recent conversation history when it is relevant, "
                "but do not claim memories beyond what is provided in this chat context. "
                "Do not treat unrelated prior assistant turns as the answer to a new question. "
                "Keep replies short and useful for chat or voice."
                f"{news_extra}{boundary_extra}"
            ),
        }
    ]
    durable_notes = (
        [str(note or "").strip() for note in (memory_notes or []) if str(note or "").strip()]
        if inject_durable_memory
        else []
    )
    if durable_notes:
        messages.append(
            {
                "role": "system",
                "content": "Durable user context:\n- " + "\n- ".join(durable_notes[:4]),
            }
        )
    scrubbed = _scrub_history_for_direct(history)
    for turn in scrubbed[-history_turns:]:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        content = str(turn.get("content") or "").strip()
        if content:
            messages.append({"role": role, "content": content})
    payload = {
        "model": model,
        "temperature": 0.4,
        "max_tokens": 220,
        "messages": [*messages, {"role": "user", "content": str(text).strip()}],
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
        raise RuntimeError(f"openai_direct_http_{err.code}:{raw[:300]}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"openai_direct_transport:{err}") from err
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("openai_direct_no_choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        content = "\n".join(p for p in parts if p.strip())
    text_out = str(content or "").strip()
    if not text_out:
        raise RuntimeError("openai_direct_empty_content")
    safe = sanitize_user_surface_text(text_out, fallback="", limit=2000)
    if not safe or is_stale_openclaw_narrative(text_out):
        raise RuntimeError("openai_direct_contaminated")
    return safe


def build_direct_reply(
    text: str,
    history: list[dict[str, str]] | None = None,
    memory_notes: list[str] | None = None,
    *,
    turn_domain: str = "",
    context_boundary: str = "",
    inject_durable_memory: bool = True,
) -> str:
    clean = _normalize(text)
    lightweight_convo = _lightweight_conversational_reply(clean)
    if lightweight_convo:
        return lightweight_convo
    utility_direct = _simple_direct_utility_reply(clean)
    if utility_direct:
        return utility_direct
    has_memory = bool(
        inject_durable_memory and memory_notes and any(str(n).strip() for n in memory_notes)
    )
    greeting_short = _is_greeting_only(clean) and len(clean.split()) <= 6
    # Longer turns with principal memory should reach the model/heuristic fallback chain
    # instead of the ultra-short greeting fast-path.
    if has_memory and len(clean.split()) > 8:
        greeting_short = False
    if (
        greeting_short
        or THANKS_RE.search(clean)
        or IDENTITY_RE.search(clean)
        or META_CURSOR_RE.search(clean)
        or _meta_stack_question(clean, text)
        or (HELP_RE.search(clean) and len(clean.split()) <= 6 and not has_memory)
    ):
        return _finalize_direct_surface_reply(
            _heuristic_reply(text, history=history),
            user_seed=text,
            history=history,
        )
    try:
        reply = _finalize_direct_surface_reply(
            _openai_direct_reply(
                text,
                history=history,
                memory_notes=memory_notes,
                turn_domain=turn_domain,
                context_boundary=context_boundary,
                inject_durable_memory=inject_durable_memory,
            ),
            user_seed=text,
            history=history,
        )
    except Exception:
        mem_fb = memory_notes if inject_durable_memory else []
        reply = _finalize_direct_surface_reply(
            _contextual_fallback(text, history=history, memory_notes=mem_fb),
            user_seed=text,
            history=history,
        )
    domain = str(turn_domain or "").strip()
    mem_ctx = memory_notes if inject_durable_memory else []
    # Domain-aware guardrail: never emit the weak generic fallback for these lanes.
    if domain == "casual_conversation" and is_generic_direct_reply(reply):
        if is_standalone_casual_social_turn(text):
            return "Pretty good, thanks for asking. How are you doing?"
        return _finalize_direct_surface_reply(
            _contextual_fallback(text, history=history, memory_notes=mem_ctx),
            user_seed=text,
            history=history,
        )
    if domain == "personal_agenda" and is_generic_direct_reply(reply):
        return DIRECT_AGENDA_NO_CALENDAR_REPLY
    if domain == "attention_today" and is_generic_direct_reply(reply):
        return DIRECT_ATTENTION_NO_STATE_REPLY
    if domain == "opinion_reflection" and is_generic_direct_reply(reply):
        return _finalize_direct_surface_reply(
            _contextual_fallback(text, history=history, memory_notes=mem_ctx),
            user_seed=text,
            history=history,
        )
    if domain == "external_information" and is_generic_direct_reply(reply):
        return _finalize_direct_surface_reply(
            _heuristic_reply(text, history=history),
            user_seed=text,
            history=history,
        )
    return reply


def route_message(
    text: str,
    history: list[dict[str, str]] | None = None,
    *,
    routing_hint: str = "auto",
    collaboration_mode: str = "auto",
    preferred_model_family: str = "",
    memory_notes: list[str] | None = None,
    turn_domain: str = "",
    context_boundary: str = "",
    inject_durable_memory: bool = True,
) -> AndreaRouteDecision:
    mode, reason, delegate_target, resolved_collab = classify_route(
        text,
        routing_hint=routing_hint,
        collaboration_mode=collaboration_mode,
        preferred_model_family=preferred_model_family,
    )
    if mode == "delegate":
        return AndreaRouteDecision(
            mode="delegate",
            reason=reason,
            delegate_target=delegate_target,
            collaboration_mode=resolved_collab,
        )
    return AndreaRouteDecision(
        mode="direct",
        reason=reason,
        reply_text=build_direct_reply(
            text,
            history=history,
            memory_notes=memory_notes,
            turn_domain=turn_domain,
            context_boundary=context_boundary,
            inject_durable_memory=inject_durable_memory,
        ),
        collaboration_mode=resolved_collab,
    )


def is_generic_direct_reply(text: str) -> bool:
    clean = str(text or "").strip()
    if not clean:
        return True
    return bool(GENERIC_DIRECT_REPLY_FALLBACK_RE.search(clean))
