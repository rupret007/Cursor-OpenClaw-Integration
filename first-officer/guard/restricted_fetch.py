#!/usr/bin/env python3
"""Fetch one allowlisted page and return it inside a fence.

An empty allowlist refuses every fetch. Hosts must match exactly after IDNA
normalization. Public pages are HTTPS on port 443 and must resolve to public
unicast addresses. The only loopback form is an explicit fixture origin,
`http://127.0.0.1:<port>`. One redirect is followed only when that next page is
an exact source for the same route. Any other redirect, including a second hop,
is refused before that URL is contacted. A private address or a page that
cannot be fenced is refused, and the body is not returned. The connection uses
the addresses from that check and does not resolve the name again.
"""

import importlib.util
import ipaddress
from pathlib import Path
import re
import socket
from urllib import error, parse, request


def _fence():
    path = Path(__file__).resolve().parent / "sanitize.py"
    spec = importlib.util.spec_from_file_location("fo_guard_sanitize", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.fence


fence = _fence()


def _pinhttp():
    path = Path(__file__).resolve().parents[1] / "pinhttp.py"
    spec = importlib.util.spec_from_file_location("fo_fetch_pinhttp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pinhttp = _pinhttp()


LIMIT = 256 * 1024
TEXT_LIMIT = 8 * 1024
KINDS = ("text/html", "text/plain")
REDIRECTS = {301, 302, 303, 307, 308}
HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)


def normalize_host(host):
    if not isinstance(host, str) or not host or "/" in host:
        raise ValueError("The fetch URL is not allowed")
    try:
        return host.encode("idna").decode("ascii").rstrip(".").lower()
    except UnicodeError:
        raise ValueError("The fetch URL is not allowed") from None


def load_allowlist(document):
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("The fetch allowlist is unreadable")
    hosts = document.get("hosts", [])
    fixtures = document.get("fixtures", [])
    sources = document.get("sources", {})
    extra = set(document) - {"version", "hosts", "fixtures", "sources"}
    if (extra or not isinstance(hosts, list) or not isinstance(fixtures, list)
            or not isinstance(sources, dict)):
        raise ValueError("The fetch allowlist is unreadable")
    clean_hosts = []
    for host in hosts:
        if not isinstance(host, str) or ":" in host or host.startswith("."):
            raise ValueError("The fetch allowlist is unreadable")
        normalized = normalize_host(host)
        if not HOST.fullmatch(normalized):
            raise ValueError("The fetch allowlist is unreadable")
        if normalized in clean_hosts:
            raise ValueError("The fetch allowlist repeats a host")
        clean_hosts.append(normalized)
    clean_fixtures = []
    for item in fixtures:
        parts = parse.urlsplit(item if isinstance(item, str) else "")
        if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port is None
                or parts.path not in ("", "/") or parts.query or parts.fragment or parts.username):
            raise ValueError("A fetch fixture must be loopback HTTP")
        origin = f"http://127.0.0.1:{parts.port}"
        if origin in clean_fixtures:
            raise ValueError("The fetch allowlist repeats a fixture")
        clean_fixtures.append(origin)
    clean_sources = {}
    for route, urls in sources.items():
        if route not in ("structured_live_news", "structured_grounded_research") or not isinstance(urls, list):
            raise ValueError("The fetch allowlist is unreadable")
        clean_urls = []
        for url in urls:
            clean_urls.append(_source_url(url, clean_hosts, clean_fixtures))
        if len(clean_urls) != len(set(clean_urls)):
            raise ValueError("The fetch allowlist repeats a source")
        clean_sources[route] = tuple(clean_urls)
    return {"hosts": tuple(clean_hosts), "fixtures": tuple(clean_fixtures), "sources": clean_sources}


def _source_url(url, hosts, fixtures):
    """A route source is an exact allowlisted page. The query does not choose it."""
    parts = parse.urlsplit(url if isinstance(url, str) else "")
    if (parts.username or parts.password or parts.query or parts.fragment
            or not parts.hostname or parts.scheme not in ("http", "https")):
        raise ValueError("The fetch allowlist is unreadable")
    if parts.scheme == "http":
        origin = f"http://127.0.0.1:{parts.port}"
        if parts.hostname != "127.0.0.1" or parts.port is None or origin not in fixtures:
            raise ValueError("The fetch allowlist is unreadable")
        if parts.path in ("",) or not parts.path.startswith("/"):
            raise ValueError("The fetch allowlist is unreadable")
        return url
    host = normalize_host(parts.hostname)
    if parts.port not in (None, 443) or host not in hosts or not parts.path.startswith("/"):
        raise ValueError("The fetch allowlist is unreadable")
    return url


def public_unicast(raw):
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return address.is_global


def system_resolve(host):
    try:
        found = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError("The fetch URL is not allowed") from None
    return [item[4][0] for item in found]


def check_target(url, allowlist, resolve):
    parts = parse.urlsplit(url if isinstance(url, str) else "")
    if (parts.username or parts.password or parts.query or parts.fragment
            or not parts.hostname or parts.scheme not in ("http", "https")):
        raise ValueError("The fetch URL is not allowed")
    host = normalize_host(parts.hostname)
    if parts.scheme == "http":
        origin = f"http://127.0.0.1:{parts.port}"
        if host != "127.0.0.1" or parts.port is None or origin not in allowlist["fixtures"]:
            raise ValueError("The fetch URL is not allowed")
        return ["127.0.0.1"]
    if parts.port not in (None, 443) or host not in allowlist["hosts"]:
        raise ValueError("The fetch URL is not allowed")
    found = resolve(host)
    if not isinstance(found, list) or not found or any(not public_unicast(item) for item in found):
        raise ValueError("The fetch URL is not allowed")
    try:
        return [str(ipaddress.ip_address(item)) for item in found]
    except ValueError:
        raise ValueError("The fetch URL is not allowed") from None


def visible_text(page):
    if "<<END " in page or "<<UNTRUSTED " in page:
        raise ValueError("The untrusted text closed its fence")
    text = re.sub(r"(?is)<!--.*?-->", " ", page)
    text = re.sub(r"(?is)<script\b[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style\b[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?is)<script\b[^>]*>.*", " ", text)
    text = re.sub(r"(?is)<style\b[^>]*>.*", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class _StopRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(url, addresses):
    opener = pinhttp.opener(parse.urlsplit(url).scheme, addresses, _StopRedirect)
    try:
        response = opener.open(request.Request(url, headers={"Accept": "text/plain, text/html"}), timeout=5)
    except error.HTTPError as failure:
        if failure.code in REDIRECTS:
            location = failure.headers.get("Location")
            failure.close()
            if not location:
                raise ValueError("The page was not read") from None
            return None, parse.urljoin(url, location)
        failure.close()
        raise ValueError("The page was not read") from None
    except error.URLError:
        raise ValueError("The page was not read") from None
    with response:
        if response.status != 200:
            raise ValueError("The page was not read")
        kind = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if kind not in KINDS:
            raise ValueError("The page was not read")
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > LIMIT):
            raise ValueError("The page is too large")
        chunks = []
        total = 0
        while True:
            block = response.read(65536)
            if not block:
                break
            total += len(block)
            if total > LIMIT:
                raise ValueError("The page is too large")
            chunks.append(block)
    try:
        return b"".join(chunks).decode("utf-8"), None
    except UnicodeError:
        raise ValueError("The page was not read") from None


def _redirect_ok(current, target, allowlist):
    """A hop is allowed only when both pages are exact sources of one route."""
    sources = allowlist.get("sources") or {}
    for urls in sources.values():
        if current in urls and target in urls:
            return True
    return False


def fetch(url, allowlist, resolve=system_resolve, token=None):
    current = url
    page = None
    for hop in range(2):
        pinned = check_target(current, allowlist, resolve)
        page, redirect = _open(current, pinned)
        if redirect is None:
            break
        if hop == 1 or not _redirect_ok(current, redirect, allowlist):
            raise ValueError("The fetch URL is not allowed")
        current = redirect
    if page is None:
        raise ValueError("The page was not read")
    text = visible_text(page)
    if not text:
        raise ValueError("The page was not read")
    return fence("fetch", text, TEXT_LIMIT, token)
