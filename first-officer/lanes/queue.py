#!/usr/bin/env python3
"""Queue one review lane on Hatchet, and use Restate only when Hatchet definitely refuses it.

Hatchet is the primary. Its trigger is the documented stable REST call. Restate is the
documented fallback ingress: ``POST /restate/send/{service}/{handler}`` with an
``idempotency-key``. Those two backends do not share that key, so a timeout, HTTP 408,
any 5xx, a dropped connection, or any answer that is not a definite refusal stays on
Hatchet and is never sent to Restate. The primary may already have accepted the lane.
The private ledger is append-only. A pending or unknown row blocks a second send until
the original backend can confirm acceptance. The request connects to the addresses that passed
that check and does not resolve the name again. An HTTPS address literal has to be
listed in ``FO_QUEUE_HOSTS`` the same way a name does. Loopback HTTP does not.
"""

import argparse
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import http.client
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


def _listed(address):
    """Match an HTTPS address literal against the allowlist by address, not spelling."""
    for host in queue_hosts():
        try:
            if ipaddress.ip_address(host) == address:
                return True
        except ValueError:
            continue
    return False


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
    elif not _usable_private(address) or not _listed(address):
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
    except (OSError, http.client.HTTPException):
        raise Uncertain("The queue connection failed after the request was sent") from None
    if response is None:
        raise Uncertain("The queue gave no answer")
    try:
        if getattr(response, "status", 200) >= 300:
            raise Uncertain(f"The queue answered HTTP {response.status}")
        raw = response.read(MAX_BODY + 1)
        if len(raw) <= MAX_BODY and getattr(response, "length", 0):
            raise Uncertain("The queue response ended before its declared length")
    except (OSError, http.client.HTTPException):
        raise Uncertain("The queue response was interrupted") from None
    finally:
        response.close()
    if len(raw) > MAX_BODY:
        raise Uncertain("The queue returned an oversized response")
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise Uncertain("The queue returned an unreadable response") from None
    if not isinstance(body, dict):
        raise Uncertain("The queue returned an unexpected response")
    return body


def run_identifier(value):
    if not isinstance(value, str) or not RUN_ID.fullmatch(value):
        raise Uncertain("The queue accepted the lane without a usable run identifier")
    return value


def submission(backend, config, request, key):
    """Validate the selected route and resolve its credential before reserving a send."""
    if backend == "hatchet":
        tenant, workflow = config["tenant"], config["workflow"]
        if not TENANT.fullmatch(tenant) or not NAME.fullmatch(workflow):
            raise ValueError("The Hatchet lane queue needs a tenant and a workflow name")
        path = f"/api/v1/stable/tenants/{quote(tenant)}/workflow-runs/trigger"
        payload = {"workflowName": workflow, "input": request,
                   "additionalMetadata": {"idempotencyKey": key}}
        extra_headers = {}
    else:
        service, handler = config["service"], config["handler"]
        if not NAME.fullmatch(service) or not NAME.fullmatch(handler):
            raise ValueError("The Restate lane queue needs a service and a handler")
        path = f"/restate/send/{quote(service)}/{quote(handler)}"
        payload = request
        extra_headers = {"idempotency-key": key}
    token = secret(config["token_env"])
    base, opener = endpoint(config["base_url"])
    target = hashlib.sha256((base + path).encode()).hexdigest()
    return (opener, base + path, {"Authorization": "Bearer " + token, **extra_headers}, payload), target


def accepted_run(backend, body):
    if backend == "restate":
        if body.get("status") not in ("Accepted", "PreviouslyAccepted"):
            raise Uncertain("Restate did not return an acceptance receipt")
        return run_identifier(body.get("invocationId"))
    run = body.get("run", body)
    metadata = run.get("metadata") if isinstance(run, dict) else None
    return run_identifier(metadata.get("id") if isinstance(metadata, dict) else None)


def _check_ledger(info):
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        raise ValueError("The lane ledger must be a private owner file")
    if info.st_size > MAX_LEDGER:
        raise ValueError("The lane ledger is too large to trust")


@contextmanager
def lane_lock(ledger, key):
    """Serialize one request while allowing unrelated reservations to make progress."""
    directory = Path(ledger).with_name(Path(ledger).name + ".locks")
    directory.mkdir(mode=0o700, exist_ok=True)
    root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(root)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("The lane lock directory must be private")
        # One of 16 persistent locks keeps disk usage bounded across old lane keys.
        handle = os.open(key[-1:], os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=root)
        try:
            info = os.fstat(handle)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ValueError("The lane lock must be a private owner file")
            fcntl.flock(handle, fcntl.LOCK_EX)
            if os.fstat(handle).st_nlink != 1:
                raise ValueError("The lane lock was replaced")
            yield
        finally:
            os.close(handle)
    finally:
        os.close(root)


@contextmanager
def locked_ledger(path, *, read_only=False):
    try:
        flags = os.O_RDONLY if read_only else os.O_RDWR | os.O_CREAT
        handle = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("The lane ledger must be a private owner file") from None
        raise
    file = os.fdopen(handle, "r" if read_only else "r+", encoding="utf-8")
    try:
        info = os.fstat(file.fileno())
        if not read_only and stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_size == 0 and info.st_nlink == 1:
            os.fchmod(file.fileno(), 0o600)
            info = os.fstat(file.fileno())
        _check_ledger(info)
        fcntl.flock(file, fcntl.LOCK_SH if read_only else fcntl.LOCK_EX)
        info = os.fstat(file.fileno())
        _check_ledger(info)
        if not read_only and info.st_size == 0:
            # The first reservation also needs a durable directory entry.
            directory = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
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
    line = json.dumps(entry, sort_keys=True) + "\n"
    if file.tell() + len(line) > MAX_LEDGER:
        raise ValueError("The lane ledger is too large to trust")
    file.write(line)
    file.flush()
    os.fsync(file.fileno())


def _bounded_timeout(timeout):
    if isinstance(timeout, bool) or type(timeout) not in (int, float) or not 0 < timeout <= TIMEOUT:
        raise ValueError("The queue timeout must be within the reviewed limit")
    return timeout


def queue_state(file, request, key):
    state = latest(file, key)
    if state is None:
        return None
    phase, backend, run = state.get("phase"), state.get("backend"), state.get("run")
    if (state.get("head") != request["head"] or state.get("lane") != request["lane"]
            or phase not in ("pending", "uncertain", "refused", "queued")
            or backend not in ("hatchet", "restate")
            or (phase == "queued" and (not isinstance(run, str) or not RUN_ID.fullmatch(run)))
            or (phase != "queued" and run is not None)):
        raise ValueError("The lane ledger has an invalid state")
    return state


def transition(ledger, request, key, expected, entry):
    """Fsync a state change only while its predecessor is still current."""
    with locked_ledger(ledger) as file:
        if queue_state(file, request, key) != expected:
            raise ValueError("The lane ledger changed during dispatch. Check its current state.")
        append_entry(file, entry)


def dispatch(config, request, ledger, now=None, timeout=TIMEOUT):
    """Dispatch one lane once. Resume from a recorded primary refusal after interruption."""
    timeout = _bounded_timeout(timeout)
    if now is None:
        moment = int(time.time())
    elif type(now) is int and 0 <= now < 1 << 42:
        moment = now
    else:
        raise ValueError("The queue clock is unusable")
    key = idempotency_key(request)
    with lane_lock(ledger, key):
        with locked_ledger(ledger) as file:
            state = queue_state(file, request, key)
        if state and state["phase"] == "queued":
            return {**state, "reused": True}
        if state and state["phase"] != "refused":
            raise ValueError("The earlier queue attempt has an unknown outcome. "
                             "The lane was not sent again.")

        def attempt(backend):
            nonlocal state
            args, target = submission(backend, config[backend], request, key)
            entry = {"at": moment, "backend": backend, "head": request["head"], "key": key,
                     "lane": request["lane"], "phase": "pending", "run": None, "target": target}
            transition(ledger, request, key, state, entry)
            try:
                run = accepted_run(backend, post(*args, timeout))
            except (Refusal, Uncertain) as error:
                phase = "refused" if isinstance(error, Refusal) else "uncertain"
                outcome = {**entry, "phase": phase, "detail": str(error)}
                transition(ledger, request, key, entry, outcome)
                state = outcome
                raise
            outcome = {**entry, "phase": "queued", "run": run}
            transition(ledger, request, key, entry, outcome)
            return {**outcome, "reused": False}

        if not state or state["backend"] != "hatchet":
            try:
                return attempt("hatchet")
            except Uncertain as error:
                raise ValueError(f"Hatchet outcome is unknown: {error}. The lane was not failed over.") from None
            except Refusal:
                pass
        try:
            return attempt("restate")
        except Uncertain as error:
            raise ValueError(f"Hatchet refused the lane and the Restate outcome is unknown: {error}") from None
        except Refusal as error:
            raise ValueError(f"Hatchet refused the lane and Restate refused it ({error}). "
                             "The lane was not queued.") from None


def reconcile_restate(config, request, ledger, timeout=TIMEOUT):
    """Record positive lookup evidence for the original Restate destination only."""
    timeout = _bounded_timeout(timeout)
    key = idempotency_key(request)
    with lane_lock(ledger, key):
        with locked_ledger(ledger) as file:
            state = queue_state(file, request, key)
        if state and state["phase"] == "queued":
            return {**state, "reused": True}
        if not state or state["backend"] != "restate" or state["phase"] not in ("pending", "uncertain"):
            raise ValueError("The ledger has no unresolved Restate submission")
        route = config["restate"]
        base = route["base_url"].rstrip("/")
        service, handler = route["service"], route["handler"]
        if not NAME.fullmatch(service) or not NAME.fullmatch(handler):
            raise ValueError("The Restate lane queue needs a service and a handler")
        target = hashlib.sha256(f"{base}/restate/send/{quote(service)}/{quote(handler)}".encode()).hexdigest()
        if state.get("target") != target:
            raise ValueError("The ledger cannot verify this as the original Restate destination")
        base, opener = endpoint(base)
        payload = {"target": "idempotentInvocation", "service": service, "handler": handler,
                   "idempotencyKey": key}
        try:
            body = post(opener, f"{base}/restate/lookup",
                        {"Authorization": "Bearer " + secret(route["token_env"])}, payload, timeout)
            run = run_identifier(body.get("invocationId"))
        except (Refusal, Uncertain):
            raise ValueError("Restate lookup did not confirm acceptance. The lane remains unresolved.") from None
        entry = {**state, "at": int(time.time()), "phase": "queued", "run": run}
        entry.pop("detail", None)
        transition(ledger, request, key, state, entry)
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="Read local state without contacting either backend")
    mode.add_argument("--reconcile-restate", action="store_true", help="Recover an existing Restate receipt without resending")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        request = {"lane": args.lane, "head": args.head, "pull_request": args.pull_request}
        if args.status:
            key = idempotency_key(request)
            try:
                with locked_ledger(args.ledger, read_only=True) as file:
                    state = queue_state(file, request, key)
            except FileNotFoundError:
                state = None
            print(json.dumps({key: state[key] for key in ("phase", "backend", "run")} if state else
                             {"phase": "unseen"}, sort_keys=True))
            return 0
        operation = reconcile_restate if args.reconcile_restate else dispatch
        result = operation(load_config(args.policy), request, args.ledger)
        if args.reconcile_restate:
            print(f"Lane {result['lane']} has a verified {result['backend']} receipt.")
            return 0
        verb = "was already queued on" if result["reused"] else "is queued on"
        print(f"Lane {result['lane']} {verb} {result['backend']} as {result['run']}.")
        return 0
    except (OSError, KeyError, TypeError, ValueError, Refusal) as error:
        message = str(error) if type(error) in (ValueError, Refusal) else "The lane queue operation failed"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
