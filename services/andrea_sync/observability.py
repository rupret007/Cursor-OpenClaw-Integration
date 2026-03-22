"""Lightweight hooks for structured logs and metric-style lines (opt-in via env)."""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict


def structured_log(event: str, **fields: Any) -> None:
    if os.environ.get("ANDREA_SYNC_STRUCTURED_LOG", "0") != "1":
        return
    row: Dict[str, Any] = {"event": event, "ts": time.time(), **fields}
    try:
        print(json.dumps(row, default=str), flush=True)
    except (TypeError, ValueError):
        print(f'{{"event":"{event}","error":"log_encode_failed"}}', flush=True)


def metric_log(name: str, **tags: Any) -> None:
    if os.environ.get("ANDREA_SYNC_METRICS_LOG", "0") != "1":
        return
    try:
        print(json.dumps({"metric": name, **tags}, default=str), flush=True)
    except (TypeError, ValueError):
        pass
