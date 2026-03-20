#!/usr/bin/env python3
"""
Cursor Cloud Agents + OpenClaw integration CLI.

Provides hardened wrappers for:
  - auth verification
  - agent lifecycle (create/list/status/followup/stop/delete)
  - conversation + artifacts endpoints
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import env_loader  # noqa: E402


TERMINAL_STATUSES = {"FINISHED", "FAILED", "CANCELLED", "STOPPED", "EXPIRED"}


def _load_repo_dotenv() -> None:
    """Populate os.environ from repo-root .env then cwd .env without overriding exports."""
    repo_root = _SCRIPTS_DIR.parent
    env_loader.merge_dotenv_paths([repo_root / ".env", Path.cwd() / ".env"], override=False)


def parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "y"}:
        return True
    if lowered in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}. Use true|false.")


def normalize_base_url(raw: Optional[str]) -> str:
    base = (raw or "https://api.cursor.com").strip()
    if not base:
        base = "https://api.cursor.com"
    return base.rstrip("/")


def redact(value: str) -> str:
    if not value:
        return "***"
    if len(value) <= 8:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


@dataclass
class Config:
    base_url: str
    api_key: str
    auth_mode: str
    timeout_seconds: int
    retries: int
    retry_backoff_seconds: float
    output_json: bool


class CursorApiClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def _auth_headers(self, mode: str) -> Dict[str, str]:
        if mode == "bearer":
            return {"Authorization": f"Bearer {self.cfg.api_key}"}
        if mode == "basic":
            token = base64.b64encode(f"{self.cfg.api_key}:".encode("utf-8")).decode("ascii")
            return {"Authorization": f"Basic {token}"}
        raise ValueError(f"Unsupported auth mode: {mode}")

    def _request_once(
        self, method: str, path: str, query: Optional[Dict[str, str]], body: Optional[Dict[str, Any]], mode: str
    ) -> Tuple[int, Dict[str, Any], str]:
        url = f"{self.cfg.base_url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        payload: Optional[bytes] = None
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "cursor-openclaw-integration/1.0",
        }
        headers.update(self._auth_headers(mode))
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url=url, data=payload, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else {}
                return resp.status, data, raw
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"raw": raw}
            return err.code, data, raw

    def request(
        self, method: str, path: str, query: Optional[Dict[str, str]] = None, body: Optional[Dict[str, Any]] = None
    ) -> Tuple[int, Dict[str, Any], str, str]:
        auth_order = ["bearer", "basic"] if self.cfg.auth_mode == "auto" else [self.cfg.auth_mode]
        last: Tuple[int, Dict[str, Any], str, str] = (0, {}, "", auth_order[0])
        for mode in auth_order:
            attempt = 0
            while True:
                status, data, raw = self._request_once(method, path, query, body, mode)
                last = (status, data, raw, mode)
                retryable = status in {429, 500, 502, 503, 504}
                if retryable and attempt < self.cfg.retries:
                    time.sleep(self.cfg.retry_backoff_seconds * (2**attempt))
                    attempt += 1
                    continue
                break
            # If unauthorized/forbidden with auto mode, try next auth mode.
            if self.cfg.auth_mode == "auto" and status in {401, 403} and mode != auth_order[-1]:
                continue
            return status, data, raw, mode
        return last


def print_out(payload: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")


def require_api_key() -> str:
    key = (os.getenv("CURSOR_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("CURSOR_API_KEY is required.")
    return key


def require_one_of(repo: str, pr_url: str) -> None:
    if not repo and not pr_url:
        raise ValueError("Provide --repository or --pr-url.")


def build_create_payload(args: argparse.Namespace) -> Dict[str, Any]:
    source: Dict[str, Any]
    if args.pr_url:
        source = {"prUrl": args.pr_url}
    else:
        source = {"repository": args.repository}
        if args.ref:
            source["ref"] = args.ref

    target: Dict[str, Any] = {"branchName": args.branch_name, "autoCreatePr": args.auto_create_pr}
    if args.auto_create_pr:
        target["openAsCursorGithubApp"] = args.open_as_cursor_github_app
        if args.open_as_cursor_github_app:
            target["skipReviewerRequest"] = args.skip_reviewer_request

    return {
        "prompt": {"text": args.prompt},
        "model": args.model,
        "source": source,
        "target": target,
    }


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=os.getenv("CURSOR_BASE_URL", "https://api.cursor.com"))
    parser.add_argument("--auth-mode", choices=["auto", "basic", "bearer"], default=os.getenv("CURSOR_AUTH_MODE", "auto"))
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-backoff-seconds", type=float, default=0.5)
    parser.add_argument("--json", action="store_true")


def validate_common_args(args: argparse.Namespace) -> None:
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be > 0")
    if args.retries < 0:
        raise ValueError("--retries must be >= 0")
    if args.retry_backoff_seconds < 0:
        raise ValueError("--retry-backoff-seconds must be >= 0")


def validate_command_args(args: argparse.Namespace) -> None:
    if args.command == "create-agent":
        if args.poll_attempts < 0:
            raise ValueError("--poll-attempts must be >= 0")
        if args.poll_interval_seconds < 0:
            raise ValueError("--poll-interval-seconds must be >= 0")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cursor Cloud Agents CLI for OpenClaw integrations.")
    add_common_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami")
    sub.add_parser("models")

    p_list = sub.add_parser("list-agents")
    p_list.add_argument("--limit", default="20")
    p_list.add_argument("--cursor", default="")
    p_list.add_argument("--pr-url", default="")

    p_status = sub.add_parser("agent-status")
    p_status.add_argument("--id", required=True)

    p_conv = sub.add_parser("conversation")
    p_conv.add_argument("--id", required=True)

    p_art = sub.add_parser("artifacts")
    p_art.add_argument("--id", required=True)

    p_art_dl = sub.add_parser("artifact-download-url")
    p_art_dl.add_argument("--id", required=True)
    p_art_dl.add_argument("--path", required=True)

    p_create = sub.add_parser("create-agent")
    p_create.add_argument("--prompt", required=True)
    p_create.add_argument("--repository", default="")
    p_create.add_argument("--ref", default="")
    p_create.add_argument("--pr-url", default="")
    p_create.add_argument("--model", default="default")
    p_create.add_argument("--branch-name", required=True)
    p_create.add_argument("--auto-create-pr", type=parse_bool, default=False)
    p_create.add_argument("--open-as-cursor-github-app", type=parse_bool, default=False)
    p_create.add_argument("--skip-reviewer-request", type=parse_bool, default=False)
    p_create.add_argument("--poll-attempts", type=int, default=0)
    p_create.add_argument("--poll-interval-seconds", type=float, default=3.0)
    p_create.add_argument("--dry-run", action="store_true")

    p_follow = sub.add_parser("followup")
    p_follow.add_argument("--id", required=True)
    p_follow.add_argument("--prompt", required=True)

    p_stop = sub.add_parser("stop-agent")
    p_stop.add_argument("--id", required=True)

    p_delete = sub.add_parser("delete-agent")
    p_delete.add_argument("--id", required=True)

    p_diag = sub.add_parser("diagnose")
    p_diag.add_argument("--show-key", action="store_true")
    return parser.parse_args()


def handle(cfg: Config, args: argparse.Namespace) -> Tuple[int, Dict[str, Any]]:
    client = CursorApiClient(cfg)

    if args.command == "diagnose":
        payload = {
            "ok": True,
            "base_url": cfg.base_url,
            "auth_mode": cfg.auth_mode,
            "timeout_seconds": cfg.timeout_seconds,
            "retries": cfg.retries,
            "api_key_present": bool(cfg.api_key),
            "api_key_redacted": redact(cfg.api_key) if args.show_key else "***",
        }
        return 0, payload

    if args.command == "whoami":
        status, data, raw, auth_mode = client.request("GET", "/v0/me")
        return status, {"status": status, "auth_mode": auth_mode, "response": data or raw}

    if args.command == "models":
        status, data, raw, auth_mode = client.request("GET", "/v0/models")
        return status, {"status": status, "auth_mode": auth_mode, "response": data or raw}

    if args.command == "list-agents":
        try:
            limit = int(str(args.limit))
        except ValueError as err:
            raise ValueError("--limit must be an integer between 1 and 100.") from err
        if limit < 1 or limit > 100:
            raise ValueError("--limit must be an integer between 1 and 100.")

        query: Dict[str, str] = {"limit": str(limit)}
        if args.cursor:
            query["cursor"] = args.cursor
        if args.pr_url:
            query["prUrl"] = args.pr_url
        status, data, raw, auth_mode = client.request("GET", "/v0/agents", query=query)
        return status, {"status": status, "auth_mode": auth_mode, "response": data or raw}

    if args.command == "agent-status":
        status, data, raw, auth_mode = client.request("GET", f"/v0/agents/{args.id}")
        return status, {"status": status, "auth_mode": auth_mode, "response": data or raw}

    if args.command == "conversation":
        status, data, raw, auth_mode = client.request("GET", f"/v0/agents/{args.id}/conversation")
        return status, {"status": status, "auth_mode": auth_mode, "response": data or raw}

    if args.command == "artifacts":
        status, data, raw, auth_mode = client.request("GET", f"/v0/agents/{args.id}/artifacts")
        return status, {"status": status, "auth_mode": auth_mode, "response": data or raw}

    if args.command == "artifact-download-url":
        status, data, raw, auth_mode = client.request(
            "GET",
            f"/v0/agents/{args.id}/artifacts/download",
            query={"path": args.path},
        )
        return status, {"status": status, "auth_mode": auth_mode, "response": data or raw}

    if args.command == "create-agent":
        require_one_of(args.repository, args.pr_url)
        payload = build_create_payload(args)
        if args.dry_run:
            return 0, {"status": 0, "dry_run": True, "payload": payload}
        status, data, raw, auth_mode = client.request("POST", "/v0/agents", body=payload)
        response = {"status": status, "auth_mode": auth_mode, "response": data or raw}
        if status < 400 and args.poll_attempts > 0 and isinstance(data, dict) and data.get("id"):
            agent_id = str(data["id"])
            current = data
            for _ in range(args.poll_attempts):
                current_status = str(current.get("status", ""))
                if current_status in TERMINAL_STATUSES:
                    break
                time.sleep(max(0.0, args.poll_interval_seconds))
                poll_status, poll_data, _, _ = client.request("GET", f"/v0/agents/{agent_id}")
                if poll_status >= 400:
                    break
                current = poll_data
            response["polled"] = current
        return status, response

    if args.command == "followup":
        body = {"prompt": {"text": args.prompt}}
        status, data, raw, auth_mode = client.request("POST", f"/v0/agents/{args.id}/followup", body=body)
        return status, {"status": status, "auth_mode": auth_mode, "response": data or raw}

    if args.command == "stop-agent":
        status, data, raw, auth_mode = client.request("POST", f"/v0/agents/{args.id}/stop")
        return status, {"status": status, "auth_mode": auth_mode, "response": data or raw}

    if args.command == "delete-agent":
        status, data, raw, auth_mode = client.request("DELETE", f"/v0/agents/{args.id}")
        return status, {"status": status, "auth_mode": auth_mode, "response": data or raw}

    raise ValueError(f"Unsupported command: {args.command}")


def main() -> int:
    _load_repo_dotenv()
    args = parse_args()
    try:
        validate_common_args(args)
        validate_command_args(args)
        api_key = (os.getenv("CURSOR_API_KEY") or "").strip()
        if args.command != "diagnose" and not api_key:
            raise RuntimeError("CURSOR_API_KEY is required.")
        cfg = Config(
            base_url=normalize_base_url(args.base_url),
            api_key=api_key,
            auth_mode=args.auth_mode,
            timeout_seconds=args.timeout_seconds,
            retries=max(0, args.retries),
            retry_backoff_seconds=max(0.0, args.retry_backoff_seconds),
            output_json=args.json,
        )
        status, payload = handle(cfg, args)
        payload["ok"] = status < 400
        print_out(payload, as_json=cfg.output_json)
        return 0 if status < 400 else 4
    except Exception as err:  # noqa: BLE001
        payload = {"ok": False, "error": str(err)}
        as_json = "--json" in sys.argv
        print_out(payload, as_json=as_json)
        return 2


if __name__ == "__main__":
    sys.exit(main())
