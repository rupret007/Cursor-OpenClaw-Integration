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
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import env_loader  # noqa: E402
import cursor_api_common  # noqa: E402
import handoff_context  # noqa: E402

VERSION = "1.2.0"

TERMINAL_STATUSES = {"FINISHED", "FAILED", "CANCELLED", "STOPPED", "EXPIRED"}

_DOTENV_FILES_LOADED: list[Path] = []


def _load_repo_dotenv() -> None:
    """Populate os.environ from repo-root .env then cwd .env without overriding exports."""
    global _DOTENV_FILES_LOADED
    repo_root = _SCRIPTS_DIR.parent
    _DOTENV_FILES_LOADED = env_loader.merge_dotenv_paths(
        [repo_root / ".env", Path.cwd() / ".env"], override=False
    )


def parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "y"}:
        return True
    if lowered in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}. Use true|false.")


def _now_branch_suffix() -> str:
    # Keep it ASCII and filesystem/URL safe.
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _run_git(args: list[str], cwd: Optional[Path] = None) -> Tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def infer_git_remote_url(repo_path: Path) -> Optional[str]:
    code, out, _ = _run_git(["remote", "get-url", "origin"], cwd=repo_path)
    if code != 0:
        return None
    value = out.strip()
    if not value:
        return None
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value.split("git@github.com:", 1)[1]
    if value.endswith(".git"):
        value = value[:-4]
    return value


def infer_git_ref(repo_path: Path) -> Optional[str]:
    code, out, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
    if code != 0:
        return None
    ref = out.strip()
    if not ref or ref == "HEAD":
        return None
    return ref


def normalize_base_url(raw: Optional[str]) -> str:
    default = "https://api.cursor.com"
    base = (raw or default).strip()
    if not base:
        base = default
    lowered = base.lower()
    if not lowered.startswith(("http://", "https://")):
        raise ValueError("Base URL must start with http:// or https:// (check CURSOR_BASE_URL).")
    return base.rstrip("/")


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
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": cursor_api_common.USER_AGENT_OPENCLAW,
        }
        headers.update(self._auth_headers(mode))
        if body is not None:
            payload = cursor_api_common.encode_request_json(body)
        req = urllib.request.Request(url=url, data=payload, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = cursor_api_common.parse_json_response_body(raw)
                return resp.status, data, raw
        except urllib.error.HTTPError as err:
            try:
                raw = err.read().decode("utf-8", errors="replace")
            except Exception as read_err:  # noqa: BLE001
                raw = f"<unreadable HTTP error body: {read_err}>"
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"raw": raw}
            return err.code, data, raw
        except (urllib.error.URLError, TimeoutError, ConnectionError, BrokenPipeError, OSError) as err:
            msg = str(err)
            data = {"error": msg, "error_type": type(err).__name__}
            return cursor_api_common.TRANSIENT_TRANSPORT_STATUS, data, msg

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
                retryable = status in {429, 500, 502, 503, 504, cursor_api_common.TRANSIENT_TRANSPORT_STATUS}
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


def require_one_of(repo: str, pr_url: str) -> None:
    has_r = bool((repo or "").strip())
    has_p = bool((pr_url or "").strip())
    if has_r and has_p:
        raise ValueError("Pass only one of --repository or --pr-url (not both).")
    if not has_r and not has_p:
        raise ValueError("Provide --repository or --pr-url.")


def build_create_payload(args: argparse.Namespace, prompt_text: Optional[str] = None) -> Dict[str, Any]:
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

    text = args.prompt if prompt_text is None else prompt_text
    return {
        "prompt": {"text": text},
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
        cursor_api_common.assert_no_newlines_or_nul(args.branch_name, "--branch-name")
        if args.poll_attempts < 0:
            raise ValueError("--poll-attempts must be >= 0")
        if args.poll_interval_seconds < 0:
            raise ValueError("--poll-interval-seconds must be >= 0")
        has_prompt = bool((args.prompt or "").strip())
        intent = getattr(args, "intent", None)
        has_triage = bool((getattr(args, "triage_repo", "") or "").strip())
        if not has_prompt and intent is None and not has_triage:
            raise ValueError("Provide --prompt and/or --intent, and/or --triage-repo.")
    if args.command == "talk":
        if args.poll_attempts < 0:
            raise ValueError("--poll-attempts must be >= 0")
        if args.poll_interval_seconds < 0:
            raise ValueError("--poll-interval-seconds must be >= 0")
        cursor_api_common.assert_no_newlines_or_nul(args.branch_name, "--branch-name")
        if not (args.prompt or "").strip():
            raise ValueError("Provide --prompt.")
        if bool((args.repository or "").strip()) and bool((args.pr_url or "").strip()):
            raise ValueError("Pass only one of --repository or --pr-url (not both).")


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
    p_create.add_argument("--prompt", default="", help="Task text (optional if --intent or --triage-repo)")
    p_create.add_argument(
        "--intent",
        default=None,
        choices=list(handoff_context.INTENT_IDS),
        help="Scaffold: code-review | refactor | release-notes | brief",
    )
    p_create.add_argument(
        "--triage-repo",
        default="",
        help="Local directory; prepends non-secret repo snapshot to prompt",
    )
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

    p_talk = sub.add_parser("talk")
    p_talk.add_argument("--prompt", required=True, help="Message/task for Cursor agent")
    p_talk.add_argument(
        "--repo-path",
        default=".",
        help="Local repository path used to infer --repository/--ref when not explicitly provided (default: .)",
    )
    p_talk.add_argument("--repository", default="", help="Explicit GitHub repo URL (skips inference)")
    p_talk.add_argument("--ref", default="", help="Explicit git ref (skips inference; only with --repository)")
    p_talk.add_argument("--pr-url", default="", help="Explicit GitHub PR URL (uses source.prUrl)")
    p_talk.add_argument("--model", default="default")
    p_talk.add_argument("--branch-name", default=f"openclaw/talk-{_now_branch_suffix()}")
    p_talk.add_argument("--auto-create-pr", type=parse_bool, default=False)
    p_talk.add_argument("--open-as-cursor-github-app", type=parse_bool, default=False)
    p_talk.add_argument("--skip-reviewer-request", type=parse_bool, default=False)
    p_talk.add_argument("--poll-attempts", type=int, default=0)
    p_talk.add_argument("--poll-interval-seconds", type=float, default=3.0)
    p_talk.add_argument("--dry-run", action="store_true")

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
        openai_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        openai_on = cursor_api_common.parse_openai_enabled()
        payload = {
            "ok": True,
            "cli_version": VERSION,
            "base_url": cfg.base_url,
            "auth_mode": cfg.auth_mode,
            "timeout_seconds": cfg.timeout_seconds,
            "retries": cfg.retries,
            "api_key_present": bool(cfg.api_key),
            "api_key_redacted": cursor_api_common.redact_secret(cfg.api_key) if args.show_key else "***",
            "openai_api_key_present": bool(openai_key),
            "openai_api_enabled": openai_on,
            "openai_api_key_redacted": cursor_api_common.redact_secret(openai_key) if args.show_key else "***",
            "dotenv_files_loaded": [str(p) for p in _DOTENV_FILES_LOADED],
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
        cursor_api_common.validate_agent_id(args.id)
        status, data, raw, auth_mode = client.request("GET", f"/v0/agents/{args.id}")
        return status, {"status": status, "auth_mode": auth_mode, "response": data or raw}

    if args.command == "conversation":
        cursor_api_common.validate_agent_id(args.id)
        status, data, raw, auth_mode = client.request("GET", f"/v0/agents/{args.id}/conversation")
        return status, {"status": status, "auth_mode": auth_mode, "response": data or raw}

    if args.command == "artifacts":
        cursor_api_common.validate_agent_id(args.id)
        status, data, raw, auth_mode = client.request("GET", f"/v0/agents/{args.id}/artifacts")
        return status, {"status": status, "auth_mode": auth_mode, "response": data or raw}

    if args.command == "artifact-download-url":
        cursor_api_common.validate_agent_id(args.id)
        status, data, raw, auth_mode = client.request(
            "GET",
            f"/v0/agents/{args.id}/artifacts/download",
            query={"path": args.path},
        )
        return status, {"status": status, "auth_mode": auth_mode, "response": data or raw}

    if args.command == "create-agent":
        require_one_of(args.repository, args.pr_url)
        triage_path: Optional[Path] = None
        tr = (getattr(args, "triage_repo", "") or "").strip()
        if tr:
            triage_path = Path(tr).expanduser().resolve()
            if not triage_path.is_dir():
                raise ValueError("--triage-repo must be an existing directory")
        intent = getattr(args, "intent", None)
        if intent or triage_path is not None:
            try:
                prompt_body = handoff_context.compose_handoff_body(args.prompt or "", intent, triage_path)
            except ValueError as err:
                raise ValueError(str(err)) from err
        else:
            prompt_body = (args.prompt or "").strip()
        payload = build_create_payload(args, prompt_body)
        if args.dry_run:
            return 0, {"status": 0, "dry_run": True, "payload": payload}
        status, data, raw, auth_mode = client.request("POST", "/v0/agents", body=payload)
        response = {"status": status, "auth_mode": auth_mode, "response": data or raw}
        if status < 400 and args.poll_attempts > 0 and isinstance(data, dict) and data.get("id") is not None:
            agent_id = str(data["id"]).strip()
            current = data
            try:
                cursor_api_common.validate_agent_id(agent_id, flag_name="agent id from API")
            except ValueError as err:
                response["poll_skipped"] = str(err)
            else:
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

    if args.command == "talk":
        # A convenience wrapper around create-agent:
        # - if --repository/--pr-url not provided, infer from local git repo
        repository = (args.repository or "").strip()
        pr_url = (args.pr_url or "").strip()
        ref = (args.ref or "").strip()

        if not repository and not pr_url:
            repo_path = Path(args.repo_path).expanduser().resolve()
            if not repo_path.is_dir():
                raise ValueError("--repo-path must be an existing directory")
            repository = infer_git_remote_url(repo_path) or ""
            ref = ref or (infer_git_ref(repo_path) or "")
            if not repository:
                raise ValueError(
                    "Could not infer repository from git remote 'origin'. "
                    "Provide --repository or --pr-url (or run from a git clone with an origin remote)."
                )

        require_one_of(repository, pr_url)
        if pr_url and ref:
            raise ValueError("--ref cannot be used with --pr-url.")

        # Reuse the create-agent payload shape.
        class _Args:
            prompt = args.prompt
            repository = repository
            ref = ref
            pr_url = pr_url
            model = args.model
            branch_name = args.branch_name
            auto_create_pr = args.auto_create_pr
            open_as_cursor_github_app = args.open_as_cursor_github_app
            skip_reviewer_request = args.skip_reviewer_request

        payload = build_create_payload(_Args())
        if args.dry_run:
            return 0, {
                "status": 0,
                "dry_run": True,
                "inferred": {"repository": repository or None, "ref": ref or None, "pr_url": pr_url or None},
                "payload": payload,
            }

        status, data, raw, auth_mode = client.request("POST", "/v0/agents", body=payload)
        response = {"status": status, "auth_mode": auth_mode, "response": data or raw}
        if status < 400 and args.poll_attempts > 0 and isinstance(data, dict) and data.get("id") is not None:
            agent_id = str(data["id"]).strip()
            current = data
            try:
                cursor_api_common.validate_agent_id(agent_id, flag_name="agent id from API")
            except ValueError as err:
                response["poll_skipped"] = str(err)
            else:
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
        cursor_api_common.validate_agent_id(args.id)
        body = {"prompt": {"text": args.prompt}}
        status, data, raw, auth_mode = client.request("POST", f"/v0/agents/{args.id}/followup", body=body)
        return status, {"status": status, "auth_mode": auth_mode, "response": data or raw}

    if args.command == "stop-agent":
        cursor_api_common.validate_agent_id(args.id)
        status, data, raw, auth_mode = client.request("POST", f"/v0/agents/{args.id}/stop")
        return status, {"status": status, "auth_mode": auth_mode, "response": data or raw}

    if args.command == "delete-agent":
        cursor_api_common.validate_agent_id(args.id)
        status, data, raw, auth_mode = client.request("DELETE", f"/v0/agents/{args.id}")
        return status, {"status": status, "auth_mode": auth_mode, "response": data or raw}

    raise ValueError(f"Unsupported command: {args.command}")


def main() -> int:
    _load_repo_dotenv()
    if len(sys.argv) == 2 and sys.argv[1] in ("--version", "-V"):
        print(f"cursor_openclaw {VERSION}")
        return 0
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
        print_out(payload, as_json=cursor_api_common.argv_has_json_flag())
        return 2


if __name__ == "__main__":
    sys.exit(main())
