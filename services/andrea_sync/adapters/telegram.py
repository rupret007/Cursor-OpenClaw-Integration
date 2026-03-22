"""Map Telegram Bot API Update objects to lockstep commands."""
from __future__ import annotations

import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

MENTION_RE = re.compile(r"(?<!\w)@(andrea|cursor)\b", re.I)
MODEL_MENTION_RE = re.compile(r"(?<!\w)@(gemini|minimax|openai|gpt)\b", re.I)
COLLABORATION_RE = re.compile(
    r"\b(work together|team up|collaborate|both of you|double-?check|second opinion)\b",
    re.I,
)
FULL_DIALOGUE_RE = re.compile(
    r"\b(full dialogue|near-?full|show (?:me )?(?:the )?(?:dialogue|conversation|back-and-forth|handoffs)|"
    r"show all (?:steps|handoffs|dialogue)|visible collaboration|show the llm dialogue)\b",
    re.I,
)


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def extract_routing_hints(text: str) -> Dict[str, Any]:
    raw_text = str(text or "").strip()
    matches = [m.group(1).lower() for m in MENTION_RE.finditer(raw_text)]
    mention_targets = sorted(set(matches))
    model_aliases = {"gemini": "gemini", "minimax": "minimax", "openai": "openai", "gpt": "openai"}
    model_labels = {"gemini": "Gemini", "minimax": "MiniMax", "openai": "OpenAI"}
    model_mentions: list[str] = []
    preferred_model_family = ""
    for match in MODEL_MENTION_RE.finditer(raw_text):
        family = model_aliases.get(match.group(1).lower(), "")
        if family and family not in model_mentions:
            model_mentions.append(family)
        if not preferred_model_family and family:
            preferred_model_family = family
    preferred_model_label = model_labels.get(preferred_model_family, "")
    routing_hint = "auto"
    if mention_targets == ["andrea"]:
        routing_hint = "andrea"
    elif mention_targets == ["cursor"]:
        routing_hint = "cursor"
    elif mention_targets == ["andrea", "cursor"]:
        routing_hint = "collaborate"
    cleaned = _normalize_spaces(MODEL_MENTION_RE.sub(" ", MENTION_RE.sub(" ", raw_text)))
    collaboration_mode = "auto"
    if routing_hint == "cursor":
        collaboration_mode = "cursor_primary"
    elif routing_hint == "collaborate" or COLLABORATION_RE.search(raw_text):
        collaboration_mode = "collaborative"
    elif routing_hint == "andrea":
        collaboration_mode = "andrea_primary"
    visibility_mode = "summary"
    if FULL_DIALOGUE_RE.search(raw_text):
        visibility_mode = "full"
    return {
        "raw_text": raw_text,
        "routing_text": cleaned,
        "mention_targets": mention_targets,
        "model_mentions": model_mentions,
        "preferred_model_family": preferred_model_family,
        "preferred_model_label": preferred_model_label,
        "routing_hint": routing_hint,
        "collaboration_mode": collaboration_mode,
        "visibility_mode": visibility_mode,
    }


def update_to_command(update: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Return a command body for bus.handle_command, or None if no user text to process.
    """
    uid = update.get("update_id")
    if uid is None:
        return None
    msg = update.get("message") or update.get("edited_message")
    if not isinstance(msg, dict):
        return None
    text = msg.get("text") or msg.get("caption")
    if not text or not str(text).strip():
        return None
    routing = extract_routing_hints(str(text))
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    message_id = msg.get("message_id")
    from_user = (msg.get("from") or {}).get("id")
    return {
        "command_type": "SubmitUserMessage",
        "channel": "telegram",
        "external_id": str(uid),
        "payload": {
            "text": routing["raw_text"],
            "routing_text": routing["routing_text"],
            "mention_targets": routing["mention_targets"],
            "model_mentions": routing["model_mentions"],
            "preferred_model_family": routing["preferred_model_family"],
            "preferred_model_label": routing["preferred_model_label"],
            "routing_hint": routing["routing_hint"],
            "collaboration_mode": routing["collaboration_mode"],
            "visibility_mode": routing["visibility_mode"],
            "chat_id": chat_id,
            "chat_type": chat.get("type"),
            "message_id": message_id,
            "from_user": from_user,
            "from_username": (msg.get("from") or {}).get("username"),
            "auto_cursor_job": False,
        },
    }


def _safe_compare(a: str, b: str) -> bool:
    try:
        return secrets.compare_digest(str(a), str(b))
    except ValueError:
        return False


def verify_webhook_secret(query_secret: str, configured: str) -> bool:
    if not configured:
        return False
    return _safe_compare(query_secret, configured)


def verify_webhook_header(header_value: str, configured: str) -> bool:
    """Telegram sends X-Telegram-Bot-Api-Secret-Token when setWebhook used secret_token."""
    if not configured or not header_value:
        return False
    return _safe_compare(header_value, configured)


def verify_telegram_webhook(
    query_secret: str,
    header_secret: str,
    *,
    query_configured: str,
    header_configured: str,
) -> bool:
    """
    If header_configured is set, accept matching header OR (when query_configured) query param.
    If only query_configured, accept query param only.
    """
    if header_configured:
        if verify_webhook_header(header_secret, header_configured):
            return True
        if query_configured and verify_webhook_secret(query_secret, query_configured):
            return True
        return False
    if query_configured:
        return verify_webhook_secret(query_secret, query_configured)
    return False


def send_text_message(
    *,
    bot_token: str,
    chat_id: int | str,
    text: str,
    reply_to_message_id: Optional[int | str] = None,
    timeout_seconds: int = 20,
) -> Dict[str, Any]:
    if not bot_token.strip():
        raise ValueError("missing Telegram bot token")
    msg = str(text).strip()
    if not msg:
        raise ValueError("message text required")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": msg,
        "disable_web_page_preview": True,
    }
    if reply_to_message_id not in (None, ""):
        try:
            body["reply_parameters"] = {"message_id": int(reply_to_message_id)}
        except (TypeError, ValueError):
            pass
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {"ok": True}
            if isinstance(payload, dict) and payload.get("ok") is False:
                raise RuntimeError(f"telegram sendMessage rejected: {payload}")
            return payload
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        raise RuntimeError(
            f"telegram sendMessage failed (HTTP {err.code}): {payload}"
        ) from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"telegram sendMessage transport error: {err}") from err


def build_webhook_url(public_base: str, secret: str, *, use_query: bool = True) -> str:
    base = public_base.rstrip("/")
    if use_query and secret:
        query = urllib.parse.urlencode({"secret": secret})
        return f"{base}/v1/telegram/webhook?{query}"
    return f"{base}/v1/telegram/webhook"


def normalize_webhook_url(url: str) -> Dict[str, Any]:
    raw = str(url or "").strip()
    if not raw:
        return {}
    parsed = urllib.parse.urlparse(raw)
    query_pairs = []
    for key, values in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items():
        query_pairs.append((str(key), tuple(sorted(str(v) for v in values))))
    return {
        "scheme": parsed.scheme.lower(),
        "netloc": parsed.netloc.lower(),
        "path": parsed.path.rstrip("/") or "/",
        "query": tuple(sorted(query_pairs)),
    }


def webhook_urls_match(current_url: str, expected_url: str) -> bool:
    current = normalize_webhook_url(current_url)
    expected = normalize_webhook_url(expected_url)
    return bool(current and expected and current == expected)


def telegram_api(method: str, token: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        raise RuntimeError(
            f"telegram {method} failed (HTTP {err.code}): {payload}"
        ) from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"telegram {method} transport error: {err}") from err


def telegram_post(method: str, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw": raw}
        raise RuntimeError(
            f"telegram {method} failed (HTTP {err.code}): {data}"
        ) from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"telegram {method} transport error: {err}") from err


def get_webhook_info(bot_token: str) -> Dict[str, Any]:
    return telegram_api("getWebhookInfo", bot_token)


def set_webhook(
    *,
    bot_token: str,
    public_base: str,
    query_secret: str,
    header_secret: str,
    use_query_secret: bool = True,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "url": build_webhook_url(public_base, query_secret, use_query=use_query_secret),
        "drop_pending_updates": False,
    }
    secret_token = header_secret or query_secret
    if secret_token:
        payload["secret_token"] = secret_token[:256]
    return telegram_post("setWebhook", bot_token, payload)
