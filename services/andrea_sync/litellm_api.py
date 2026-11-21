"""OpenAI-compatible chat transport authenticated with Andrea's LiteLLM lane key.

The request connects to the addresses that passed the endpoint check and does
not resolve the name again.
"""
from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
from pathlib import Path
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


def _pinhttp():
    path = Path(__file__).resolve().parents[2] / "first-officer" / "pinhttp.py"
    spec = importlib.util.spec_from_file_location("fo_andrea_pinhttp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pinhttp = _pinhttp()


def _private(hostname: str, resolve) -> list[str]:
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
    return [str(address) for address in addresses]


def _localhost(resolve) -> list[str]:
    resolver = resolve or socket.getaddrinfo
    try:
        answers = resolver("localhost", None)
    except OSError:
        raise ValueError("private HTTPS host is not allowlisted") from None
    pins = []
    for answer in answers:
        try:
            address = ipaddress.ip_address(answer[4][0])
        except (TypeError, ValueError, IndexError):
            raise ValueError("private HTTPS host is not allowlisted") from None
        if not address.is_loopback:
            raise ValueError("private HTTPS host is not allowlisted")
        pins.append(str(address))
    if not pins:
        raise ValueError("private HTTPS host is not allowlisted")
    return pins


def _target(*, resolve=None) -> tuple[str, str, list[str]] | None:
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
    try:
        if url.scheme == "https":
            pins = _private(host, resolve)
        elif url.scheme == "http" and host == "localhost":
            pins = _localhost(resolve)
        elif url.scheme == "http" and _loopback(host):
            pins = [str(ipaddress.ip_address(host))]
        else:
            return None
    except ValueError:
        return None
    if not url.path.endswith("/v1"):
        base += "/v1"
    return base + "/chat/completions", key, pins


def _credentials(*, resolve=None) -> tuple[str, str] | None:
    found = _target(resolve=resolve)
    if found is None:
        return None
    endpoint, key, _pins = found
    return endpoint, key


def litellm_configured() -> bool:
    return _credentials() is not None


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def litellm_chat(
    payload: Mapping[str, Any], *, timeout_seconds: int, error_prefix: str
) -> Any:
    target = _target()
    if target is None:
        raise RuntimeError(f"{error_prefix}_proxy_unconfigured")
    endpoint, key, pins = target
    opener = pinhttp.opener(urllib.parse.urlsplit(endpoint).scheme, pins, _RejectRedirect)
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
        with opener.open(req, timeout=timeout_seconds) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        code = err.code
        err.close()
        raise RuntimeError(f"{error_prefix}_http_{code}") from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError(f"{error_prefix}_transport") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError(f"{error_prefix}_invalid_response") from None
