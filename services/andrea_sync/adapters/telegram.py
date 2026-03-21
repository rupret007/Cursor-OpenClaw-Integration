"""Map Telegram Bot API Update objects to lockstep commands."""
from __future__ import annotations

import secrets
from typing import Any, Dict, Optional, Tuple


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
    from_user = (msg.get("from") or {}).get("id")
    return {
        "command_type": "SubmitUserMessage",
        "channel": "telegram",
        "external_id": str(uid),
        "payload": {
            "text": str(text).strip(),
            "chat_id": chat_id,
            "from_user": from_user,
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
