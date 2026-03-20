"""
Load local .env files into os.environ (stdlib only).

Mirrored at skills/cursor_handoff/scripts/env_loader.py — keep copies in sync.

- Does not override variables already present in the environment (e.g. exported in shell).
- Values prefer JSON strings (as written by setup_admin.sh); falls back to POSIX shlex for
  hand-edited .env / .env.example lines.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env_line(line: str) -> Optional[Tuple[str, str]]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.lower().startswith("export "):
        line = line[7:].strip()
    if "=" not in line:
        return None
    key, _, raw_val = line.partition("=")
    key = key.strip()
    if not KEY_RE.match(key):
        return None
    raw_val = raw_val.strip()
    if not raw_val:
        return key, ""
    val = _parse_value(raw_val)
    if val is None:
        return None
    return key, val


def _parse_value(raw_val: str) -> Optional[str]:
    try:
        parsed = json.loads(raw_val)
        if isinstance(parsed, str):
            return parsed
    except json.JSONDecodeError:
        pass
    try:
        parts = shlex.split(raw_val, posix=True)
    except ValueError:
        return None
    if len(parts) != 1:
        return None
    return parts[0]


def merge_dotenv_paths(paths: Iterable[Path], *, override: bool = False) -> List[Path]:
    """Merge each existing file into os.environ. Returns paths actually read."""
    loaded: List[Path] = []
    for path in paths:
        resolved = path.expanduser()
        try:
            resolved = resolved.resolve()
        except OSError:
            continue
        if not resolved.is_file():
            continue
        if _merge_file(resolved, override=override):
            loaded.append(resolved)
    return loaded


def _merge_file(path: Path, *, override: bool) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    touched = False
    for line in text.splitlines():
        pair = parse_env_line(line)
        if not pair:
            continue
        key, value = pair
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        touched = True
    return touched
