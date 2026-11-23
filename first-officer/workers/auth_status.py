"""Read subscription-worker sign-in status without launching a model."""

import json
import os
from pathlib import Path
import re
import subprocess


def _status(binary, names, args, run):
    path = Path(binary)
    if not path.is_absolute() or path.name not in names or not path.is_file() or not os.access(path, os.X_OK):
        return None
    env = {name: os.environ[name] for name in ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR")
           if name in os.environ}
    try:
        result = run([str(path), *args], env=env, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None
    return result if len(result.stdout) + len(result.stderr) <= 16_384 else None


def claude_subscription_status(binary, *, run=subprocess.run):
    result = _status(binary, {"claude"}, ("auth", "status"), run)
    if result is None:
        return "inconclusive"
    try:
        data = json.loads(result.stdout)
    except (ValueError, RecursionError):
        return "inconclusive"
    if not isinstance(data, dict):
        return "inconclusive"
    if result.returncode == 0 and data.get("loggedIn") is True:
        return "subscription-sign-in" if data.get("authMethod") == "claude.ai" else "other-sign-in"
    if result.returncode == 1 and data.get("loggedIn") is False:
        return "not-signed-in"
    return "inconclusive"


def cursor_sign_in_status(binary, *, run=subprocess.run):
    result = _status(binary, {"agent", "cursor-agent"}, ("status",), run)
    if result is None:
        return "inconclusive"
    output = result.stdout + "\n" + result.stderr
    if re.search(r"\b(?:not authenticated|not logged in)\b", output, re.I):
        return "not-signed-in"
    if result.returncode == 0 and re.search(r"\b(?:logged in as|authenticated as|login successful)\b", output, re.I):
        return "signed-in"
    return "inconclusive"
