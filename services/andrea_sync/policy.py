"""
Verify-before-deny: capability truth must be fresh before claiming a skill absent.

Expects digest JSON shaped like `scripts/andrea_capabilities.py --json` output
(with `rows` list and optional `summary`).
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from .store import get_meta

META_DIGEST_KEY = "capability_digest_json"
META_DIGEST_TS_KEY = "capability_digest_ts"


def _parse_digest(raw: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Optional[float]]:
    if not raw:
        return None, None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(obj, dict):
        return None, None
    ts = obj.get("published_ts")
    if ts is not None:
        try:
            return obj, float(ts)
        except (TypeError, ValueError):
            return obj, None
    return obj, None


def get_capability_digest(conn: sqlite3.Connection) -> Dict[str, Any]:
    raw = get_meta(conn, META_DIGEST_KEY)
    ts_raw = get_meta(conn, META_DIGEST_TS_KEY)
    digest, embedded_ts = _parse_digest(raw)
    ts_val: Optional[float] = None
    if ts_raw:
        try:
            ts_val = float(ts_raw)
        except ValueError:
            ts_val = None
    if ts_val is None:
        ts_val = embedded_ts
    return {
        "present": digest is not None,
        "published_ts": ts_val,
        "digest": digest,
    }


def digest_age_seconds(conn: sqlite3.Connection) -> Optional[float]:
    info = get_capability_digest(conn)
    ts = info.get("published_ts")
    if ts is None:
        return None
    return max(0.0, time.time() - float(ts))


def evaluate_skill_absence_claim(
    conn: sqlite3.Connection,
    skill_key: str,
    *,
    max_age_seconds: float = 900.0,
) -> Dict[str, Any]:
    """
    When a channel wants to tell the user a skill is missing/unavailable, require fresh digest.

    skill_key: e.g. "apple-reminders" or matrix id like "skill:telegram"
    """
    key = str(skill_key).strip().lower()
    info = get_capability_digest(conn)
    digest = info.get("digest")
    ts = info.get("published_ts")
    if digest is None or ts is None:
        return {
            "may_claim_absent": False,
            "reason": "capability_digest_missing",
            "must_refresh": True,
        }
    age = time.time() - float(ts)
    if age > max_age_seconds:
        return {
            "may_claim_absent": False,
            "reason": "capability_digest_stale",
            "age_seconds": age,
            "max_age_seconds": max_age_seconds,
            "must_refresh": True,
        }

    rows: List[Dict[str, Any]] = digest.get("rows") if isinstance(digest, dict) else []
    if not isinstance(rows, list):
        rows = []

    matches: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or "").lower()
        if rid == key or rid == f"skill:{key}" or rid.endswith(f":{key}"):
            matches.append(r)
        elif key.startswith("skill:") and rid == key:
            matches.append(r)

    if not matches:
        return {
            "may_claim_absent": True,
            "reason": "skill_not_listed_in_digest_treat_as_unknown",
            "matches": [],
        }

    # If any match says ready-ish, deny absence claim
    blocking_statuses = {"blocked"}
    readyish = {"ready"}
    limbo = {"ready_with_limits"}

    worst = None
    for m in matches:
        st = str(m.get("status") or "").lower()
        worst = st
        if st in readyish:
            return {
                "may_claim_absent": False,
                "reason": "verify_before_deny:skill_ready",
                "matches": matches,
            }
        if st in limbo:
            return {
                "may_claim_absent": False,
                "reason": "verify_before_deny:skill_ready_with_limits",
                "matches": matches,
            }
        if st in blocking_statuses:
            return {
                "may_claim_absent": True,
                "reason": "digest_shows_blocked",
                "matches": matches,
            }

    return {
        "may_claim_absent": True,
        "reason": f"unhandled_status:{worst}",
        "matches": matches,
    }
