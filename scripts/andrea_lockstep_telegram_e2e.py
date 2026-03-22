#!/usr/bin/env python3
"""
Andrea lockstep: Telegram webhook E2E helper.

Loads repo .env, cwd .env, ~/andrea-lockstep.env, then optional ANDREA_ENV_FILE.
Never prints secret values.

Commands:
  check-env          Verify required variables are set (names only on failure).
  health             GET ANDREA_SYNC_URL/v1/health (default http://127.0.0.1:8765).
  set-webhook        Register Telegram webhook (needs TELEGRAM_BOT_TOKEN, ANDREA_SYNC_TELEGRAM_SECRET,
                     ANDREA_SYNC_PUBLIC_BASE=https://host  no trailing path).
  webhook-info       Call getWebhookInfo (redacted URL in output + health summary).
  tunnel-and-webhook Start cloudflared quick tunnel to local sync, then set-webhook (requires cloudflared on PATH).
  wait-telegram-task Poll GET /v1/tasks until a telegram-channel task appears or timeout.

Environment:
  TELEGRAM_BOT_TOKEN
  ANDREA_SYNC_TELEGRAM_SECRET
  ANDREA_SYNC_URL          default http://127.0.0.1:8765
  ANDREA_SYNC_PUBLIC_BASE  HTTPS origin only, e.g. https://abc.trycloudflare.com
  ANDREA_ENV_FILE          optional extra dotenv path merged after .env
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import env_loader  # noqa: E402
from services.andrea_sync.adapters import telegram as tg_adapt  # noqa: E402


def repo_root() -> Path:
    return ROOT


def load_env() -> None:
    root = repo_root()
    env_loader.merge_dotenv_paths([root / ".env", Path.cwd() / ".env"], override=False)
    override_paths = [Path.home() / "andrea-lockstep.env"]
    extra = (os.environ.get("ANDREA_ENV_FILE") or "").strip()
    if extra:
        override_paths.append(Path(extra).expanduser())
    env_loader.merge_dotenv_paths(override_paths, override=True)


REQUIRED = (
    "TELEGRAM_BOT_TOKEN",
    "ANDREA_SYNC_TELEGRAM_SECRET",
)


def check_env(strict_internal: bool = False) -> int:
    load_env()
    missing = [k for k in REQUIRED if not (os.environ.get(k) or "").strip()]
    if strict_internal:
        if not (os.environ.get("ANDREA_SYNC_INTERNAL_TOKEN") or "").strip():
            missing.append("ANDREA_SYNC_INTERNAL_TOKEN")
    keys = list(REQUIRED)
    if strict_internal:
        keys.append("ANDREA_SYNC_INTERNAL_TOKEN")
    for k in keys:
        if k in missing:
            print(f"{k}: MISSING")
        else:
            print(f"{k}: OK")
    if missing:
        print(
            "\nAdd these to repo .env (see .env.example) or export them, "
            "or set ANDREA_ENV_FILE=/path/to/extra.env",
            file=sys.stderr,
        )
        return 1
    return 0


def default_sync_url() -> str:
    return (os.environ.get("ANDREA_SYNC_URL") or "http://127.0.0.1:8765").rstrip("/")


def http_json(method: str, url: str, headers: dict | None = None, data: bytes | None = None, timeout: float = 30.0):
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return resp.status, json.loads(raw) if raw.strip() else {}


def cmd_health() -> int:
    load_env()
    url = f"{default_sync_url()}/v1/health"
    try:
        status, body = http_json("GET", url)
    except urllib.error.URLError as e:
        print(f"health: FAIL ({e})", file=sys.stderr)
        return 1
    print(json.dumps({"http_status": status, "body": body}, indent=2))
    return 0 if body.get("ok") else 1


def build_webhook_url(public_base: str, secret: str) -> str:
    use_query = os.environ.get("ANDREA_SYNC_TELEGRAM_URL_QUERY", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    return tg_adapt.build_webhook_url(public_base, secret, use_query=use_query)


def redact_url(u: str) -> str:
    parsed = urllib.parse.urlparse(u)
    q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if "secret" in q:
        q["secret"] = ["***"]
    new_query = urllib.parse.urlencode({k: v[0] if v else "" for k, v in q.items()})
    return urllib.parse.urlunparse(parsed._replace(query=new_query))


def expected_webhook_url_from_env() -> str:
    public = (os.environ.get("ANDREA_SYNC_PUBLIC_BASE") or "").strip().rstrip("/")
    secret = (os.environ.get("ANDREA_SYNC_TELEGRAM_SECRET") or "").strip()
    if not public:
        return ""
    return build_webhook_url(public, secret)


def classify_webhook_health(info: dict, *, expected_url: str = "") -> dict:
    result = info.get("result") if isinstance(info.get("result"), dict) else {}
    current_url = str(result.get("url") or "").strip()
    registered = bool(current_url)
    expected = str(expected_url or "").strip()
    matches_expected = tg_adapt.webhook_urls_match(current_url, expected) if expected else False
    status = "registered"
    reason = "Telegram reports an active webhook URL."
    if not registered:
        status = "unset"
        reason = "Telegram returned an empty webhook URL; no webhook is currently registered."
    elif expected and matches_expected:
        status = "healthy"
        reason = "Telegram webhook matches the expected registration."
    elif expected:
        status = "drifted"
        reason = "Telegram webhook is registered, but it does not match the expected URL."
    elif not expected:
        status = "registered"
        reason = "Telegram webhook is registered, but no local expected URL is configured."
    return {
        "status": status,
        "registered": registered,
        "matches_expected": matches_expected,
        "current_url": redact_url(current_url) if current_url and "secret=" in current_url else current_url,
        "expected_url": redact_url(expected) if expected and "secret=" in expected else expected,
        "reason": reason,
    }


def telegram_api(method: str, token: str, params: dict | None = None) -> dict:
    base = f"https://api.telegram.org/bot{token}/{method}"
    url = base
    if params:
        url = base + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def telegram_post(method: str, token: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cmd_set_webhook() -> int:
    load_env()
    if check_env(strict_internal=False) != 0:
        return 1
    public = (os.environ.get("ANDREA_SYNC_PUBLIC_BASE") or "").strip().rstrip("/")
    if not public.startswith("https://"):
        print(
            "ANDREA_SYNC_PUBLIC_BASE must be https://... (e.g. cloudflared trycloudflare URL)",
            file=sys.stderr,
        )
        return 1
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    secret = os.environ["ANDREA_SYNC_TELEGRAM_SECRET"].strip()
    header_sec = (
        os.environ.get("ANDREA_SYNC_TELEGRAM_WEBHOOK_SECRET") or ""
    ).strip() or secret
    use_query = os.environ.get("ANDREA_SYNC_TELEGRAM_URL_QUERY", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    wh_url = (
        build_webhook_url(public, secret)
        if use_query and secret
        else f"{public.rstrip('/')}/v1/telegram/webhook"
    )
    print("webhook_url:", redact_url(wh_url))
    payload: dict = {"url": wh_url, "drop_pending_updates": False}
    if header_sec:
        payload["secret_token"] = header_sec[:256]
    res = telegram_post("setWebhook", token, payload)
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


def cmd_webhook_info(*, require_registered: bool = False, require_match: bool = False) -> int:
    load_env()
    if check_env(strict_internal=False) != 0:
        return 1
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    res = telegram_api("getWebhookInfo", token)
    # Redact URL query in result
    r = dict(res)
    if r.get("result") and isinstance(r["result"], dict):
        inner = dict(r["result"])
        u = inner.get("url")
        if isinstance(u, str):
            inner["url"] = redact_url(u) if "secret=" in u else u
        r["result"] = inner
    r["webhook_health"] = classify_webhook_health(
        res,
        expected_url=expected_webhook_url_from_env(),
    )
    print(json.dumps(r, indent=2))
    if not res.get("ok"):
        return 1
    health = r["webhook_health"]
    if require_registered and not health.get("registered"):
        return 1
    if require_match and not health.get("matches_expected"):
        return 1
    return 0


def _read_cloudflared_url(proc: subprocess.Popen, timeout_sec: float = 90.0) -> str:
    assert proc.stdout is not None
    deadline = time.time() + timeout_sec
    pat = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line and proc.poll() is not None:
            break
        m = pat.search(line)
        if m:
            return m.group(0)
    raise RuntimeError("timeout waiting for trycloudflare.com URL from cloudflared")


def cmd_tunnel_and_webhook() -> int:
    from shutil import which

    load_env()
    if check_env(strict_internal=False) != 0:
        return 1
    local = default_sync_url()
    if not which("cloudflared"):
        print(
            "cloudflared not on PATH. Install: brew install cloudflared\n"
            "If brew fails with permissions: sudo chown -R $(whoami) /usr/local/Homebrew",
            file=sys.stderr,
        )
        return 1
    try:
        status, _ = http_json("GET", f"{local}/v1/health", timeout=5.0)
        if status != 200:
            raise RuntimeError(f"health HTTP {status}")
    except Exception as e:  # noqa: BLE001
        print(
            f"Local andrea_sync not reachable at {local}/v1/health ({e}).\n"
            "Start: export ANDREA_SYNC_TELEGRAM_SECRET=... ANDREA_SYNC_INTERNAL_TOKEN=...; "
            "python3 scripts/andrea_sync_server.py",
            file=sys.stderr,
        )
        return 1

    cmd = ["cloudflared", "tunnel", "--no-autoupdate", "--url", local]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        pub = _read_cloudflared_url(proc)
    except Exception as e:  # noqa: BLE001
        proc.terminate()
        print(f"cloudflared: {e}", file=sys.stderr)
        return 1

    os.environ["ANDREA_SYNC_PUBLIC_BASE"] = pub
    print("public_base:", pub)
    rc = cmd_set_webhook()
    if rc != 0:
        proc.terminate()
        return rc
    print(
        "\nTunnel is running (PID {}). Leave this terminal open while testing Telegram.\n"
        "Ctrl+C stops the tunnel — you will need to setWebhook again with a new URL.\n"
        "Next: message your bot, then in another shell:\n"
        "  curl -sS {}/v1/tasks?limit=10 | python3 -m json.tool\n".format(proc.pid, local),
    )
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
    return 0


def cmd_wait_telegram_task(timeout_sec: float, interval_sec: float) -> int:
    load_env()
    base = default_sync_url()
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            _, body = http_json("GET", f"{base}/v1/tasks?limit=50", timeout=10.0)
        except urllib.error.URLError:
            time.sleep(interval_sec)
            continue
        tasks = body.get("tasks") or []
        for t in tasks:
            if isinstance(t, dict) and t.get("channel") == "telegram":
                print(json.dumps({"found": t}, indent=2))
                return 0
        time.sleep(interval_sec)
    print("timeout: no telegram task in /v1/tasks yet (did you message the bot?)", file=sys.stderr)
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description="Andrea lockstep Telegram E2E helper")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check-env", help="Verify TELEGRAM_BOT_TOKEN + ANDREA_SYNC_TELEGRAM_SECRET")
    sub.add_parser("check-env-strict", help="Also require ANDREA_SYNC_INTERNAL_TOKEN")

    sub.add_parser("health", help="GET /v1/health on ANDREA_SYNC_URL")
    sub.add_parser("set-webhook", help="setWebhook using ANDREA_SYNC_PUBLIC_BASE")
    wh = sub.add_parser("webhook-info", help="getWebhookInfo with webhook health summary")
    wh.add_argument("--require-registered", action="store_true")
    wh.add_argument("--require-match", action="store_true")
    sub.add_parser("tunnel-and-webhook", help="cloudflared quick tunnel + set-webhook")
    wt = sub.add_parser("wait-telegram-task", help="Poll /v1/tasks for channel=telegram")
    wt.add_argument("--timeout-sec", type=float, default=120.0)
    wt.add_argument("--interval-sec", type=float, default=3.0)

    args = p.parse_args()
    if args.cmd == "check-env":
        return check_env(strict_internal=False)
    if args.cmd == "check-env-strict":
        return check_env(strict_internal=True)
    if args.cmd == "health":
        return cmd_health()
    if args.cmd == "set-webhook":
        return cmd_set_webhook()
    if args.cmd == "webhook-info":
        return cmd_webhook_info(
            require_registered=bool(args.require_registered),
            require_match=bool(args.require_match),
        )
    if args.cmd == "tunnel-and-webhook":
        return cmd_tunnel_and_webhook()
    if args.cmd == "wait-telegram-task":
        return cmd_wait_telegram_task(args.timeout_sec, args.interval_sec)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
