"""Global kill switch: env, optional flag file, and persisted meta (SQLite)."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from .store import get_meta, set_meta

META_KEY = "kill_switch_state"
DEFAULT_KILL_FILE = "andrea_sync.kill"


def default_kill_file_path() -> Path:
    raw = os.environ.get("ANDREA_SYNC_KILL_FILE")
    if raw:
        return Path(raw).expanduser()
    # Co-locate with the active DB so tests / alternate DB paths do not touch repo data/.
    db_override = os.environ.get("ANDREA_SYNC_DB")
    if db_override:
        p = Path(db_override).expanduser()
        return p.parent / f"{p.name}.kill"
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "data" / DEFAULT_KILL_FILE


def _state_from_meta(conn: Optional[sqlite3.Connection]) -> Optional[Dict[str, Any]]:
    if conn is None:
        return None
    raw = get_meta(conn, META_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def is_kill_switch_engaged(conn: Optional[sqlite3.Connection] = None) -> bool:
    if (os.environ.get("ANDREA_SYNC_KILL_SWITCH") or "").strip() in ("1", "true", "yes", "on"):
        return True
    try:
        if default_kill_file_path().is_file():
            return True
    except OSError:
        pass
    st = _state_from_meta(conn)
    if isinstance(st, dict) and st.get("engaged") is True:
        return True
    return False


def engage_kill_switch(
    conn: sqlite3.Connection,
    *,
    reason: str = "",
    source: str = "api",
) -> None:
    payload = {
        "engaged": True,
        "reason": str(reason)[:2000],
        "source": str(source)[:200],
    }
    set_meta(conn, META_KEY, json.dumps(payload, ensure_ascii=False))
    try:
        p = default_kill_file_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def release_kill_switch(conn: sqlite3.Connection) -> None:
    set_meta(conn, META_KEY, json.dumps({"engaged": False}, ensure_ascii=False))
    try:
        p = default_kill_file_path()
        if p.is_file():
            p.unlink()
    except OSError:
        pass


def kill_switch_status(conn: Optional[sqlite3.Connection]) -> Dict[str, Any]:
    engaged = is_kill_switch_engaged(conn)
    meta_st = _state_from_meta(conn)
    return {
        "engaged": engaged,
        "env_flag": (os.environ.get("ANDREA_SYNC_KILL_SWITCH") or "").strip()
        in ("1", "true", "yes", "on"),
        "file": str(default_kill_file_path()),
        "file_present": default_kill_file_path().is_file(),
        "meta": meta_st,
    }
