#!/usr/bin/env python3
"""
Probe Andrea lockstep HTTP endpoint.

Environment:
  ANDREA_SYNC_URL       e.g. http://127.0.0.1:8765 (if unset, probe skipped)
  ANDREA_SYNC_REQUIRED  if 1, missing URL or failed health is exit 1
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    url = (os.environ.get("ANDREA_SYNC_URL") or "").strip().rstrip("/")
    required = os.environ.get("ANDREA_SYNC_REQUIRED", "0") == "1"
    if not url:
        if required:
            print("FAIL: ANDREA_SYNC_URL not set but ANDREA_SYNC_REQUIRED=1", file=sys.stderr)
            return 1
        print("SKIP: andrea_sync health (ANDREA_SYNC_URL unset)")
        return 0
    health = f"{url}/v1/health"
    try:
        req = urllib.request.Request(health, method="GET")
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status != 200:
                print(f"FAIL: status {resp.status} {body[:200]}", file=sys.stderr)
                return 1
    except urllib.error.URLError as e:
        print(f"FAIL: andrea_sync health {e}", file=sys.stderr)
        return 1
    print(f"OK: andrea_sync {body[:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
