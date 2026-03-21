#!/usr/bin/env python3
"""One-shot Telegram getMe timing for SLO checks. Never prints the bot token."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


def main() -> int:
    tok = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not tok:
        print("ok=false error=missing_TELEGRAM_BOT_TOKEN", file=sys.stderr)
        return 1
    url = f"https://api.telegram.org/bot{tok}/getMe"
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            _ = resp.status
    except urllib.error.HTTPError as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        print(f"ok=false http={e.code} elapsed_ms={elapsed_ms}")
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        print(f"ok=false error={type(e).__name__} elapsed_ms={elapsed_ms}")
        return 1
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"ok=false error=invalid_json elapsed_ms={elapsed_ms}")
        return 1
    ok = bool(data.get("ok"))
    print(f"ok={str(ok).lower()} elapsed_ms={elapsed_ms}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
