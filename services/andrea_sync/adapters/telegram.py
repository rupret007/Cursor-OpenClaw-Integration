"""Map Telegram Bot API Update objects to lockstep commands."""
from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


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
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    message_id = msg.get("message_id")
    from_user = (msg.get("from") or {}).get("id")
    return {
        "command_type": "SubmitUserMessage",
        "channel": "telegram",
        "external_id": str(uid),
        "payload": {
            "text": str(text).strip(),
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
    if reply_to_message_id is not None:
        body["reply_parameters"] = {"message_id": int(reply_to_message_id)}
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
            return json.loads(raw) if raw else {"ok": True}
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
