#!/usr/bin/env python3
"""Stage the LiteLLM policy and provision a single budgeted virtual key per lane."""

import argparse
from copy import deepcopy
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


LANE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
ENV_REF = re.compile(r"os\.environ/[A-Z_][A-Z0-9_]*")
TOKEN = re.compile(r"sk-[A-Za-z0-9_-]{8,256}")
ALLOWED_ROUTES = ["/chat/completions", "/v1/chat/completions", "/models", "/v1/models",
                  "/responses", "/v1/responses"]


def model_names(values):
    if not isinstance(values, list) or not values or not all(
        isinstance(name, str) and MODEL.fullmatch(name) for name in values
    ) or len(values) != len(set(values)):
        raise ValueError("Expected a nonempty list of distinct model aliases")
    return values


def lane_parameters(lane, policy):
    if not isinstance(lane, str) or not LANE.fullmatch(lane) or len(lane) > 64:
        raise ValueError("Invalid lane name")
    if not isinstance(policy, dict) or set(policy) != {"models", "max_budget", "budget_duration"}:
        raise ValueError("Each lane must define its models, budget and budget duration")
    maximum = policy["max_budget"]
    duration = policy["budget_duration"]
    if type(maximum) not in (int, float) or not 0 < maximum < float("inf"):
        raise ValueError("Each lane budget must be positive and finite")
    if not isinstance(duration, str) or not re.fullmatch(r"[1-9][0-9]{0,3}[smhd]", duration):
        raise ValueError("Invalid lane budget duration")
    return {"key_alias": f"first-officer-{lane}", "models": model_names(policy["models"]),
            "max_budget": maximum, "budget_duration": duration, "allowed_routes": ALLOWED_ROUTES}


def _charge(row, field):
    found = []
    for key in ("litellm_params", "model_info"):
        source = row.get(key)
        if source is None:
            continue
        if not isinstance(source, dict):
            raise ValueError("A routed model has pricing that bypasses budget enforcement")
        if field not in source:
            continue
        value = source[field]
        if found and value != found[0]:
            raise ValueError("A routed model has pricing that bypasses budget enforcement")
        found.append(value)
    if not found:
        raise ValueError("A routed model has pricing that bypasses budget enforcement")
    value = found[0]
    if type(value) not in (int, float) or value != value or value < 0 or value == float("inf"):
        raise ValueError("A routed model has pricing that bypasses budget enforcement")
    return value


def candidate_config(original, plan):
    if not isinstance(original, dict) or not isinstance(plan, dict) or set(plan) != {"fallbacks", "lanes"}:
        raise ValueError("Expected a proxy configuration and fallback and lane policy")
    cfg = deepcopy(original)
    deployments = cfg.get("model_list")
    if not isinstance(deployments, list) or not all(isinstance(row, dict) for row in deployments):
        raise ValueError("Expected a static LiteLLM model catalog")
    catalog = {name for row in deployments if isinstance(name := row.get("model_name"), str)}
    fallbacks, lanes = plan["fallbacks"], plan["lanes"]
    if not isinstance(fallbacks, dict) or not isinstance(lanes, dict) or not lanes:
        raise ValueError("The plan must contain a fallback map and budgeted lanes")
    for primary, targets in fallbacks.items():
        targets = set(model_names(targets))
        if primary not in catalog or primary in targets or not targets <= catalog:
            raise ValueError("A fallback must name distinct configured model aliases")
    metered = set()
    for lane, policy in lanes.items():
        allowed = set(lane_parameters(lane, policy)["models"])
        if not allowed <= catalog or any(
            primary in allowed and not set(targets) <= allowed for primary, targets in fallbacks.items()
        ):
            raise ValueError("A lane must explicitly allow its configured aliases and fallback targets")
        metered.update(allowed)
    for row in deployments:
        if row.get("model_name") not in metered:
            continue
        incoming = _charge(row, "input_cost_per_token")
        outgoing = _charge(row, "output_cost_per_token")
        if incoming <= 0 and outgoing <= 0:
            raise ValueError("A routed model has pricing that bypasses budget enforcement")

    general = cfg.setdefault("general_settings", {})
    for field, variable in (("master_key", "LITELLM_MASTER_KEY"), ("database_url", "DATABASE_URL")):
        reference = general.setdefault(field, f"os.environ/{variable}")
        if not isinstance(reference, str) or not ENV_REF.fullmatch(reference):
            raise ValueError("Use environment references for the proxy master key and database URL")
    general.update(fail_closed_budget_enforcement=True, disable_budget_reservation=False,
                   enforce_fallback_model_access=True)
    cfg.setdefault("litellm_settings", {})["block_requests_for_models_without_pricing"] = True
    router = cfg.setdefault("router_settings", {})
    existing = router.get("fallbacks") or []
    if not isinstance(existing, list) or not all(isinstance(row, dict) and len(row) == 1 for row in existing):
        raise ValueError("Existing fallback rules need manual review")
    router["fallbacks"] = [row for row in existing if not set(row) & set(fallbacks)]
    router["fallbacks"].extend({alias: list(targets)} for alias, targets in fallbacks.items())
    return cfg


HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
PRIVATE_ENDPOINT = "Use the local LiteLLM management URL or its private HTTPS root"


def _pinhttp():
    path = Path(__file__).resolve().parents[1] / "pinhttp.py"
    spec = importlib.util.spec_from_file_location("fo_litellm_pinhttp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pinhttp = _pinhttp()


def litellm_hosts():
    hosts = []
    for item in os.environ.get("FO_LITELLM_HOSTS", "").split(","):
        host = item.strip().lower().rstrip(".")
        if not host:
            continue
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not HOST.fullmatch(host) or len(host) > 253:
                raise ValueError(PRIVATE_ENDPOINT)
        hosts.append(host)
    return hosts


def _usable_private(address):
    return (address.is_loopback or address.is_private) and not address.is_multicast and not address.is_unspecified


def private_endpoint(hostname, resolve=None):
    host = hostname.lower().rstrip(".")
    if host not in litellm_hosts():
        raise ValueError(PRIVATE_ENDPOINT)
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        resolver = resolve or socket.getaddrinfo
        try:
            answers = resolver(host, None)
        except OSError:
            raise ValueError(PRIVATE_ENDPOINT) from None
        addresses = []
        for answer in answers:
            try:
                addresses.append(ipaddress.ip_address(answer[4][0]))
            except (TypeError, ValueError, IndexError):
                raise ValueError(PRIVATE_ENDPOINT) from None
    if not addresses or any(not _usable_private(address) for address in addresses):
        raise ValueError(PRIVATE_ENDPOINT)
    return [str(address) for address in addresses]


def _http_pins(hostname, resolve):
    if hostname == "localhost":
        resolver = resolve or socket.getaddrinfo
        try:
            answers = resolver(hostname, None)
        except OSError:
            raise ValueError(PRIVATE_ENDPOINT) from None
        pins = []
        for answer in answers:
            try:
                address = ipaddress.ip_address(answer[4][0])
            except (TypeError, ValueError, IndexError):
                raise ValueError(PRIVATE_ENDPOINT) from None
            if not address.is_loopback:
                raise ValueError(PRIVATE_ENDPOINT)
            pins.append(str(address))
        if not pins:
            raise ValueError(PRIVATE_ENDPOINT)
        return pins
    return [str(ipaddress.ip_address(hostname))]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def proxy_request(base_url, master_key, route, payload=None, *, resolve=None):
    url = urlsplit(base_url)
    if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None or
            url.password is not None or url.path not in ("", "/") or url.query or url.fragment or
            (url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"))):
        raise ValueError(PRIVATE_ENDPOINT)
    if url.scheme == "https":
        pins = private_endpoint(url.hostname, resolve)
    else:
        pins = _http_pins(url.hostname, resolve)
    if not TOKEN.fullmatch(master_key):
        raise ValueError("The LiteLLM master key is missing or invalid")
    request = Request(base_url.rstrip("/") + route,
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Authorization": f"Bearer {master_key}", "Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({}), NoRedirect(), pinhttp.handler(url.scheme, pins)).open(
                request, timeout=15) as response:
            raw = response.read(65_537)
            if len(raw) > 65_536:
                raise ValueError("LiteLLM returned an oversized management response")
            data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except HTTPError as error:
        raise ValueError(f"LiteLLM rejected the management request with HTTP {error.code}") from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    raise ValueError("LiteLLM returned no usable management response")


def key_info(base_url, master_key, key):
    # The proxy accepts hashed keys. The live credential stays out of request URLs and server access logs.
    if not TOKEN.fullmatch(key) or key == master_key:
        raise ValueError("Expected a virtual key distinct from the master key")
    digest = hashlib.sha256(key.encode()).hexdigest()
    info = proxy_request(base_url, master_key, "/key/info?" + urlencode({"key": digest})).get("info")
    if not isinstance(info, dict):
        raise ValueError("LiteLLM returned no virtual key policy")
    return info


def verify_lane(base_url, master_key, key, lane, policy):
    expected = lane_parameters(lane, policy)
    info = key_info(base_url, master_key, key)
    if (info.get("status") != "active" or not info.get("budget_reset_at") or any(
        info.get(field) != value for field, value in expected.items()
    )):
        raise ValueError("Stored virtual key policy differs from the reviewed lane policy")


def create_lane(base_url, master_key, lane, policy, key=None):
    payload = lane_parameters(lane, policy)
    if key is not None:
        if not TOKEN.fullmatch(key) or key == master_key:
            raise ValueError("Expected a virtual key distinct from the master key")
        payload = {**payload, "key": key}
    issued = proxy_request(base_url, master_key, "/key/generate", payload).get("key")
    if not isinstance(issued, str) or not TOKEN.fullmatch(issued) or issued == master_key:
        raise ValueError("LiteLLM returned no usable virtual key")
    if key is not None and issued != key:
        try:
            revoke_lane(base_url, master_key, issued)
        finally:
            raise ValueError("LiteLLM returned a different virtual key")
    return issued


def revoke_lane(base_url, master_key, key):
    """Revoke one virtual key. The key is not included in the raised error."""
    if not TOKEN.fullmatch(key) or key == master_key:
        raise ValueError("Expected a virtual key distinct from the master key")
    proxy_request(base_url, master_key, "/key/delete", {"keys": [key]})


def _new_key():
    key = "sk-" + secrets.token_urlsafe(32)
    if not TOKEN.fullmatch(key):
        raise ValueError("LiteLLM returned no usable virtual key")
    return key


def provision_lane(base_url, master_key, lane, policy):
    key = create_lane(base_url, master_key, lane, policy)
    verify_lane(base_url, master_key, key, lane, policy)
    return key


def write_private(path, contents):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(contents)
        handle.flush()
        os.fsync(handle.fileno())


def read_key(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as handle:
        file = os.fstat(handle.fileno())
        if (not stat.S_ISREG(file.st_mode) or file.st_uid != os.getuid() or file.st_nlink != 1 or
                file.st_mode & 0o077 or file.st_size > 300):
            raise ValueError("Virtual key file must be a private owner file")
        return handle.read(301).strip()


def provision_file(base_url, master_key, lane, policy, credential):
    directory = os.open(credential.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        owner = os.fstat(directory)
        if owner.st_uid != os.getuid() or owner.st_mode & 0o022:
            raise ValueError("Virtual key directory must be writable only by its owner")
        fcntl.flock(directory, fcntl.LOCK_EX)
        pending = credential.with_name(credential.name + ".pending")
        try:
            key = read_key(credential)
        except FileNotFoundError:
            try:
                key = read_key(pending)
            except FileNotFoundError:
                key = _new_key()
                # The pending file is the recovery record. Do not ask the proxy
                # to create a key until this write has succeeded.
                write_private(pending, key + "\n")
            try:
                create_lane(base_url, master_key, lane, policy, key)
            except ValueError:
                # The same key may already be registered after a crash.
                pass
            verify_lane(base_url, master_key, key, lane, policy)
            os.replace(pending, credential)
            os.fsync(directory)
        else:
            verify_lane(base_url, master_key, key, lane, policy)
    finally:
        os.close(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy_config", type=Path)
    parser.add_argument("plan", type=Path, help="Reviewed JSON fallback and lane policy")
    subcommands = parser.add_subparsers(dest="command", required=True)
    render = subcommands.add_parser("render", help="Write a private candidate config without contacting the proxy")
    render.add_argument("output", type=Path)
    provision = subcommands.add_parser("provision", help="Verify or issue one reviewed virtual key")
    provision.add_argument("base_url")
    provision.add_argument("lane")
    provision.add_argument("credential", type=Path, help="Private credential file for the lane")
    args = parser.parse_args()
    try:
        import yaml
    except ImportError:
        print("LiteLLM configuration requires PyYAML", file=sys.stderr)
        return 1

    try:
        original = yaml.safe_load(args.proxy_config.read_text(encoding="utf-8"))
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        config = candidate_config(original, plan)
        if args.command == "render":
            write_private(args.output, yaml.safe_dump(config, sort_keys=False))
            print("LiteLLM candidate rendered")
            return 0
        policy = plan["lanes"].get(args.lane)
        lane_parameters(args.lane, policy)
        master_key = os.environ.get("LITELLM_MASTER_KEY", "")
        provision_file(args.base_url, master_key, args.lane, policy, args.credential)
        print("LiteLLM lane policy verified")
        return 0
    except (OSError, KeyError, AttributeError, TypeError, ValueError, yaml.YAMLError):
        print("LiteLLM operation failed. Check the reviewed inputs, private files and management endpoint.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
