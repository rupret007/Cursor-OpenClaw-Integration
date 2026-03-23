"""Andrea-first routing: direct assistant reply vs Cursor delegation."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass


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
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


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
    r"what would you like me to work on|tell me what you need)",
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
    r"send a message|draft a message|email|inbox|search the web|search online|weather|"
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
    if _is_greeting_only(clean) and word_count <= 6:
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
            return _clip(turn["content"], 180)
    for turn in reversed(history):
        if turn.get("role") == "user" and turn.get("content"):
            return _clip(turn["content"], 180)
    for turn in reversed(history):
        if turn.get("role") == "assistant" and turn.get("content"):
            return _clip(turn["content"], 180)
    return ""


def _memory_hint(memory_notes: list[str] | None) -> str:
    if not memory_notes:
        return ""
    for note in memory_notes:
        clipped = _clip(note, 180)
        if clipped:
            return clipped
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
            return (
                "Yes. I remember the recent conversation and I also have a durable note for this principal. "
                f"Recent thread: {hint} Durable note: {memory_hint}"
            )
        if hint:
            return (
                "Yes, I remember the recent conversation in this chat. "
                f"The latest useful context I have is: {hint} What would you like to continue?"
            )
        if memory_hint:
            return (
                "Yes. I have a durable note for this principal, even if this specific chat thread is light. "
                f"The strongest saved note I have is: {memory_hint}"
            )
        return (
            "I can remember the recent conversation in this chat once we build a little history together. "
            "Tell me what you want to continue, and I'll pick it up from there."
        )
    if "anything else" in clean or "say more" in clean or "tell me more" in clean:
        if hint:
            return (
                "Yes. Building on what we were just discussing, "
                f"the most relevant recent context is: {hint}"
            )
        return "Yes. Tell me what direction you want to go next, and I'll expand from there."
    return _heuristic_reply(text)


def _heuristic_reply(text: str) -> str:
    clean = _normalize(text)
    trimmed = clean.rstrip("?.! ").strip()
    if _is_greeting_only(clean):
        if "how are you" in clean or "how're you" in clean:
            return "Hi! I'm doing well, and I'm ready to help. What would you like me to work on?"
        return "Hi! I'm here and ready to help. What would you like to do?"
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
            "I can help with current news. Tell me the topic or place you want, "
            "and I'll focus the update there."
        )
    if _meta_stack_question(clean, text):
        return (
            "You're talking with Andrea. OpenClaw is a collaboration layer I can use when deeper "
            "reasoning helps, and Cursor is the heavy execution lane for repo and coding work. "
            "I keep the internal plumbing behind the scenes, and lightweight questions like this stay direct."
        )
    if HELP_RE.search(clean) and len(clean.split()) <= 6:
        return (
            "Absolutely. Tell me what you want to get done, and I'll either handle it directly "
            "or bring in Cursor if the work needs deeper technical help."
        )
    if IDENTITY_RE.search(clean):
        return (
            "I'm Andrea, your personal assistant layer here. I handle direct assistant requests myself "
            "and bring in Cursor when the task needs deeper technical or repo work."
        )
    return (
        "I can help with that directly when it's lightweight, and I'll bring in Cursor when the task "
        "needs deeper technical work. Tell me what you need."
    )


def _openai_direct_reply(
    text: str,
    history: list[dict[str, str]] | None = None,
    memory_notes: list[str] | None = None,
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
    messages = [
        {
            "role": "system",
            "content": (
                "You are Andrea, a warm and capable personal assistant. "
                "Answer directly, naturally, and concisely. "
                "Do not mention Cursor unless the user asks. "
                "Never mention session IDs, session labels, tool configuration flags, or runtime internals. "
                "If the user asks about Cursor or collaboration, explain it in product terms instead. "
                "Use the recent conversation history when it is relevant, "
                "but do not claim memories beyond what is provided in this chat context. "
                "Keep replies short and useful for chat or voice."
            ),
        }
    ]
    durable_notes = [str(note or "").strip() for note in (memory_notes or []) if str(note or "").strip()]
    if durable_notes:
        messages.append(
            {
                "role": "system",
                "content": "Durable user context:\n- " + "\n- ".join(durable_notes[:4]),
            }
        )
    for turn in (history or [])[-history_turns:]:
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
    return text_out


def build_direct_reply(
    text: str,
    history: list[dict[str, str]] | None = None,
    memory_notes: list[str] | None = None,
) -> str:
    clean = _normalize(text)
    if (
        (_is_greeting_only(clean) and len(clean.split()) <= 6)
        or THANKS_RE.search(clean)
        or IDENTITY_RE.search(clean)
        or META_CURSOR_RE.search(clean)
        or NEWS_RE.search(clean)
        or _meta_stack_question(clean, text)
        or (HELP_RE.search(clean) and len(clean.split()) <= 6)
    ):
        return _heuristic_reply(text)
    try:
        return _openai_direct_reply(text, history=history, memory_notes=memory_notes)
    except Exception:
        return _contextual_fallback(text, history=history, memory_notes=memory_notes)


def route_message(
    text: str,
    history: list[dict[str, str]] | None = None,
    *,
    routing_hint: str = "auto",
    collaboration_mode: str = "auto",
    preferred_model_family: str = "",
    memory_notes: list[str] | None = None,
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
        reply_text=build_direct_reply(text, history=history, memory_notes=memory_notes),
        collaboration_mode=resolved_collab,
    )
