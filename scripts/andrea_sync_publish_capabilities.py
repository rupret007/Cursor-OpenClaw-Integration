#!/usr/bin/env python3
"""
Publish `scripts/andrea_capabilities.py --json` into lockstep via PublishCapabilitySnapshot.

Requires:
  ANDREA_SYNC_INTERNAL_TOKEN
  ANDREA_SYNC_URL (default http://127.0.0.1:8765)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    tok = (os.environ.get("ANDREA_SYNC_INTERNAL_TOKEN") or "").strip()
    if not tok:
        print("error: ANDREA_SYNC_INTERNAL_TOKEN required", file=sys.stderr)
        return 1
    base = (os.environ.get("ANDREA_SYNC_URL") or "http://127.0.0.1:8765").rstrip("/")
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "andrea_capabilities.py"), "--json"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout or "capabilities failed\n")
        return proc.returncode
    try:
        matrix = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(f"error: invalid capabilities JSON: {e}", file=sys.stderr)
        return 1
    body = json.dumps(
        {
            "command_type": "PublishCapabilitySnapshot",
            "channel": "internal",
            "payload": matrix,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/v1/commands",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {tok}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            print(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        print(e.read().decode("utf-8", errors="replace"), file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
