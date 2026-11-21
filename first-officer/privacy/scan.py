#!/usr/bin/env python3
"""Scan the checkout, its reachable history and its logs for secrets and personal data.

A finding names the rule and the location. It never includes the matched text.
Known synthetic fixtures are the only credential strings that are ignored, and a
different string of the same shape is a failure. The command exits 1 when any
finding remains. History includes one patch per parent of a merge, so a value
that appears only in a merge result is still a finding.
"""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys


FIXTURES = frozenset({
    "sk-live-secretvalue",
    "sk-abcd1234efgh5678",
})
SKIP = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"}
LOCAL_DATABASE = {
    Path("data/andrea_sync.db"),
    Path("data/andrea_sync.db-wal"),
    Path("data/andrea_sync.db-shm"),
}
LOCAL_DATABASE_CAP = 8 * 1024 * 1024
BLOCKED = (
    Path("/opt/openclaw-fo"),
    Path.home() / ".openclaw",
    Path.home() / ".local/state/openclaw-conductor",
    Path.home() / "work/lanes/openclaw-backup2-20261118-1340",
)
MAX_BYTES = 1_048_576
CREDENTIAL = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{16,}|"
    r"AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|\d{8,10}:[A-Za-z0-9_-]{35})"
)
BEARER = re.compile(r"(?<![A-Za-z0-9])Bearer ([A-Za-z0-9._~+/-]{16,})")
NANP = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?(?:\(\d{3}\)[\s.-]?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\d)")
E164 = re.compile(r"(?<![A-Za-z0-9])\+[1-9]\d{10,14}(?!\d)")
ADDRESS = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
EMAIL = re.compile(r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}\b")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----")
DOC_NETS = {(192, 0, 2), (198, 51, 100), (203, 0, 113)}
EXAMPLE_ADDRESSES = {(8, 8, 8, 8), (1, 1, 1, 1), (1, 2, 3, 4)}
EMAIL_HOSTS = {"example.com", "example.org", "example.net", "example.invalid", "github.com", "localhost"}
SHA = re.compile(r"[0-9a-f]{40}")
TRAILER = re.compile(
    r"(?i)(?:signed-off-by|co-authored-by|reviewed-by|acked-by|reported-by|tested-by|cc):"
    r"[ \t]+[^<>]+[ \t]+<(?P<email>[^<>]+)>"
)


def commit_message(body):
    """Exempt the email in a complete identity trailer from the email rule."""
    lines = []
    for line in str(body).splitlines():
        line = line.strip()
        identity = TRAILER.fullmatch(line)
        if identity and EMAIL.fullmatch(identity["email"]):
            start, end = identity.span("email")
            line = line[:start] + line[start:end].replace("@", " ") + line[end:]
        lines.append(line)
    return "\n".join(lines)


def refuse_live(path):
    resolved = Path(path).resolve()
    for marker in BLOCKED:
        try:
            marker = marker.resolve()
        except OSError:
            continue
        if resolved == marker or marker in resolved.parents:
            raise ValueError("The scan must stay off the live gateway")


def public_name(name):
    text = name.as_posix() if isinstance(name, Path) else str(name)
    if findings_in_text(text):
        return "[redacted]"
    return text


def _phone_digits(raw):
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def fictional_phone(raw):
    """The reserved 555-0100 through 555-0199 lines are fixtures, not a person."""
    digits = _phone_digits(raw)
    return len(digits) == 10 and digits[3:6] == "555" and digits[6:].startswith("01")


def personal_address(raw):
    try:
        parts = tuple(int(piece) for piece in raw.split("."))
    except ValueError:
        return False
    if len(parts) != 4 or any(part > 255 for part in parts):
        return False
    if parts[0] in {0, 127} or parts[0] >= 224 or parts in EXAMPLE_ADDRESSES:
        return False
    if parts[0] == 169 and parts[1] == 254:
        return False
    if parts[:3] in DOC_NETS:
        return False
    return True


def personal_email(raw):
    host = raw.rsplit("@", 1)[-1].lower()
    return host not in EMAIL_HOSTS and not host.endswith(".example.com")


def findings_in_text(text):
    """Return rule and line only. The matched characters are not retained."""
    found = []
    seen = set()
    for number, line in enumerate(str(text).splitlines(), 1):
        rules = []
        if PRIVATE_KEY.search(line):
            rules.append("private-key")
        for match in CREDENTIAL.finditer(line):
            if match.group() not in FIXTURES:
                rules.append("credential")
                break
        for match in BEARER.finditer(line):
            token = match.group(1)
            if any(character.isdigit() for character in token) and not re.fullmatch(r"[A-Z0-9_]+", token):
                rules.append("credential")
                break
        for match in list(E164.finditer(line)) + list(NANP.finditer(line)):
            if not fictional_phone(match.group()):
                rules.append("phone")
                break
        for match in ADDRESS.finditer(line):
            if personal_address(match.group()):
                rules.append("address")
                break
        for match in EMAIL.finditer(line):
            if personal_email(match.group()):
                rules.append("email")
                break
        for rule in rules:
            if (rule, number) not in seen:
                seen.add((rule, number))
                found.append({"rule": rule, "line": number})
    return found


def _root(path):
    refuse_live(path)
    root = Path(path)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("The scan root must be a directory")
    return root.resolve()


def _descend(root, directory, names, findings):
    """Skip named tooling trees. A directory symlink is a finding and is not followed."""
    kept = []
    for name in names:
        if name in SKIP:
            continue
        path = Path(directory) / name
        if path.is_symlink():
            findings.append({"rule": "symlink", "path": public_name(path.relative_to(root)), "line": 0})
            continue
        kept.append(name)
    return kept


def scan_files(root):
    findings = []
    count = 0
    for directory, names, files in os.walk(root):
        names[:] = _descend(root, directory, names, findings)
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                findings.append({"rule": "symlink", "path": public_name(path.relative_to(root)), "line": 0})
                continue
            count += 1
            findings.extend(_file_findings(root, path))
    return findings, count


def _file_findings(root, path):
    relative = path.relative_to(root)
    shown = public_name(relative)
    try:
        info = path.stat()
    except OSError:
        return [{"rule": "unreadable", "path": shown, "line": 0}]
    if relative in LOCAL_DATABASE:
        if info.st_size > LOCAL_DATABASE_CAP:
            return [{"rule": "oversized", "path": shown, "line": 0}]
        try:
            payload = path.read_bytes()
        except OSError:
            return [{"rule": "unreadable", "path": shown, "line": 0}]
        if b"\0" in payload:
            return []
    elif info.st_size > MAX_BYTES:
        return [{"rule": "oversized", "path": shown, "line": 0}]
    else:
        try:
            payload = path.read_bytes()
        except OSError:
            return [{"rule": "unreadable", "path": shown, "line": 0}]
        if b"\0" in payload:
            return [{"rule": "binary", "path": shown, "line": 0}]
    text = payload.decode("utf-8", "replace")
    located = [{"rule": item["rule"], "path": "[redacted]", "line": 0}
               for item in findings_in_text(relative.as_posix())]
    for finding in findings_in_text(text):
        located.append({"rule": finding["rule"], "path": shown, "line": finding["line"]})
    return located


def _git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, errors="replace")
    if result.returncode:
        raise ValueError("The history scan failed")
    return result.stdout


def _note_sensitive_path(findings, seen, commit, text):
    """A secret in a path is a finding. The report never keeps the path text."""
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    for finding in findings_in_text(text):
        key = (finding["rule"], commit, "pathname")
        if key not in seen:
            seen.add(key)
            findings.append({"rule": finding["rule"], "path": "[redacted]", "commit": commit})


def _unquote_git(text, index):
    """Decode one Git C-quoted token, starting at the opening quote."""
    if index >= len(text) or text[index] != '"':
        raise ValueError("The history scan failed")
    index += 1
    chars = []
    while index < len(text):
        character = text[index]
        if character == '"':
            return "".join(chars), index + 1
        if character != "\\":
            chars.append(character)
            index += 1
            continue
        index += 1
        if index >= len(text):
            break
        escaped = text[index]
        simple = {
            "n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"',
            "a": "\a", "b": "\b", "f": "\f", "v": "\v",
        }
        if escaped in simple:
            chars.append(simple[escaped])
            index += 1
            continue
        if escaped in "01234567":
            octal = escaped
            index += 1
            for _extra in range(2):
                if index < len(text) and text[index] in "01234567":
                    octal += text[index]
                    index += 1
                else:
                    break
            chars.append(chr(int(octal, 8)))
            continue
        chars.append(escaped)
        index += 1
    return "".join(chars), index


def _git_tokens(body):
    tokens = []
    index = 0
    while index < len(body):
        if body[index] == " ":
            index += 1
            continue
        if body[index] == '"':
            token, index = _unquote_git(body, index)
            tokens.append(token)
            continue
        end = body.find(" ", index)
        if end == -1:
            tokens.append(body[index:])
            break
        tokens.append(body[index:end])
        index = end
    return tokens


def _path_side(token):
    if token.startswith("a/") or token.startswith("b/"):
        return token[2:]
    return token


def _metadata_path(raw):
    rest = raw.split(" ", 2)[-1]
    if rest.startswith('"'):
        try:
            name, _index = _unquote_git(rest, 0)
        except ValueError:
            name = rest
        return name
    return rest


def scan_history(root):
    git_dir = root / ".git"
    if git_dir.is_file():
        pointer = git_dir.read_text(encoding="utf-8", errors="replace")
        if not pointer.startswith("gitdir: "):
            raise ValueError("The history scan failed")
        git_dir = Path(pointer.split("gitdir:", 1)[1].strip())
        if not git_dir.is_absolute():
            git_dir = (root / git_dir).resolve()
    if (git_dir / "shallow").exists():
        raise ValueError("The history scan needs the full clone")
    count = int(_git(root, "rev-list", "--all", "--count").strip() or "0")
    if count <= 0:
        raise ValueError("The history scan needs the reachable commits")
    findings = []
    seen = set()
    messages = _git(root, "log", "--all", "--pretty=format:%H%x1e%B%x1e")
    chunks = messages.split("\x1e")
    for index in range(0, len(chunks) - 1, 2):
        commit = chunks[index].strip()
        if not SHA.fullmatch(commit):
            raise ValueError("The history scan failed")
        for finding in findings_in_text(commit_message(chunks[index + 1])):
            key = (finding["rule"], commit, "message")
            if key not in seen:
                seen.add(key)
                findings.append({"rule": finding["rule"], "path": "message", "commit": commit})
    # `-m` emits one patch per merge parent. Plain `git log -p` omits merge
    # results, so a credential that exists only in a merge resolution is missed.
    patch = _git(root, "log", "--all", "-m", "-p", "--no-color", "-U0", "--pretty=format:%H")
    commit = ""
    path = ""
    for raw in patch.splitlines():
        if SHA.fullmatch(raw):
            commit = raw
            path = ""
            continue
        if raw.startswith("diff --git "):
            names = [_path_side(token) for token in _git_tokens(raw[len("diff --git "):]) if token]
            for name in names:
                _note_sensitive_path(findings, seen, commit, name)
            shown = names[-1] if names else ""
            path = public_name(shown) if shown else "[redacted]"
            continue
        if raw.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            _note_sensitive_path(findings, seen, commit, _metadata_path(raw))
            continue
        if raw.startswith(("Binary files ", "GIT binary patch")):
            key = ("binary", commit, path)
            if key not in seen:
                seen.add(key)
                findings.append({"rule": "binary", "path": path or "[redacted]", "commit": commit})
            continue
        if not commit or not path or not raw.startswith(("+", "-")) or raw.startswith(("+++", "---")):
            continue
        for finding in findings_in_text(raw[1:]):
            key = (finding["rule"], commit, path)
            if key not in seen:
                seen.add(key)
                findings.append({"rule": finding["rule"], "path": path, "commit": commit})
    return findings, count


def scan_logs(directory):
    """Scan one log directory. Locations are relative to that directory."""
    if directory is None:
        return [], 0
    root = _root(directory)
    findings = []
    count = 0
    for directory_name, names, files in os.walk(root):
        names[:] = _descend(root, directory_name, names, findings)
        for name in files:
            path = Path(directory_name) / name
            if path.suffix != ".log" and "logs" not in path.relative_to(root).parts:
                continue
            count += 1
            for finding in _file_findings(root, path):
                findings.append(finding)
    return findings, count


def default_logs(root):
    findings = []
    count = 0
    for directory, names, files in os.walk(root):
        names[:] = _descend(root, directory, names, findings)
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root)
            if path.suffix != ".log" and "logs" not in relative.parts:
                continue
            count += 1
            # The file walk already reports the contents. Count the log here.
            if path.is_symlink() or not path.is_file():
                findings.append({"rule": "symlink", "path": relative.as_posix(), "line": 0})
    return findings, count


def survey(root, logs=None):
    root = _root(root)
    file_findings, files = scan_files(root)
    history_findings, commits = scan_history(root)
    if logs is None:
        log_findings, log_count = default_logs(root)
    else:
        log_findings, log_count = scan_logs(logs)
    return {
        "files": files,
        "commits": commits,
        "logs": log_count,
        "findings": file_findings + history_findings + log_findings,
    }


def report(result):
    if result["findings"]:
        lines = ["Secret scan failed."]
        for finding in result["findings"]:
            place = finding["path"]
            if "line" in finding and finding["line"]:
                place = f"{place}:{finding['line']}"
            if "commit" in finding:
                place = f"{finding['commit']} {place}"
            lines.append(f"{finding['rule']} {place}")
        return "\n".join(lines), 1
    return (f"Secret scan clean. Files {result['files']}. "
            f"History commits {result['commits']}. Logs {result['logs']}.", 0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--logs", type=Path)
    args = parser.parse_args(argv)
    try:
        text, code = report(survey(args.root, args.logs))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        text = str(error) if type(error) is ValueError else "The secret scan failed"
        code = 1
    print(text, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
