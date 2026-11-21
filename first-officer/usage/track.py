#!/usr/bin/env python3
"""Track the lane's Codex and Claude windows and hand off before either runs out."""

import argparse
from datetime import datetime
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo


CHICAGO = ZoneInfo("America/Chicago")
WARN_PERCENT = 90.0
STOP_PERCENT = 97.0
OK, WARNED, STOPPED = 0, 3, 4
HANDOFF = "HANDOFF.md"
PROTECTED = ("main", "master")
CLAUDE_HEADER = "anthropic-ratelimit-unified-{}-{}"
CLAUDE_WINDOWS = (("session", "5h"), ("weekly", "7d"))
CODEX_WINDOWS = (("5h", "session"), ("weekly", "weekly"))
SECRET = re.compile(r"sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{16,}|\b(?:\d{1,3}\.){3}\d{1,3}\b")
BRANCH = re.compile(r"[0-9A-Za-z][0-9A-Za-z._/-]{0,199}")
SHA = re.compile(r"[0-9a-f]{40}")


def chicago_reset(epoch):
    """Render a reset instant in Jeff's time zone, so CST and CDT are never mixed up."""
    stamp = datetime.fromtimestamp(epoch, CHICAGO)
    meridiem = "a.m." if stamp.hour < 12 else "p.m."
    return stamp.strftime(f"%B {stamp.day}, %Y at {stamp.hour % 12 or 12}:%M:%S {meridiem} %Z")


def window(provider, name, used, reset, now):
    if type(used) not in (int, float) or not math.isfinite(used) or used < 0:
        raise ValueError(f"Unusable {provider} {name} utilization")
    if type(reset) is not int or not 0 < reset < 1 << 42:
        raise ValueError(f"Unusable {provider} {name} reset time")
    if reset <= now:
        raise ValueError(f"The {provider} {name} reading is stale")
    return {"provider": provider, "window": name, "used": min(float(used), 100.0),
            "reset": reset, "reset_text": chicago_reset(reset)}


def codex_windows(result, account, now):
    """Reuse the lane runner's own quota parser so both readers accept the same payload."""
    path = Path(__file__).resolve().parents[1] / "segment1/runner-quota.py"
    spec = importlib.util.spec_from_file_location("runner_quota", path)
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    limits = reader.quota_windows(result, account, now)
    return [window("codex", name, limits[key]["used"], limits[key]["reset"], now)
            for name, key in CODEX_WINDOWS]


def claude_windows(headers, now):
    """Read the unified subscription headers Claude Code receives on a claude.ai login."""
    if not isinstance(headers, dict):
        raise ValueError("Expected Claude response headers")
    lookup = {str(name).lower(): value for name, value in headers.items()}
    if len(lookup) != len(headers):
        raise ValueError("Claude response headers repeat a name")
    if lookup.get("anthropic-ratelimit-unified-status") not in ("allowed", "allowed_warning", "rejected"):
        raise ValueError("Unknown Claude rate-limit status")
    status = lookup["anthropic-ratelimit-unified-status"]
    rows = []
    for name, abbreviation in CLAUDE_WINDOWS:
        try:
            utilization = float(lookup[CLAUDE_HEADER.format(abbreviation, "utilization")])
            reset = int(lookup[CLAUDE_HEADER.format(abbreviation, "reset")])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Incomplete Claude {name} window") from None
        # A rejected response means the subscription is already out, whatever the fraction says.
        used = 100.0 if status == "rejected" else utilization * 100
        rows.append(window("claude", name, used, reset, now))
    return rows


def assess(windows, warn_percent=WARN_PERCENT, stop_percent=STOP_PERCENT):
    if not windows:
        raise ValueError("Refusing to report on no usage windows")
    if not 0 < warn_percent <= stop_percent <= 100:
        raise ValueError("Invalid warning and stop thresholds")
    expected = ({("codex", name) for name, _ in CODEX_WINDOWS} |
                {("claude", name) for name, _ in CLAUDE_WINDOWS})
    missing = expected - {(row["provider"], row["window"]) for row in windows}
    warned = sorted((row for row in windows if row["used"] >= warn_percent),
                    key=lambda row: -row["used"])
    return {"windows": sorted(windows, key=lambda row: (row["provider"], row["window"])),
            "warned": warned, "unread": sorted(missing),
            "warn_percent": warn_percent, "stop_percent": stop_percent,
            "stop": any(row["used"] >= stop_percent for row in warned)}


def report_text(report):
    lines = [f"{row['provider'].capitalize()} {row['window']} window at {row['used']:.6g}% used."
             f" It resets {row['reset_text']}." for row in report["windows"]]
    for provider, name in report["unread"]:
        lines.append(f"The {provider} {name} window was not read.")
    if report["stop"]:
        lines.append(f"At or above the {report['stop_percent']:.6g}% stop threshold. Stop after the handoff.")
    elif report["warned"]:
        lines.append(f"At or above the {report['warn_percent']:.6g}% warning threshold.")
    return "\n".join(lines)


def handoff_text(report, branch, head, step, next_step, failures):
    if not BRANCH.fullmatch(branch) or not SHA.fullmatch(head):
        raise ValueError("Expected the lane's branch name and full head commit")
    for field in (step, next_step, *failures):
        if not isinstance(field, str) or not field.strip() or "\0" in field:
            raise ValueError("Each handoff field must be nonempty text")
    body = "\n".join([
        "# First Officer handoff",
        "",
        report_text(report),
        "",
        "## Resume point",
        "",
        f"- Branch `{branch}` is at `{head}`.",
        f"- Step in progress: {step.strip()}",
        f"- Next: {next_step.strip()}",
        *(f"- Open failure: {failure.strip()}" for failure in failures),
        "- Nothing was pushed, merged or activated by this handoff.",
        "",
    ])
    if SECRET.search(body):
        raise ValueError("Refusing to write a handoff containing a credential or address")
    return body


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True,
                          text=True, timeout=30, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})


def commit_handoff(repo, report, branch, head, step, next_step, failures=()):
    """Write the handoff and commit only that file. A lane never pushes and never commits on main."""
    repo = Path(repo)
    observed = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if observed != branch:
        raise ValueError("The checkout is not on the branch named in the handoff")
    if observed in PROTECTED:
        raise ValueError("Refusing to commit a handoff on a protected branch")
    if not SHA.fullmatch(head) or git(repo, "merge-base", "--is-ancestor", head, "HEAD",
                                      check=False).returncode != 0:
        raise ValueError("The named head is not in this branch's history")
    document = handoff_text(report, branch, head, step, next_step, failures)
    target = repo / HANDOFF
    pending = target.with_name(HANDOFF + ".pending")
    pending.write_text(document, encoding="utf-8")
    os.replace(pending, target)
    if not git(repo, "status", "--porcelain", "--", HANDOFF).stdout.strip():
        return None
    git(repo, "add", "--", HANDOFF)
    git(repo, "commit", "--only", "-m", "Record First Officer usage warning and resume point", "--", HANDOFF)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def read_json(path):
    if path is None:
        return None
    text = Path(path).read_text(encoding="utf-8")
    if len(text) > 256 * 1024:
        raise ValueError("Usage reading is too large to trust")
    return json.loads(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, help="Codex account/rateLimits/read result as JSON")
    parser.add_argument("--codex-account", help="Account identifier the Codex reading must match")
    parser.add_argument("--claude", type=Path, help="Claude response headers as a JSON object")
    parser.add_argument("--warn-percent", type=float, default=WARN_PERCENT)
    parser.add_argument("--stop-percent", type=float, default=STOP_PERCENT)
    parser.add_argument("--repo", type=Path, help="Checkout that receives HANDOFF.md on a warning")
    parser.add_argument("--step", default="unrecorded")
    parser.add_argument("--next", dest="next_step", default="unrecorded")
    parser.add_argument("--failure", action="append", default=[])
    args = parser.parse_args(argv)
    now = time.time()
    try:
        windows = []
        if args.codex is not None:
            windows += codex_windows(read_json(args.codex), args.codex_account, now)
        if args.claude is not None:
            windows += claude_windows(read_json(args.claude), now)
        report = assess(windows, args.warn_percent, args.stop_percent)
        print(report_text(report))
        if not report["warned"]:
            return OK
        if args.repo is not None:
            branch = git(args.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            head = git(args.repo, "rev-parse", "HEAD").stdout.strip()
            commit = commit_handoff(args.repo, report, branch, head, args.step, args.next_step,
                                    args.failure)
            print(f"Handoff committed as {commit}." if commit else "Handoff already current.")
        return STOPPED if report["stop"] else WARNED
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Usage tracking failed: {SECRET.sub('[redacted]', str(error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
