"""
Verify-before-deny: capability truth must be fresh before claiming a skill absent.

Expects digest JSON shaped like `scripts/andrea_capabilities.py --json` output
(with `rows` list and optional `summary`).
"""
from __future__ import annotations

import json
import re
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


def _normalize_skill_query(value: str) -> Tuple[str, str]:
    clean = re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower())
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean, clean.replace(" ", "")


def _row_match_tokens(row: Dict[str, Any]) -> List[str]:
    tokens: List[str] = []

    def add(value: Any) -> None:
        if value is None:
            return
        text = str(value).strip().lower()
        if not text:
            return
        for item in {text, *(_normalize_skill_query(text))}:
            if item and item not in tokens:
                tokens.append(item)

    add(row.get("id"))
    add(row.get("detail"))
    aliases = row.get("aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            add(alias)
    return tokens


def resolve_skill_truth(
    conn: sqlite3.Connection,
    skill_key: str,
    *,
    max_age_seconds: float = 900.0,
) -> Dict[str, Any]:
    key = str(skill_key).strip().lower()
    normalized_key, collapsed_key = _normalize_skill_query(key)
    info = get_capability_digest(conn)
    digest = info.get("digest")
    ts = info.get("published_ts")
    if digest is None or ts is None:
        return {
            "verified": False,
            "status": "stale_needs_refresh",
            "reason": "capability_digest_missing",
            "must_refresh": True,
            "normalized_query": normalized_key,
            "collapsed_query": collapsed_key,
            "matches": [],
        }
    age = time.time() - float(ts)
    if age > max_age_seconds:
        return {
            "verified": False,
            "status": "stale_needs_refresh",
            "reason": "capability_digest_stale",
            "age_seconds": age,
            "max_age_seconds": max_age_seconds,
            "must_refresh": True,
            "normalized_query": normalized_key,
            "collapsed_query": collapsed_key,
            "matches": [],
        }

    rows: List[Dict[str, Any]] = digest.get("rows") if isinstance(digest, dict) else []
    if not isinstance(rows, list):
        rows = []

    matches: List[Dict[str, Any]] = []
    wanted = {key, normalized_key, collapsed_key, f"skill:{key}", f"skill:{collapsed_key}"}
    wanted.discard("")
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_tokens = set(_row_match_tokens(row))
        if wanted.intersection(row_tokens):
            matches.append(row)

    if not matches:
        return {
            "verified": False,
            "status": "unknown_needs_probe",
            "reason": "skill_unknown_probe_required",
            "must_probe": True,
            "normalized_query": normalized_key,
            "collapsed_query": collapsed_key,
            "matches": [],
        }

    for row in matches:
        if str(row.get("status") or "").lower() == "ready":
            return {
                "verified": True,
                "status": "verified_available",
                "reason": "verify_before_deny:skill_ready",
                "matches": matches,
                "normalized_query": normalized_key,
                "collapsed_query": collapsed_key,
            }

    for row in matches:
        availability = str(row.get("availability") or "").strip().lower()
        if availability == "installed_but_not_eligible":
            return {
                "verified": True,
                "status": "installed_but_not_eligible",
                "reason": "verify_before_deny:skill_installed_but_not_eligible",
                "matches": matches,
                "normalized_query": normalized_key,
                "collapsed_query": collapsed_key,
            }

    for row in matches:
        if str(row.get("status") or "").lower() == "ready_with_limits":
            return {
                "verified": True,
                "status": "verified_unavailable",
                "reason": "verify_before_deny:skill_ready_with_limits",
                "matches": matches,
                "normalized_query": normalized_key,
                "collapsed_query": collapsed_key,
            }

    return {
        "verified": True,
        "status": "verified_unavailable",
        "reason": "digest_shows_blocked",
        "matches": matches,
        "normalized_query": normalized_key,
        "collapsed_query": collapsed_key,
    }


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
    truth = resolve_skill_truth(conn, skill_key, max_age_seconds=max_age_seconds)
    status = str(truth.get("status") or "")
    if truth.get("must_refresh"):
        return {
            "may_claim_absent": False,
            "reason": str(truth.get("reason") or "capability_digest_stale"),
            "age_seconds": truth.get("age_seconds"),
            "max_age_seconds": truth.get("max_age_seconds"),
            "must_refresh": True,
            "matches": truth.get("matches", []),
        }
    if truth.get("must_probe"):
        return {
            "may_claim_absent": False,
            "reason": str(truth.get("reason") or "skill_unknown_probe_required"),
            "must_probe": True,
            "matches": truth.get("matches", []),
        }
    if status == "verified_available":
        return {
            "may_claim_absent": False,
            "reason": str(truth.get("reason") or "verify_before_deny:skill_ready"),
            "matches": truth.get("matches", []),
        }
    if status == "installed_but_not_eligible":
        return {
            "may_claim_absent": False,
            "reason": str(
                truth.get("reason") or "verify_before_deny:skill_installed_but_not_eligible"
            ),
            "matches": truth.get("matches", []),
        }
    return {
        "may_claim_absent": True,
        "reason": str(truth.get("reason") or "digest_shows_blocked"),
        "matches": truth.get("matches", []),
    }
