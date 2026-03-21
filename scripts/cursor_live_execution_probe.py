#!/usr/bin/env python3
"""
Cursor live execution probe.

Goals:
- Deterministic, offline checks (no network calls).
- Verifies that the runtime can execute subprocesses and do basic filesystem I/O.
- Produces machine-readable JSON by default, suitable for CI/probe collection.

This is intentionally small and safe: it does not read secrets, and it avoids
printing environment variables by default.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    probe: str
    ts_epoch_s: int
    python: str
    platform: str
    cwd: str
    checks: dict
    error: str | None = None


def _run_cmd(argv: list[str], timeout_s: float) -> tuple[int, str, str]:
    p = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def _self_test(timeout_s: float) -> dict:
    checks: dict[str, object] = {}

    # 1) Subprocess execution (shell-free; stable output)
    rc, out, err = _run_cmd([sys.executable, "-c", "print('ok')"], timeout_s=timeout_s)
    checks["subprocess_python_rc"] = rc
    checks["subprocess_python_stdout"] = out.strip()
    checks["subprocess_python_stderr_nonempty"] = bool(err.strip())
    if rc != 0 or out.strip() != "ok":
        raise RuntimeError(f"subprocess failed (rc={rc}, out={out!r}, err={err!r})")

    # 2) Temp file write/read (workspace FS sanity)
    with tempfile.TemporaryDirectory(prefix="cursor-live-probe-") as td:
        path = os.path.join(td, "probe.txt")
        payload = f"probe:{int(time.time())}"
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
        with open(path, "r", encoding="utf-8") as f:
            got = f.read()
        checks["tmpfile_roundtrip_ok"] = got == payload
        if got != payload:
            raise RuntimeError("tmpfile roundtrip mismatch")

    # 3) Clock sanity (non-decreasing monotonic)
    t1 = time.monotonic()
    time.sleep(0.01)
    t2 = time.monotonic()
    checks["monotonic_increases"] = t2 >= t1
    if t2 < t1:
        raise RuntimeError("monotonic clock moved backwards")

    return checks


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Cursor live execution probe (offline).")
    ap.add_argument("--json", action="store_true", help="Emit JSON (default).")
    ap.add_argument(
        "--self-test",
        action="store_true",
        help="Run deterministic offline checks (recommended).",
    )
    ap.add_argument(
        "--timeout-s",
        type=float,
        default=10.0,
        help="Timeout in seconds for subprocess checks (default: 10).",
    )
    args = ap.parse_args(argv)

    ts = int(time.time())
    checks: dict[str, object] = {}
    err: str | None = None

    try:
        if args.self_test:
            checks.update(_self_test(timeout_s=args.timeout_s))
        ok = True
    except Exception as e:  # pragma: no cover - error path
        ok = False
        err = str(e)

    result = ProbeResult(
        ok=ok,
        probe="cursor_live_execution_probe",
        ts_epoch_s=ts,
        python=sys.version.split()[0],
        platform=f"{platform.system()} {platform.release()}",
        cwd=os.getcwd(),
        checks=checks,
        error=err,
    )

    # Default to JSON: probes should be machine-readable and stable.
    if args.json or True:
        json.dump(asdict(result), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:  # pragma: no cover
        print(result)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

