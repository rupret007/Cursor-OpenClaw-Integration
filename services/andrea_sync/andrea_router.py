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
MEMORY_RE = re.compile(
    r"\b(remember|before|earlier|last time|previous|our chat|our conversation|we talked|continue|resume|pick up)\b",
    re.I,
)
GENERIC_DIRECT_REPLY_RE = re.compile(
    r"(i can help with that directly|i'm here and ready to help|what would you like to do|"
    r"what would you like me to work on|tell me what you need)",
    re.I,
)
META_CURSOR_RE = re.compile(r"\b(talk to cursor|have cursor|use cursor|delegate to cursor)\b", re.I)
HYBRID_SKILL_RE = re.compile(
    r"\b(remind me|reminder|note|notes|calendar|schedule|todo|to-do|task list|message someone|"
    r"send a message|draft a message|email|inbox|search the web|search online|weather|"
    r"summarize this|summarise this)\b",
    re.I,
)
DELEGATE_KEYWORDS_RE = re.compile(
    r"\b(code|repo|repository|file|files|branch|commit|pull request|pr\b|debug|test suite|tests\b|"
    r"implement|implementation|fix|bug|refactor|edit|patch|script|service|restart|reload|deploy|"
    r"openclaw|cursor|traceback|stack trace|lint|unit test|integration test|github)\b",
    re.I,
)
PATH_RE = re.compile(r"[/~][\w.\-~/]+|`[^`]+`|\b\w+\.(py|ts|tsx|js|jsx|md|sh|json|yaml|yml)\b", re.I)
COLLABORATE_RE = re.compile(
    r"\b(work together|team up|collaborate|both of you|double-?check|second opinion)\b",
    re.I,
)


@dataclass
class AndreaRouteDecision:
    mode: str
    reason: str
    reply_text: str = ""
    delegate_target: str = ""
    collaboration_mode: str = "auto"


def _default_delegate_target() -> str:
    raw = (os.environ.get("ANDREA_TELEGRAM_DELEGATE_LANE") or "openclaw_hybrid").strip().lower()
    if raw in {"cursor", "cursor_direct", "direct_cursor"}:
        return "direct_cursor"
    return "openclaw_hybrid"


def classify_route(
    text: str,
    *,
    routing_hint: str = "auto",
    collaboration_mode: str = "auto",
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
    if THANKS_RE.search(clean):
        return "direct", "explicit_andrea_mention" if andrea_preferred else "greeting_or_social", "", "andrea_primary" if andrea_preferred else collab
    if GREETING_RE.search(clean) and not MEMORY_RE.search(clean) and word_count <= 6:
        return "direct", "explicit_andrea_mention" if andrea_preferred else "greeting_or_social", "", "andrea_primary" if andrea_preferred else collab
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
    if IDENTITY_RE.search(clean):
        return "direct", "explicit_andrea_mention" if andrea_preferred else "assistant_identity", "", "andrea_primary" if andrea_preferred else collab
    if HELP_RE.search(clean) and word_count <= 6:
        return "direct", "explicit_andrea_mention" if andrea_preferred else "short_help_request", "", "andrea_primary" if andrea_preferred else collab
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


def _contextual_fallback(text: str, history: list[dict[str, str]] | None = None) -> str:
    clean = _normalize(text)
    hint = _history_hint(history)
    if MEMORY_RE.search(clean):
        if hint:
            return (
                "Yes, I remember the recent conversation in this chat. "
                f"The latest useful context I have is: {hint} What would you like to continue?"
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
    if hint and ("?" in text or len(clean.split()) > 8):
        return (
            "I can answer that using the recent context from this chat. "
            f"The latest useful thread I have is: {hint}"
        )
    return _heuristic_reply(text)


def _heuristic_reply(text: str) -> str:
    clean = _normalize(text)
    if GREETING_RE.search(clean):
        if "how are you" in clean or "how're you" in clean:
            return "Hi! I'm doing well, and I'm ready to help. What would you like me to work on?"
        return "Hi! I'm here and ready to help. What would you like to do?"
    if THANKS_RE.search(clean):
        return "You're welcome. I'm ready for the next thing whenever you are."
    if META_CURSOR_RE.search(clean):
        return (
            "Yes. I can coordinate with Cursor when the work needs heavier repo or coding help, "
            "but I'll answer directly when I can handle it myself."
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


def _openai_direct_reply(text: str, history: list[dict[str, str]] | None = None) -> str:
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
                "Use the recent conversation history when it is relevant, "
                "but do not claim memories beyond what is provided in this chat context. "
                "Keep replies short and useful for chat or voice."
            ),
        }
    ]
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


def build_direct_reply(text: str, history: list[dict[str, str]] | None = None) -> str:
    clean = _normalize(text)
    if (
        (GREETING_RE.search(clean) and not MEMORY_RE.search(clean) and len(clean.split()) <= 6)
        or THANKS_RE.search(clean)
        or IDENTITY_RE.search(clean)
        or META_CURSOR_RE.search(clean)
        or (HELP_RE.search(clean) and len(clean.split()) <= 6)
    ):
        return _heuristic_reply(text)
    try:
        return _openai_direct_reply(text, history=history)
    except Exception:
        return _contextual_fallback(text, history=history)


def route_message(
    text: str,
    history: list[dict[str, str]] | None = None,
    *,
    routing_hint: str = "auto",
    collaboration_mode: str = "auto",
) -> AndreaRouteDecision:
    mode, reason, delegate_target, resolved_collab = classify_route(
        text,
        routing_hint=routing_hint,
        collaboration_mode=collaboration_mode,
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
        reply_text=build_direct_reply(text, history=history),
        collaboration_mode=resolved_collab,
    )
