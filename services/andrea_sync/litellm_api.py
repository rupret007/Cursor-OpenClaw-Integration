"""OpenAI-compatible chat transport authenticated with Andrea's LiteLLM lane key."""
from __future__ import annotations

import ipaddress
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _credentials() -> tuple[str, str] | None:
    key = (os.environ.get("ANDREA_LITELLM_API_KEY") or "").strip()
    model = (os.environ.get("ANDREA_LITELLM_MODEL") or "").strip()
    base = (os.environ.get("ANDREA_LITELLM_BASE_URL") or "").strip().rstrip("/")
    if not (base and key and model):
        return None
    try:
        url = urllib.parse.urlsplit(base)
        host = url.hostname
        port = url.port
    except ValueError:
        return None
    if not host or url.username is not None or url.query or url.fragment or port == 0:
        return None
    if url.scheme != "https" and not (url.scheme == "http" and _loopback(host)):
        return None
    if not url.path.endswith("/v1"):
        base += "/v1"
    return base + "/chat/completions", key


def litellm_configured() -> bool:
    return _credentials() is not None


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_RejectRedirect())


def litellm_chat(
    payload: Mapping[str, Any], *, timeout_seconds: int, error_prefix: str
) -> Any:
    credentials = _credentials()
    if credentials is None:
        raise RuntimeError(f"{error_prefix}_proxy_unconfigured")
    endpoint, key = credentials
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with _opener.open(req, timeout=timeout_seconds) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        code = err.code
        err.close()
        raise RuntimeError(f"{error_prefix}_http_{code}") from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError(f"{error_prefix}_transport") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError(f"{error_prefix}_invalid_response") from None
