#!/usr/bin/env python3
"""
cursor_handoff.py

OpenClaw local skill orchestrator for handing off heavy repository work to Cursor.

Backends:
  - API (preferred): Cursor Cloud Agents API
  - CLI fallback: local `agent` or `cursor-agent` wrapper script

Docs basis used in this implementation:
  - Endpoints docs: https://cursor.com/docs/cloud-agent/api/endpoints
  - OpenAPI: https://cursor.com/docs-static/cloud-agents-openapi.yaml

API auth note:
  - Endpoints docs demonstrate Basic auth in curl examples (`-u API_KEY:`)
  - OpenAPI schema declares bearer auth (`Authorization: Bearer`)
  This script supports both by trying bearer first, then basic fallback.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import ssl
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_PREREQ = 3
EXIT_API = 4
EXIT_CLI = 5
EXIT_DIAG = 6

TERMINAL_STATUSES = {"FINISHED", "FAILED", "CANCELLED", "STOPPED", "EXPIRED"}
GITHUB_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


def parse_bool_text(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"true", "1", "yes", "y"}:
        return True
    if lowered in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}. Use true|false.")


def now_branch_name() -> str:
    return dt.datetime.now().strftime("openclaw/task-%Y%m%d-%H%M%S")


def normalize_base_url(raw: Optional[str]) -> str:
    default = "https://api.cursor.com"
    candidate = (raw or default).strip()
    if not candidate:
        candidate = default
    return candidate.rstrip("/")


def run_subprocess(args: list[str], cwd: Optional[Path] = None) -> Tuple[int, str, str]:
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def get_git_remote_url(repo_path: Path) -> Optional[str]:
    code, out, _ = run_subprocess(["git", "remote", "get-url", "origin"], cwd=repo_path)
    if code != 0:
        return None
    value = out.strip()
    if not value:
        return None
    if value.startswith("git@github.com:"):
        suffix = value.split("git@github.com:", 1)[1]
        value = f"https://github.com/{suffix}"
    if value.endswith(".git"):
        value = value[:-4]
    return value


def get_git_current_ref(repo_path: Path) -> Optional[str]:
    code, out, _ = run_subprocess(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
    if code != 0:
        return None
    ref = out.strip()
    if not ref or ref == "HEAD":
        return None
    return ref


def build_handoff_prompt(user_prompt: str, read_only: bool, branch: str, include_branch: bool) -> str:
    mode_line = (
        "READ-ONLY MODE: Analyze/review/plan only. Do NOT edit files, commit, or open PRs."
        if read_only
        else "EDIT MODE: Implement requested changes safely and summarize what changed."
    )
    branch_line = f"\nTarget branch: {branch}" if include_branch else ""
    return f"{mode_line}{branch_line}\n\nTask:\n{user_prompt.strip()}"


def normalize_repo_input(repo_value: str) -> Tuple[Optional[Path], Optional[str], Optional[str]]:
    """
    Returns (local_repo_path, repository_url, error_message).
    Accepts:
      - local directory path
      - GitHub URL (http/https)
      - owner/repo slug (converted to https://github.com/owner/repo)
    """
    raw = repo_value.strip()
    if not raw:
        return None, None, "Missing --repo value."

    expanded = Path(raw).expanduser()
    if expanded.exists():
        if not expanded.is_dir():
            return None, None, f"Repo path exists but is not a directory: {expanded}"
        return expanded.resolve(), None, None

    if raw.startswith("http://") or raw.startswith("https://"):
        return None, raw.rstrip("/"), None

    if GITHUB_SLUG_RE.fullmatch(raw):
        return None, f"https://github.com/{raw}", None

    return None, None, "Invalid --repo. Use local path, GitHub URL, or owner/repo."


def detect_cli_binary() -> Optional[str]:
    preferred = os.getenv("CURSOR_CLI_BIN", "").strip()
    if preferred:
        return preferred if shutil.which(preferred) else None
    for name in ("agent", "cursor-agent"):
        if shutil.which(name):
            return name
    return None


def choose_backend(
    requested_mode: str,
    has_api_creds: bool,
    cli_wrapper_path: Path,
    cli_binary: Optional[str],
) -> Tuple[str, Optional[str]]:
    if requested_mode == "api":
        if not has_api_creds:
            return "", "API mode requested but CURSOR_API_KEY is not set."
        return "api", None

    if requested_mode == "cli":
        if not cli_wrapper_path.exists():
            return "", f"CLI mode requested but wrapper is missing: {cli_wrapper_path}"
        if not cli_binary:
            return "", "CLI mode requested but neither 'agent' nor 'cursor-agent' is available."
        return "cli", None

    # auto mode
    if has_api_creds:
        return "api", None
    if cli_wrapper_path.exists() and cli_binary:
        return "cli", None
    return "", "Auto mode could not find usable backend (missing API creds and local CLI binary)."


@dataclass
class ApiConfig:
    base_url: str
    api_key: str
    retries: int = 2
    retry_backoff_seconds: float = 0.5


class CursorApiClient:
    def __init__(self, cfg: ApiConfig, timeout_seconds: int = 30) -> None:
        self.cfg = cfg
        self.timeout_seconds = timeout_seconds

    def _request(
        self, method: str, path: str, body: Optional[Dict[str, Any]], auth_mode: str
    ) -> Tuple[int, Dict[str, Any], str]:
        url = f"{self.cfg.base_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "openclaw-cursor-handoff/1.1",
        }

        if auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        elif auth_mode == "basic":
            import base64

            token = f"{self.cfg.api_key}:".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(token).decode("ascii")
        else:
            raise ValueError(f"Unsupported auth mode: {auth_mode}")

        payload: Optional[bytes] = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")

        request = urllib.request.Request(url=url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                text = response.read().decode("utf-8", errors="replace")
                parsed = json.loads(text) if text else {}
                return response.status, parsed, text
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return err.code, parsed, raw
        except urllib.error.URLError as err:
            raise RuntimeError(f"Network error while calling Cursor API: {err}") from err

    def _request_with_retries(
        self, method: str, path: str, body: Optional[Dict[str, Any]], auth_mode: str
    ) -> Tuple[int, Dict[str, Any], str]:
        attempts = max(0, self.cfg.retries) + 1
        for attempt in range(attempts):
            try:
                status, data, raw = self._request(method, path, body, auth_mode)
            except RuntimeError:
                if attempt >= attempts - 1:
                    raise
                delay = max(0.0, self.cfg.retry_backoff_seconds) * (2**attempt)
                if delay > 0:
                    time.sleep(delay)
                continue

            if status in TRANSIENT_HTTP_STATUSES and attempt < attempts - 1:
                delay = max(0.0, self.cfg.retry_backoff_seconds) * (2**attempt)
                if delay > 0:
                    time.sleep(delay)
                continue
            return status, data, raw

        # Should never happen because loop returns or raises.
        raise RuntimeError("Unexpected retry loop termination")

    def create_agent(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any], str, str]:
        for mode in ("bearer", "basic"):
            status, data, raw = self._request_with_retries(
                "POST", "/v0/agents", payload, auth_mode=mode
            )
            if status not in (401, 403):
                return status, data, raw, mode
        return status, data, raw, "basic"

    def get_agent(self, agent_id: str) -> Tuple[int, Dict[str, Any], str, str]:
        for mode in ("bearer", "basic"):
            status, data, raw = self._request_with_retries(
                "GET", f"/v0/agents/{agent_id}", None, auth_mode=mode
            )
            if status not in (401, 403):
                return status, data, raw, mode
        return status, data, raw, "basic"

    def get_endpoint(self, path: str) -> Tuple[int, Dict[str, Any], str, str]:
        for mode in ("bearer", "basic"):
            status, data, raw = self._request_with_retries("GET", path, None, auth_mode=mode)
            if status not in (401, 403):
                return status, data, raw, mode
        return status, data, raw, "basic"


def emit_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def emit_text(payload: Dict[str, Any]) -> None:
    if payload.get("ok"):
        print("Handoff submitted successfully.")
        print(f"Backend: {payload.get('backend')}")
        print(f"Read-only: {payload.get('read_only')}")
        print(f"Branch: {payload.get('branch')}")
        if payload.get("agent_id"):
            print(f"Agent ID: {payload.get('agent_id')}")
        if payload.get("agent_url"):
            print(f"Agent URL: {payload.get('agent_url')}")
        if payload.get("status"):
            print(f"Status: {payload.get('status')}")
    else:
        print("Handoff failed.")
        print(payload.get("error", "Unknown error"))


def build_ssl_diagnostics() -> Dict[str, Any]:
    verify = ssl.get_default_verify_paths()
    return {
        "ssl_cert_file_env": os.getenv("SSL_CERT_FILE"),
        "ssl_cert_dir_env": os.getenv("SSL_CERT_DIR"),
        "openssl_cafile": verify.openssl_cafile,
        "openssl_capath": verify.openssl_capath,
    }


def build_ssl_hint(error_text: str) -> Optional[str]:
    if "CERTIFICATE_VERIFY_FAILED" not in error_text:
        return None
    return (
        "Python SSL trust store failed. On macOS, install/update certifi and export "
        "SSL_CERT_FILE, e.g. "
        "python3 -m pip install --user --upgrade certifi && "
        "export SSL_CERT_FILE=\"$(python3 -c 'import certifi; print(certifi.where())')\""
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OpenClaw -> Cursor handoff orchestrator (Python 3 required)."
    )
    parser.add_argument("--repo", default="", help="Local repo path, GitHub URL, or owner/repo")
    parser.add_argument("--branch", default="", help="Target branch name (optional)")
    parser.add_argument("--prompt", default="", help="Task prompt for Cursor")
    parser.add_argument(
        "--mode",
        choices=["api", "cli", "auto"],
        default="",
        help="Execution mode. Defaults to OPENCLAW_CURSOR_DEFAULT_MODE or auto.",
    )
    parser.add_argument(
        "--read-only",
        default="true",
        dest="read_only",
        help="true|false. Use true for analysis/review/planning tasks.",
    )
    parser.add_argument(
        "--pr-url",
        default="",
        help="Optional GitHub PR URL. If provided, API mode submits source.prUrl.",
    )
    parser.add_argument(
        "--auto-create-pr",
        default="false",
        help="true|false. API only. Enables target.autoCreatePr.",
    )
    parser.add_argument(
        "--open-as-cursor-github-app",
        default="false",
        help="true|false. API only. Applies when autoCreatePr is true.",
    )
    parser.add_argument(
        "--skip-reviewer-request",
        default="false",
        help="true|false. API only. Applies when autoCreatePr + openAsCursorGithubApp are true.",
    )
    parser.add_argument(
        "--poll-max-attempts",
        type=int,
        default=3,
        help="API only. Number of status polls after submit (0 disables polling).",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=3.0,
        help="API only. Sleep interval between polls.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=30,
        help="API only. HTTP timeout per request (seconds).",
    )
    parser.add_argument(
        "--api-retries",
        type=int,
        default=2,
        help="API only. Retry count for transient HTTP/network errors.",
    )
    parser.add_argument(
        "--api-retry-backoff-seconds",
        type=float,
        default=0.5,
        help="API only. Base retry backoff in seconds (exponential).",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Run environment/API diagnostics instead of submitting a handoff.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print what would run")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.timeout_seconds <= 0:
        payload = {"ok": False, "error": "--timeout-seconds must be > 0"}
        emit_json(payload) if args.json else emit_text(payload)
        return EXIT_VALIDATION
    if args.api_retries < 0:
        payload = {"ok": False, "error": "--api-retries must be >= 0"}
        emit_json(payload) if args.json else emit_text(payload)
        return EXIT_VALIDATION
    if args.api_retry_backoff_seconds < 0:
        payload = {"ok": False, "error": "--api-retry-backoff-seconds must be >= 0"}
        emit_json(payload) if args.json else emit_text(payload)
        return EXIT_VALIDATION

    try:
        read_only = parse_bool_text(args.read_only)
        auto_create_pr = parse_bool_text(args.auto_create_pr)
        open_as_app = parse_bool_text(args.open_as_cursor_github_app)
        skip_reviewer = parse_bool_text(args.skip_reviewer_request)
    except ValueError as err:
        payload = {"ok": False, "error": str(err)}
        emit_json(payload) if args.json else emit_text(payload)
        return EXIT_VALIDATION

    if args.poll_max_attempts < 0:
        payload = {"ok": False, "error": "--poll-max-attempts must be >= 0"}
        emit_json(payload) if args.json else emit_text(payload)
        return EXIT_VALIDATION
    if args.poll_interval_seconds < 0:
        payload = {"ok": False, "error": "--poll-interval-seconds must be >= 0"}
        emit_json(payload) if args.json else emit_text(payload)
        return EXIT_VALIDATION

    prompt_text = args.prompt.strip()
    if not args.diagnose and not prompt_text:
        payload = {"ok": False, "error": "--prompt cannot be empty unless --diagnose is used"}
        emit_json(payload) if args.json else emit_text(payload)
        return EXIT_VALIDATION

    local_repo: Optional[Path] = None
    input_repo_url: Optional[str] = None
    if not args.diagnose:
        if not args.repo.strip():
            payload = {"ok": False, "error": "--repo is required unless --diagnose is used"}
            emit_json(payload) if args.json else emit_text(payload)
            return EXIT_VALIDATION
        local_repo, input_repo_url, repo_err = normalize_repo_input(args.repo)
        if repo_err:
            payload = {"ok": False, "error": repo_err}
            emit_json(payload) if args.json else emit_text(payload)
            return EXIT_VALIDATION

    requested_mode = (args.mode.strip().lower() or os.getenv("OPENCLAW_CURSOR_DEFAULT_MODE", "auto").strip().lower() or "auto")
    if requested_mode not in {"api", "cli", "auto"}:
        payload = {"ok": False, "error": f"Invalid mode: {requested_mode}. Use api|cli|auto."}
        emit_json(payload) if args.json else emit_text(payload)
        return EXIT_VALIDATION

    branch_from_user = bool(args.branch.strip())
    branch = args.branch.strip() or now_branch_name()
    include_branch_in_prompt = branch_from_user or (not read_only)
    final_prompt = build_handoff_prompt(
        prompt_text, read_only=read_only, branch=branch, include_branch=include_branch_in_prompt
    )

    skill_dir = Path(__file__).resolve().parent.parent
    cli_wrapper = skill_dir / "scripts" / "cursor_cli_fallback.sh"
    cli_binary = detect_cli_binary()

    api_key = os.getenv("CURSOR_API_KEY", "").strip()
    api_base_url = normalize_base_url(os.getenv("CURSOR_BASE_URL"))
    has_api_credentials = bool(api_key)

    if args.diagnose:
        suggested_backend = "api" if has_api_credentials else ("cli" if cli_binary else "none")
        checks: Dict[str, Any] = {
            "api_key_set": has_api_credentials,
            "api_key_length": len(api_key),
            "api_base_url": api_base_url,
            "requested_mode": requested_mode,
            "suggested_backend": suggested_backend,
            "cli_binary": cli_binary,
            "ssl": build_ssl_diagnostics(),
        }
        if has_api_credentials:
            api_client = CursorApiClient(
                ApiConfig(
                    base_url=api_base_url,
                    api_key=api_key,
                    retries=args.api_retries,
                    retry_backoff_seconds=args.api_retry_backoff_seconds,
                ),
                timeout_seconds=args.timeout_seconds,
            )
            try:
                me_status, me_data, me_raw, me_auth = api_client.get_endpoint("/v0/me")
                checks["me"] = {"status": me_status, "auth_mode": me_auth, "response": me_data or me_raw}
                agents_status, agents_data, agents_raw, agents_auth = api_client.get_endpoint("/v0/agents?limit=1")
                checks["agents"] = {
                    "status": agents_status,
                    "auth_mode": agents_auth,
                    "response": agents_data if agents_data else agents_raw,
                }
            except Exception as err:  # noqa: BLE001
                error_text = str(err)
                checks["api_check_error"] = error_text
                hint = build_ssl_hint(error_text)
                if hint:
                    checks["hint"] = hint
        payload = {"ok": True, "diagnose": True, "checks": checks}
        emit_json(payload) if args.json else emit_text(payload)
        return EXIT_OK

    backend, backend_error = choose_backend(
        requested_mode=requested_mode,
        has_api_creds=has_api_credentials,
        cli_wrapper_path=cli_wrapper,
        cli_binary=cli_binary,
    )
    if not backend:
        payload = {"ok": False, "error": backend_error}
        emit_json(payload) if args.json else emit_text(payload)
        return EXIT_PREREQ

    if args.dry_run:
        payload = {
            "ok": True,
            "dry_run": True,
            "backend": backend,
            "mode_requested": requested_mode,
            "read_only": read_only,
            "branch": branch,
            "repo_input": args.repo,
            "repo_local_path": str(local_repo) if local_repo else None,
            "repo_url": input_repo_url,
            "api_base_url": api_base_url if backend == "api" else None,
            "cli_binary": cli_binary if backend == "cli" else None,
            "timeout_seconds": args.timeout_seconds,
            "api_retries": args.api_retries,
            "prompt_preview": final_prompt[:500],
        }
        emit_json(payload) if args.json else emit_text(payload)
        return EXIT_OK

    if backend == "cli":
        if local_repo is None:
            payload = {"ok": False, "backend": "cli", "error": "CLI backend requires a local repo directory."}
            emit_json(payload) if args.json else emit_text(payload)
            return EXIT_VALIDATION
        if not cli_wrapper.exists():
            payload = {"ok": False, "backend": "cli", "error": f"CLI wrapper missing: {cli_wrapper}"}
            emit_json(payload) if args.json else emit_text(payload)
            return EXIT_PREREQ
        if not cli_binary:
            payload = {"ok": False, "backend": "cli", "error": "No local CLI binary found (agent/cursor-agent)."}
            emit_json(payload) if args.json else emit_text(payload)
            return EXIT_PREREQ

        env = os.environ.copy()
        env["CURSOR_CLI_BIN"] = cli_binary
        proc = subprocess.run(
            [str(cli_wrapper), str(local_repo), final_prompt, "true" if read_only else "false", branch],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        if proc.returncode != 0:
            payload = {
                "ok": False,
                "backend": "cli",
                "error": "CLI handoff failed",
                "exit_code": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
            }
            emit_json(payload) if args.json else emit_text(payload)
            return EXIT_CLI

        payload = {
            "ok": True,
            "backend": "cli",
            "submitted": True,
            "status": "submitted",
            "read_only": read_only,
            "branch": branch,
            "cli_binary": cli_binary,
            "stdout": proc.stdout.strip(),
        }
        emit_json(payload) if args.json else emit_text(payload)
        return EXIT_OK

    # API backend
    resolved_repository_url = input_repo_url
    resolved_ref = None
    if local_repo is not None:
        resolved_repository_url = get_git_remote_url(local_repo)
        resolved_ref = get_git_current_ref(local_repo)
        if not resolved_repository_url:
            payload = {
                "ok": False,
                "backend": "api",
                "error": "API mode requires a GitHub repository URL. Local repo has no 'origin' remote URL.",
            }
            emit_json(payload) if args.json else emit_text(payload)
            return EXIT_VALIDATION

    assert resolved_repository_url is not None
    api_client = CursorApiClient(
        ApiConfig(
            base_url=api_base_url,
            api_key=api_key,
            retries=args.api_retries,
            retry_backoff_seconds=args.api_retry_backoff_seconds,
        ),
        timeout_seconds=args.timeout_seconds,
    )

    if read_only and auto_create_pr:
        auto_create_pr = False

    pr_url = args.pr_url.strip()
    source_block: Dict[str, Any]
    if pr_url:
        source_block = {"prUrl": pr_url}
    else:
        source_block = {"repository": resolved_repository_url}
        if resolved_ref:
            source_block["ref"] = resolved_ref

    target_block: Dict[str, Any] = {"branchName": branch, "autoCreatePr": auto_create_pr}
    if auto_create_pr:
        target_block["openAsCursorGithubApp"] = open_as_app
        if open_as_app:
            target_block["skipReviewerRequest"] = skip_reviewer

    create_payload: Dict[str, Any] = {
        "prompt": {"text": final_prompt},
        "model": "default",
        "source": source_block,
        "target": target_block,
    }

    try:
        status_code, response_data, response_raw, auth_mode = api_client.create_agent(create_payload)
    except Exception as err:  # noqa: BLE001
        payload = {"ok": False, "backend": "api", "error": str(err)}
        emit_json(payload) if args.json else emit_text(payload)
        return EXIT_API

    if status_code >= 400:
        payload = {
            "ok": False,
            "backend": "api",
            "error": f"Cursor API create-agent failed (HTTP {status_code})",
            "auth_mode": auth_mode,
            "response": response_data if response_data else response_raw,
        }
        emit_json(payload) if args.json else emit_text(payload)
        return EXIT_API

    agent_id = response_data.get("id")
    status_text = response_data.get("status")
    target = response_data.get("target") or {}
    agent_url = target.get("url")
    pr_url_out = target.get("prUrl")

    if agent_id and args.poll_max_attempts > 0:
        for _ in range(args.poll_max_attempts):
            if status_text in TERMINAL_STATUSES:
                break
            if args.poll_interval_seconds > 0:
                time.sleep(args.poll_interval_seconds)
            poll_status, poll_data, _, _ = api_client.get_agent(agent_id)
            if poll_status >= 400:
                break
            status_text = poll_data.get("status", status_text)
            target = poll_data.get("target") or target
            agent_url = target.get("url", agent_url)
            pr_url_out = target.get("prUrl", pr_url_out)

    payload = {
        "ok": True,
        "backend": "api",
        "submitted": True,
        "read_only": read_only,
        "branch": branch,
        "auth_mode": auth_mode,
        "agent_id": agent_id,
        "status": status_text,
        "agent_url": agent_url,
        "pr_url": pr_url_out,
        "source_repository": resolved_repository_url,
        "source_ref": resolved_ref,
        "source_pr_url": pr_url or None,
        "auto_create_pr": auto_create_pr,
    }
    emit_json(payload) if args.json else emit_text(payload)
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        payload = {"ok": False, "error": "Interrupted by user"}
        emit_json(payload) if "--json" in sys.argv else emit_text(payload)
        sys.exit(EXIT_VALIDATION)
