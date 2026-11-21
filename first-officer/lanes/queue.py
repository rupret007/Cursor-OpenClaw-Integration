#!/usr/bin/env python3
"""Queue one review lane on Hatchet, and use Restate only when Hatchet definitely refuses it.

Hatchet is the primary. Its trigger is the documented stable REST call. Restate is the
documented fallback ingress: ``POST /restate/send/{service}/{handler}`` with an
``idempotency-key``. Those two backends do not share that key, so a timeout, HTTP 408,
any 5xx, a dropped connection, or any answer that is not a definite refusal stays on
Hatchet and is never sent to Restate. The primary may already have accepted the lane.
The private ledger is append-only. A pending or unknown row blocks a second send until
an operator has checked the backend. The request connects to the addresses that passed
that check and does not resolve the name again.
"""

import argparse
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import importlib.util
import ipaddress
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
from urllib.request import HTTPRedirectHandler, Request


NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
SHA = re.compile(r"[0-9a-f]{40}")
TENANT = re.compile(r"[0-9a-fA-F-]{8,64}")
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
LOOPBACK = ("localhost", "127.0.0.1", "::1")
TIMEOUT = 15
MAX_BODY = 64 * 1024
MAX_LEDGER = 4 * 1024 * 1024
# Statuses the backend answered and the lane was definitely not accepted.
# HTTP 408 and 5xx are ambiguous: Hatchet may already hold the lane, and Restate
# does not share Hatchet's idempotency check, so they never fail over.
FAILOVER_STATUS = (425, 429)
HATCHET_FIELDS = ("base_url", "tenant", "workflow", "token_env")
RESTATE_FIELDS = ("base_url", "service", "handler", "token_env")


def _pinhttp():
    path = Path(__file__).resolve().parents[1] / "pinhttp.py"
    spec = importlib.util.spec_from_file_location("fo_queue_pinhttp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pinhttp = _pinhttp()


class Refusal(Exception):
    """The backend answered, or never connected, and did not accept the lane."""


class Uncertain(Exception):
    """The backend may have accepted the lane. Do not send it anywhere else."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "The queue redirected the request", headers, fp)


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


def queue_hosts():
    hosts = []
    for item in os.environ.get("FO_QUEUE_HOSTS", "").split(","):
        host = item.strip().lower().rstrip(".")
        if not host:
            continue
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not HOST.fullmatch(host) or len(host) > 253:
                raise ValueError("Use a loopback queue URL or a private HTTPS root")
        hosts.append(host)
    return hosts


def _usable_private(address):
    return (address.is_loopback or address.is_private) and not address.is_multicast and not address.is_unspecified


def _private_named_host(hostname, resolve=None):
    """A named HTTPS queue host must be allowlisted and resolve only to private addresses."""
    host = hostname.lower().rstrip(".")
    if host not in queue_hosts():
        raise ValueError("Use a loopback queue URL or a private HTTPS root")
    resolver = resolve or socket.getaddrinfo
    try:
        answers = resolver(host, None)
    except OSError:
        raise ValueError("Use a loopback queue URL or a private HTTPS root") from None
    addresses = []
    for answer in answers:
        try:
            addresses.append(ipaddress.ip_address(answer[4][0]))
        except (TypeError, ValueError, IndexError):
            raise ValueError("Use a loopback queue URL or a private HTTPS root") from None
    if not addresses or any(not _usable_private(address) for address in addresses):
        raise ValueError("Use a loopback queue URL or a private HTTPS root")
    return [str(address) for address in addresses]


def _loopback_name(hostname, resolve):
    if hostname != "localhost":
        raise ValueError("Use a loopback queue URL or a private HTTPS root")
    resolver = resolve or socket.getaddrinfo
    try:
        answers = resolver(hostname, None)
    except OSError:
        raise ValueError("Use a loopback queue URL or a private HTTPS root") from None
    pins = []
    for answer in answers:
        try:
            address = ipaddress.ip_address(answer[4][0])
        except (TypeError, ValueError, IndexError):
            raise ValueError("Use a loopback queue URL or a private HTTPS root") from None
        if not address.is_loopback:
            raise ValueError("Use a loopback queue URL or a private HTTPS root")
        pins.append(str(address))
    if not pins:
        raise ValueError("Use a loopback queue URL or a private HTTPS root")
    return pins


def endpoint(base_url, resolve=None):
    url = urlsplit(base_url)
    if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None or
            url.password is not None or url.path not in ("", "/") or url.query or url.fragment):
        raise ValueError("Use a loopback queue URL or a private HTTPS root")
    try:
        address = ipaddress.ip_address(url.hostname)
    except ValueError:
        address = None
    if url.scheme == "http":
        if url.hostname not in LOOPBACK:
            raise ValueError("Use a loopback queue URL or a private HTTPS root")
        pins = _loopback_name(url.hostname, resolve) if address is None else [str(address)]
    elif address is None:
        pins = _private_named_host(url.hostname, resolve)
    elif not _usable_private(address):
        raise ValueError("Use a loopback queue URL or a private HTTPS root")
    else:
        pins = [str(address)]
    return base_url.rstrip("/"), pinhttp.opener(url.scheme, pins, NoRedirect)


def secret(name):
    if not ENV_NAME.fullmatch(name):
        raise ValueError("A queue credential is named by an environment variable")
    value = os.environ.get(name, "")
    if not value or len(value) > 4096 or not value.isprintable() or any(ch.isspace() for ch in value):
        raise ValueError("A queue credential is missing from the environment")
    return value


def post(opener, url, headers, payload, timeout):
    """Return the decoded object, raise Refusal when nothing was accepted, or Uncertain."""
    request = Request(url, data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", **headers})
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as error:
        error.close()
        if error.code in FAILOVER_STATUS:
            raise Refusal(f"The queue refused the lane with HTTP {error.code}") from None
        raise Uncertain(f"The queue answered HTTP {error.code}") from None
    except (socket.timeout, TimeoutError):
        raise Uncertain("The queue did not answer in time") from None
    except URLError as error:
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            raise Uncertain("The queue did not answer in time") from None
        if isinstance(error.reason, (ConnectionRefusedError, socket.gaierror)):
            raise Refusal("The queue was unreachable") from None
        raise Uncertain("The queue connection failed after the request was sent") from None
    except OSError:
        raise Uncertain("The queue connection failed after the request was sent") from None
    if response is None:
        raise Uncertain("The queue gave no answer")
    try:
        if getattr(response, "status", 200) >= 300:
            raise Uncertain(f"The queue answered HTTP {response.status}")
        raw = response.read(MAX_BODY + 1)
    finally:
        response.close()
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


def hatchet(config, request, key, timeout, token):
    base, opener = endpoint(config["base_url"])
    tenant, workflow = config["tenant"], config["workflow"]
    if not TENANT.fullmatch(tenant) or not NAME.fullmatch(workflow):
        raise ValueError("The Hatchet lane queue needs a tenant and a workflow name")
    body = post(opener, f"{base}/api/v1/stable/tenants/{quote(tenant)}/workflow-runs/trigger",
                {"Authorization": "Bearer " + token},
                {"workflowName": workflow, "input": request,
                 "additionalMetadata": {"idempotencyKey": key}}, timeout)
    run = body.get("run") if isinstance(body.get("run"), dict) else body
    return run_identifier((run.get("metadata") or {}).get("id"))


def restate(config, request, key, timeout, token):
    base, opener = endpoint(config["base_url"])
    service, handler = config["service"], config["handler"]
    if not NAME.fullmatch(service) or not NAME.fullmatch(handler):
        raise ValueError("The Restate lane queue needs a service and a handler")
    body = post(opener, f"{base}/restate/send/{quote(service)}/{quote(handler)}",
                {"Authorization": "Bearer " + token, "idempotency-key": key},
                request, timeout)
    if body.get("status") not in ("Accepted", "PreviouslyAccepted"):
        raise Uncertain("Restate did not accept the lane")
    return run_identifier(body.get("invocationId"))


def _check_ledger(info):
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("The lane ledger must be a private owner file")
    if info.st_size > MAX_LEDGER:
        raise ValueError("The lane ledger is too large to trust")


@contextmanager
def locked_ledger(path):
    try:
        handle = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("The lane ledger must be a private owner file") from None
        raise
    file = os.fdopen(handle, "r+", encoding="utf-8")
    try:
        info = os.fstat(file.fileno())
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_size == 0:
            os.fchmod(file.fileno(), 0o600)
            info = os.fstat(file.fileno())
        _check_ledger(info)
        fcntl.flock(file, fcntl.LOCK_EX)
        yield file
    finally:
        file.close()


def latest(file, key):
    file.seek(0)
    found = None
    for line in file.read().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or row.get("key") != key:
            if not isinstance(row, dict) or not isinstance(row.get("key"), str):
                raise ValueError("The lane ledger has an unreadable row")
            continue
        found = row
    return found


def append_entry(file, entry):
    file.seek(0, os.SEEK_END)
    file.write(json.dumps(entry, sort_keys=True) + "\n")
    file.flush()
    os.fsync(file.fileno())


def _bounded_timeout(timeout):
    if isinstance(timeout, bool) or type(timeout) not in (int, float) or not 0 < timeout <= TIMEOUT:
        raise ValueError("The queue timeout must be within the reviewed limit")
    return timeout


def dispatch(config, request, ledger, now=None, timeout=TIMEOUT):
    """Dispatch one lane once. Only a definite Hatchet refusal moves it to Restate."""
    timeout = _bounded_timeout(timeout)
    if now is None:
        moment = int(time.time())
    elif type(now) is int and 0 <= now < 1 << 42:
        moment = now
    else:
        raise ValueError("The queue clock is unusable")
    key = idempotency_key(request)
    primary, fallback = config["hatchet"], config["restate"]
    # A missing credential is misconfiguration, not a refusal, so it must not fail over.
    hatchet_token = secret(primary["token_env"])
    restate_token = secret(fallback["token_env"])
    with locked_ledger(ledger) as file:
        state = latest(file, key)
        if state and state.get("phase") == "queued" and state.get("run"):
            return {**state, "reused": True}
        if state and state.get("phase") != "refused":
            raise ValueError("The earlier queue attempt has an unknown outcome. "
                             "The lane was not sent again.")

        def note(phase, backend, run=None, detail=None):
            entry = {"at": moment, "backend": backend, "head": request["head"], "key": key,
                     "lane": request["lane"], "phase": phase, "run": run}
            if detail:
                entry["detail"] = detail
            append_entry(file, entry)
            return entry

        note("pending", "hatchet")
        try:
            run = hatchet(primary, request, key, timeout, hatchet_token)
        except Uncertain as uncertain:
            note("uncertain", "hatchet", detail=str(uncertain))
            raise ValueError(f"Hatchet outcome is unknown: {uncertain}. "
                             "The lane was not failed over.") from None
        except Refusal as refusal:
            note("refused", "hatchet", detail=str(refusal))
            note("pending", "restate")
            try:
                run = restate(fallback, request, key, timeout, restate_token)
            except Uncertain as uncertain:
                note("uncertain", "restate", detail=str(uncertain))
                raise ValueError(f"Hatchet refused the lane ({refusal}) and the Restate outcome "
                                 f"is unknown: {uncertain}") from None
            except Refusal as second:
                note("refused", "restate", detail=str(second))
                raise ValueError(f"Hatchet refused the lane ({refusal}) and Restate refused it "
                                 f"({second}). The lane was not queued.") from None
            entry = note("queued", "restate", run)
            return {**entry, "reused": False}
        entry = note("queued", "hatchet", run)
        return {**entry, "reused": False}


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict) or set(config) != {"hatchet", "restate"}:
        raise ValueError("The queue policy names its Hatchet primary and its Restate fallback")
    expected = {"hatchet": HATCHET_FIELDS, "restate": RESTATE_FIELDS}
    for name, fields in expected.items():
        section = config[name]
        if not isinstance(section, dict) or set(section) != set(fields):
            raise ValueError(f"The {name} queue policy is incomplete")
        if not all(isinstance(section[field], str) for field in fields):
            raise ValueError(f"The {name} queue policy is incomplete")
        endpoint(section["base_url"])
        if not ENV_NAME.fullmatch(section["token_env"]):
            raise ValueError("A queue credential is named by an environment variable")
    if not TENANT.fullmatch(config["hatchet"]["tenant"]) or not NAME.fullmatch(config["hatchet"]["workflow"]):
        raise ValueError("The Hatchet lane queue needs a tenant and a workflow name")
    if not NAME.fullmatch(config["restate"]["service"]) or not NAME.fullmatch(config["restate"]["handler"]):
        raise ValueError("The Restate lane queue needs a service and a handler")
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
    except (OSError, KeyError, TypeError, ValueError, Refusal) as error:
        message = str(error) if type(error) in (ValueError, Refusal) else "The lane queue operation failed"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
