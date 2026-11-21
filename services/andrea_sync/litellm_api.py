"""OpenAI-compatible chat transport authenticated with Andrea's LiteLLM lane key."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _hosts() -> list[str]:
    hosts = []
    for item in os.environ.get("FO_LITELLM_HOSTS", "").split(","):
        host = item.strip().lower().rstrip(".")
        if not host:
            continue
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not HOST.fullmatch(host) or len(host) > 253:
                raise ValueError("private HTTPS host is not allowlisted")
        hosts.append(host)
    return hosts


def _private(hostname: str, resolve) -> None:
    host = hostname.lower().rstrip(".")
    if host not in _hosts():
        raise ValueError("private HTTPS host is not allowlisted")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        resolver = resolve or socket.getaddrinfo
        try:
            answers = resolver(host, None)
        except OSError:
            raise ValueError("private HTTPS host is not allowlisted") from None
        addresses = []
        for answer in answers:
            try:
                addresses.append(ipaddress.ip_address(answer[4][0]))
            except (TypeError, ValueError, IndexError):
                raise ValueError("private HTTPS host is not allowlisted") from None
    if not addresses or any(
        not ((address.is_loopback or address.is_private) and not address.is_multicast and not address.is_unspecified)
        for address in addresses
    ):
        raise ValueError("private HTTPS host is not allowlisted")


def _credentials(*, resolve=None) -> tuple[str, str] | None:
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
    if url.scheme == "https":
        try:
            _private(host, resolve)
        except ValueError:
            return None
    elif not (url.scheme == "http" and _loopback(host)):
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
