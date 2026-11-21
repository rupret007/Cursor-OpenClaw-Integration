#!/usr/bin/env python3
"""Queue a review lane on Hatchet, fall back to Restate, and never dispatch one twice."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHA = re.compile(r"[0-9a-f]{40}")
TENANT = re.compile(r"[0-9a-fA-F-]{8,64}")
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
LOOPBACK = ("localhost", "127.0.0.1", "::1")
TIMEOUT = 15
MAX_BODY = 64 * 1024
# A rejection the primary definitely did not accept. Anything else must not fail over.
FAILOVER_STATUS = (429, 500, 502, 503, 504)


class NoFailover(Exception):
    """The lane must not move to the fallback: the primary may have accepted it, or it is misconfigured."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def canonical(request):
    if set(request) != {"lane", "head", "pull_request"}:
        raise ValueError("A lane request names its lane, head and pull request")
    if not NAME.fullmatch(str(request["lane"])) or not SHA.fullmatch(str(request["head"])):
        raise ValueError("A lane request needs a lane name and a full head commit")
    if type(request["pull_request"]) is not int or not 0 < request["pull_request"] < 1 << 31:
        raise ValueError("A lane request needs a pull request number")
    return json.dumps(request, sort_keys=True, separators=(",", ":"))


def idempotency_key(request):
    return "fo-lane-" + hashlib.sha256(canonical(request).encode()).hexdigest()[:32]


def endpoint(base_url):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    url = urlsplit(base_url)
    if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None or
            url.password is not None or url.path not in ("", "/") or url.query or url.fragment or
            (url.scheme == "http" and url.hostname not in LOOPBACK)):
        raise ValueError("Use a loopback queue URL or a private HTTPS root")
    return base_url.rstrip("/"), opener


def secret(name):
    if not ENV_NAME.fullmatch(name):
        raise ValueError("A queue credential is named by an environment variable")
    value = os.environ.get(name, "")
    if not value or len(value) > 4096 or not value.isprintable():
        raise ValueError("A queue credential is missing from the environment")
    return value


def post(opener, url, headers, payload):
    """Return the decoded body, or raise Uncertain when acceptance cannot be determined."""
    request = Request(url, data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", **headers})
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            raw = response.read(MAX_BODY + 1)
    except HTTPError as error:
        error.close()
        if error.code in FAILOVER_STATUS:
            raise ValueError(f"The queue refused the lane with HTTP {error.code}") from None
        raise Uncertain(f"The queue answered HTTP {error.code}") from None
    except (socket.timeout, TimeoutError):
        raise Uncertain("The queue did not answer in time") from None
    except URLError as error:
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            raise Uncertain("The queue did not answer in time") from None
        raise ValueError("The queue was unreachable") from None
    except OSError:
        raise Uncertain("The queue connection failed after the request was sent") from None
    if len(raw) > MAX_BODY:
        raise Uncertain("The queue returned an oversized response")
    try:
        body = json.loads(raw)
    except ValueError:
        raise Uncertain("The queue returned an unreadable response") from None
    if not isinstance(body, dict):
        raise Uncertain("The queue returned an unexpected response")
    return body


def run_identifier(value):
    if not isinstance(value, str) or not RUN_ID.fullmatch(value):
        raise Uncertain("The queue accepted the lane without a usable run identifier")
    return value


def hatchet(config, request, key):
    base, opener = endpoint(config["base_url"])
    tenant = config["tenant"]
    workflow = config["workflow"]
    if not TENANT.fullmatch(tenant) or not NAME.fullmatch(workflow):
        raise ValueError("The Hatchet lane queue needs a tenant and a workflow name")
    body = post(opener, f"{base}/api/v1/stable/tenants/{quote(tenant)}/workflow-runs/trigger",
                {"Authorization": "Bearer " + secret(config["token_env"])},
                {"workflowName": workflow, "input": request,
                 "additionalMetadata": {"idempotencyKey": key}})
    run = body.get("run") if isinstance(body.get("run"), dict) else body
    return run_identifier((run.get("metadata") or {}).get("id"))


def restate(config, request, key):
    base, opener = endpoint(config["base_url"])
    service, handler = config["service"], config["handler"]
    if not NAME.fullmatch(service) or not NAME.fullmatch(handler):
        raise ValueError("The Restate lane queue needs a service and a handler")
    headers = {"idempotency-key": key}
    if config.get("token_env"):
        headers["Authorization"] = "Bearer " + secret(config["token_env"])
    body = post(opener, f"{base}/restate/send/{quote(service)}/{quote(handler)}", headers, request)
    if body.get("status") not in ("Accepted", "PreviouslyAccepted"):
        raise Uncertain("Restate did not accept the lane")
    return run_identifier(body.get("invocationId"))


def ledger_entries(path):
    try:
        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    with os.fdopen(handle, encoding="utf-8") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("The lane ledger must be a private owner file")
        if info.st_size > 4 * 1024 * 1024:
            raise ValueError("The lane ledger is too large to trust")
        return {row["key"]: row for row in map(json.loads, file.read().splitlines()) if row.get("key")}


def record(path, entry):
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(handle, "a", encoding="utf-8") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        file.write(json.dumps(entry, sort_keys=True) + "\n")
        file.flush()
        os.fsync(file.fileno())


def dispatch(config, request, ledger, now=None):
    """Dispatch one lane exactly once. Only a definite refusal moves it to the fallback."""
    key = idempotency_key(request)
    ledger = Path(ledger)
    existing = ledger_entries(ledger).get(key)
    if existing is not None:
        return {**existing, "reused": True}
    primary, fallback = config["hatchet"], config["restate"]
    try:
        backend, run = "hatchet", hatchet(primary, request, key)
    except Uncertain as uncertain:
        # Failing over here could run the same lane twice. Stop and let an operator look.
        record(ledger, {"key": key, "backend": "hatchet", "run": None, "uncertain": str(uncertain),
                        "at": int(now or time.time())})
        raise ValueError(f"Hatchet outcome is unknown: {uncertain}. The lane was not failed over.") from None
    except ValueError as refusal:
        try:
            backend, run = "restate", restate(fallback, request, key)
        except Uncertain as uncertain:
            raise ValueError(f"Hatchet refused the lane ({refusal}) and the Restate outcome "
                             f"is unknown: {uncertain}") from None
        except ValueError as second:
            raise ValueError(f"Hatchet refused the lane ({refusal}) and Restate refused it "
                             f"({second}). The lane was not queued.") from None
    entry = {"key": key, "backend": backend, "run": run, "lane": request["lane"],
             "head": request["head"], "at": int(now or time.time())}
    record(ledger, entry)
    return {**entry, "reused": False}


def load_config(path):
    text = Path(path).read_text(encoding="utf-8")
    config = json.loads(text)
    if not isinstance(config, dict) or set(config) != {"hatchet", "restate"}:
        raise ValueError("The queue policy names its Hatchet primary and its Restate fallback")
    for section in config.values():
        if not isinstance(section, dict) or not isinstance(section.get("base_url"), str):
            raise ValueError("Each queue backend needs a reviewed base URL")
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("policy", type=Path, help="Reviewed JSON queue policy")
    parser.add_argument("ledger", type=Path, help="Private append-only dispatch ledger")
    parser.add_argument("--lane", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--pull-request", type=int, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        request = {"lane": args.lane, "head": args.head, "pull_request": args.pull_request}
        result = dispatch(load_config(args.policy), request, args.ledger)
        verb = "was already queued on" if result["reused"] else "is queued on"
        print(f"Lane {result['lane']} {verb} {result['backend']} as {result['run']}.")
        return 0
    except (OSError, KeyError, TypeError, ValueError) as error:
        message = str(error) if type(error) is ValueError else "The lane queue operation failed"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
