#!/usr/bin/env python3
"""Track Codex, Claude and Cursor subscription usage and hand off at the lane limit."""

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
CURSOR_WINDOW = ("cursor", "billing-period")
CURSOR_MAX_AGE = 90
CURSOR_PERIOD_FIELDS = {"observed_at", "period_start", "period_end", "included_spend", "included_limit"}
CURSOR_BILLING_FLAGS = {"has_credits", "unlimited", "on_demand_enabled", "usage_billing_enabled",
                        "auto_top_up_enabled"}


def _enforced_secret():
    path = Path(__file__).resolve().parents[1] / "privacy" / "scan.py"
    spec = importlib.util.spec_from_file_location("fo_enforced_secret_usage", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.enforced_secret


SECRET = _enforced_secret()
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


def cursor_windows(reading, now):
    """Normalize a current, subscription-only Cursor billing period."""
    if not isinstance(reading, dict) or reading.keys() != CURSOR_PERIOD_FIELDS | {"billing"}:
        raise ValueError("Incomplete or unknown Cursor plan reading")
    billing = reading["billing"]
    if (not isinstance(billing, dict) or billing.keys() != CURSOR_BILLING_FLAGS
            or any(value is not False for value in billing.values())):
        raise ValueError("Cursor paid extras are not confirmed disabled")

    start, end, observed = (reading[key] for key in ("period_start", "period_end", "observed_at"))
    if (any(type(epoch) is not int for epoch in (start, end, observed))
            or not 0 < start <= observed <= now < end or now - observed > CURSOR_MAX_AGE):
        raise ValueError("Cursor billing-period reading is stale or invalid")

    spend, limit = reading["included_spend"], reading["included_limit"]
    if type(spend) is not int or type(limit) is not int or spend < 0 or limit <= 0:
        raise ValueError("Unusable Cursor included spend or limit")
    used = 100 * min(spend, limit) / limit
    return [window(*CURSOR_WINDOW, used, end, now)]


def assess(windows, warn_percent=WARN_PERCENT, stop_percent=STOP_PERCENT):
    if not windows:
        raise ValueError("Refusing to report on no usage windows")
    if (isinstance(warn_percent, bool) or isinstance(stop_percent, bool)
            or type(warn_percent) not in (int, float) or type(stop_percent) not in (int, float)
            or not math.isfinite(warn_percent) or not math.isfinite(stop_percent)
            or not 0 < warn_percent <= stop_percent
            or warn_percent > WARN_PERCENT or stop_percent > STOP_PERCENT):
        raise ValueError("Usage overrides may only be stricter than the default thresholds")
    expected = ({("codex", name) for name, _ in CODEX_WINDOWS} |
                {("claude", name) for name, _ in CLAUDE_WINDOWS} | {CURSOR_WINDOW})
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


def read_json(path, *, object_pairs_hook=None):
    if path is None:
        return None
    with Path(path).open(encoding="utf-8") as stream:
        text = stream.read(256 * 1024 + 1)
    if len(text) > 256 * 1024:
        raise ValueError("Usage reading is too large to trust")
    return json.loads(text, object_pairs_hook=object_pairs_hook)


def unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("Cursor plan reading repeats a field")
    return fields


def read_cursor_windows(path, now):
    try:
        if path is None or not path.is_file():
            raise ValueError("Missing Cursor plan reading")
        reading = read_json(path, object_pairs_hook=unique_fields)
    except (OSError, ValueError, RecursionError):
        raise ValueError("Provide a fresh Cursor plan reading with unique fields") from None
    return cursor_windows(reading, now)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, help="Codex account/rateLimits/read result as JSON")
    parser.add_argument("--codex-account", help="Account identifier the Codex reading must match")
    parser.add_argument("--claude", type=Path, help="Claude response headers as a JSON object")
    parser.add_argument("--cursor", type=Path, help="Fresh, normalized Cursor plan and billing JSON")
    parser.add_argument("--warn-percent", type=float, default=WARN_PERCENT)
    parser.add_argument("--stop-percent", type=float, default=STOP_PERCENT)
    parser.add_argument("--repo", type=Path, help="Checkout that receives HANDOFF.md on a warning")
    parser.add_argument("--step", default="unrecorded")
    parser.add_argument("--next", dest="next_step", default="unrecorded")
    parser.add_argument("--failure", action="append", default=[])
    args = parser.parse_args(argv)
    now = time.time()
    try:
        windows = read_cursor_windows(args.cursor, now)
    except (ValueError, OverflowError) as error:
        print(f"The cursor billing-period window was not read. {error}.", file=sys.stderr)
        return 1
    try:
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
