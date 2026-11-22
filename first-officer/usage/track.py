#!/usr/bin/env python3
"""Track Codex, Claude and Cursor subscription usage and hand off at the lane limit."""

import argparse
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo


CHICAGO = ZoneInfo("America/Chicago")
WARN_PERCENT = 90.0
STOP_PERCENT = 97.0
OK, WARNED, STOPPED = 0, 3, 4
HANDOFF = "HANDOFF.md"
HANDOFF_COMMIT = "Record First Officer usage warning and resume point"
PROTECTED = ("main", "master")
CLAUDE_HEADER = "anthropic-ratelimit-unified-{}-{}"
CLAUDE_WINDOWS = (("session", "5h"), ("weekly", "7d"))
CODEX_WINDOWS = (("5h", "session"), ("weekly", "weekly"))
CURSOR_WINDOW = ("cursor", "billing-period")
CURSOR_MAX_AGE = 90
CURSOR_ACCOUNT = re.compile(r"[0-9a-f]{64}")
READING_MAX_BYTES = 256 * 1024
CURSOR_PERIOD_FIELDS = {"observed_at", "period_start", "period_end", "included_spend", "included_limit",
                        "account_binding"}
CURSOR_BILLING_FLAGS = {"has_credits", "unlimited", "on_demand_enabled", "usage_billing_enabled",
                        "auto_top_up_enabled"}


def _privacy_rules():
    path = Path(__file__).resolve().parents[1] / "privacy" / "scan.py"
    spec = importlib.util.spec_from_file_location("fo_enforced_secret_usage", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PRIVACY = _privacy_rules()
SECRET = PRIVACY.enforced_secret
BRANCH = re.compile(r"[0-9A-Za-z][0-9A-Za-z._/-]{0,199}")
SHA = re.compile(r"[0-9a-f]{40}")


def chicago_reset(epoch):
    """Render a reset instant in Jeff's time zone, so CST and CDT are never mixed up."""
    stamp = datetime.fromtimestamp(epoch, CHICAGO)
    meridiem = "a.m." if stamp.hour < 12 else "p.m."
    return stamp.strftime(f"%B {stamp.day}, %Y at {stamp.hour % 12 or 12}:%M:%S {meridiem} %Z")


def window(provider, name, used, reset, now):
    if type(used) not in (int, float, Fraction) or not math.isfinite(used) or used < 0:
        raise ValueError(f"Unusable {provider} {name} utilization")
    if type(reset) is not int or not 0 < reset < 1 << 42:
        raise ValueError(f"Unusable {provider} {name} reset time")
    if reset <= now:
        raise ValueError(f"The {provider} {name} reading is stale")
    try:
        reset_text = chicago_reset(reset)
    except (ValueError, OverflowError, OSError):
        raise ValueError(f"Unusable {provider} {name} reset time") from None
    utilization = min(used, 100) if isinstance(used, Fraction) else min(float(used), 100.0)
    return {"provider": provider, "window": name, "used": utilization, "reset": reset, "reset_text": reset_text}


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


def cursor_windows(reading, now, account):
    """Normalize a current, subscription-only Cursor billing period."""
    if not isinstance(reading, dict) or reading.keys() != CURSOR_PERIOD_FIELDS | {"billing"}:
        raise ValueError("Incomplete or unknown Cursor plan reading")
    if not isinstance(account, str) or not CURSOR_ACCOUNT.fullmatch(account):
        raise ValueError("Cursor plan account binding is missing or invalid")
    if reading["account_binding"] != account:
        raise ValueError("Cursor plan account binding does not match")
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
    used = Fraction(100 * min(spend, limit), limit)
    return [window(*CURSOR_WINDOW, used, end, now)]


def reaches_threshold(used, threshold):
    return used >= (Fraction(str(threshold)) if isinstance(used, Fraction) else threshold)


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
    warned = sorted((row for row in windows if reaches_threshold(row["used"], warn_percent)),
                    key=lambda row: -row["used"])
    return {"windows": sorted(windows, key=lambda row: (row["provider"], row["window"])),
            "warned": warned, "unread": sorted(missing),
            "warn_percent": warn_percent, "stop_percent": stop_percent,
            "stop": any(reaches_threshold(row["used"], stop_percent) for row in warned)}


def format_percent(used, *thresholds):
    if not isinstance(used, Fraction):
        return f"{used:.6g}"
    # Round down at a precision that can represent both configured thresholds.
    places = max(6, *(-Decimal(str(threshold)).as_tuple().exponent for threshold in thresholds))
    scale = 10 ** places
    whole, remainder = divmod(used.numerator * scale // used.denominator, scale)
    return f"{whole}.{remainder:0{places}d}".rstrip("0").rstrip(".")


def report_text(report):
    thresholds = report["warn_percent"], report["stop_percent"]
    lines = [f"{row['provider'].capitalize()} {row['window']} window at {format_percent(row['used'], *thresholds)}% used."
             f" It resets {row['reset_text']}." for row in report["windows"]]
    for provider, name in report["unread"]:
        lines.append(f"The {provider} {name} window was not read.")
    if report["stop"]:
        stop_percent = str(report["stop_percent"]).removesuffix(".0")
        lines.append(f"At or above the {stop_percent}% stop threshold. Stop after the handoff.")
    elif report["warned"]:
        warn_percent = str(report["warn_percent"]).removesuffix(".0")
        lines.append(f"At or above the {warn_percent}% warning threshold.")
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
    if SECRET.search(body) or PRIVACY.findings_in_text(body):
        raise ValueError("Refusing to write a handoff containing a credential or address")
    return body


def git(repo, *args, check=True, index_file=None, data=None):
    env = {key: value for key, value in os.environ.items() if key != "GIT_INDEX_FILE"}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True,
                          encoding="utf-8", input=data, timeout=30, env=env)


def handoff_anchor(repo, branch, head):
    """Reuse the resume point from the previous generated handoff on the same branch."""
    commit = git(repo, "show", "--format=%s", "--name-only", head, "--").stdout.splitlines()
    if [line for line in commit if line] != [HANDOFF_COMMIT, HANDOFF]:
        return head
    prior = git(repo, "show", f"{head}:{HANDOFF}").stdout
    prefix = f"- Branch `{branch}` is at `"
    candidates = [line[len(prefix):-2] for line in prior.splitlines()
                  if line.startswith(prefix) and line.endswith("`.")]
    if len(candidates) == 1 and SHA.fullmatch(candidates[0]) and git(
            repo, "merge-base", "--is-ancestor", candidates[0], head, check=False).returncode == 0:
        return candidates[0]
    return head


def checked_out(repo, reference, commit):
    return (git(repo, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip() == reference
            and git(repo, "rev-parse", "--verify", "HEAD", check=False).stdout.strip() == commit)


def publish_handoff(repo, reference, parent, commit, blob, document, scratch):
    """Update the named lane and synchronize only HANDOFF.md in its checked-out index."""
    index = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip())
    lock = index.with_name(index.name + ".lock")
    try:
        stream = lock.open("xb")
    except FileExistsError:
        raise ValueError("The checkout index is busy. Handoff was deferred.") from None

    published = synchronized = False
    try:
        with stream:
            if not checked_out(repo, reference, parent):
                raise ValueError("The checkout changed before the handoff could be recorded")
            checkout_index = scratch / "checkout-index"
            if index.exists():
                shutil.copyfile(index, checkout_index)
            else:
                git(repo, "read-tree", parent, index_file=checkout_index)
            git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{HANDOFF}",
                index_file=checkout_index)
            stream.write(checkout_index.read_bytes())
            stream.flush()
            if not checked_out(repo, reference, parent):
                raise ValueError("The checkout changed before the handoff could be recorded")
            if git(repo, "-c", "core.hooksPath=/dev/null", "update-ref", "--no-deref",
                   reference, commit, parent, check=False).returncode:
                raise ValueError("The lane advanced or rejected the handoff update")
            published = True
            if not checked_out(repo, reference, commit):
                raise ValueError("Handoff committed to its lane. The checkout changed before synchronization.")
            pending = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=repo, prefix=".fo-handoff-", delete=False,
            )
            try:
                with pending:
                    pending.write(document)
                if not checked_out(repo, reference, commit):
                    raise ValueError("Handoff committed to its lane. The checkout changed before synchronization.")
                os.replace(pending.name, repo / HANDOFF)
            finally:
                Path(pending.name).unlink(missing_ok=True)
        os.replace(lock, index)
        synchronized = True
    except (OSError, subprocess.SubprocessError):
        if published:
            raise ValueError("Handoff committed to its lane. Checkout synchronization failed.") from None
        raise
    finally:
        if not stream.closed:
            stream.close()
        if not synchronized:
            lock.unlink(missing_ok=True)


def commit_handoff(repo, report, branch, head, step, next_step, failures=(), *, expected_parent=None):
    """Write a handoff-only commit on the explicitly selected lane without invoking commit hooks."""
    if not BRANCH.fullmatch(branch):
        raise ValueError("Expected the lane's branch name and full head commit")
    if branch in PROTECTED:
        raise ValueError("Refusing to commit a handoff on a protected branch")
    repo = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip())
    reference = f"refs/heads/{branch}"
    if git(repo, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip() != reference:
        raise ValueError("The checkout is not on the branch named in the handoff")
    parent = git(repo, "rev-parse", "--verify", reference).stdout.strip()
    if not SHA.fullmatch(parent) or (expected_parent is not None and parent != expected_parent):
        raise ValueError("The lane advanced before the handoff could be recorded")
    if not SHA.fullmatch(head) or git(repo, "merge-base", "--is-ancestor", head, parent,
                                      check=False).returncode != 0:
        raise ValueError("The named head is not in this branch's history")
    document = handoff_text(report, branch, head, step, next_step, failures)
    with tempfile.TemporaryDirectory(prefix="fo-handoff-") as temporary:
        scratch = Path(temporary)
        parent_index = scratch / "parent-index"
        git(repo, "read-tree", parent, index_file=parent_index)
        blob = git(repo, "hash-object", "-w", "--stdin", data=document).stdout.strip()
        git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{HANDOFF}",
            index_file=parent_index)
        tree = git(repo, "write-tree", index_file=parent_index).stdout.strip()
        if tree == git(repo, "rev-parse", f"{parent}^{{tree}}").stdout.strip():
            if not checked_out(repo, reference, parent):
                raise ValueError("The checkout changed before the handoff could be recorded")
            return None
        commit = git(repo, "commit-tree", tree, "-p", parent, "-m", HANDOFF_COMMIT).stdout.strip()
        publish_handoff(repo, reference, parent, commit, blob, document, scratch)
        return commit


def read_json(path):
    if path is None:
        return None
    with Path(path).open(encoding="utf-8") as stream:
        text = stream.read(READING_MAX_BYTES + 1)
    if len(text) > READING_MAX_BYTES:
        raise ValueError("Usage reading is too large to trust")
    return json.loads(text)


def unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("Cursor plan reading repeats a field")
    return fields


def read_cursor_windows(path, now, account):
    try:
        if path is None:
            raise ValueError("Missing Cursor plan reading")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or info.st_nlink != 1 or info.st_size > READING_MAX_BYTES):
                raise ValueError("Invalid Cursor plan file")
            contents = stream.read(READING_MAX_BYTES + 1)
            current = os.fstat(stream.fileno())
            if len(contents) != info.st_size or current.st_ctime_ns != info.st_ctime_ns:
                raise ValueError("Cursor plan file changed while reading")
        reading = json.loads(contents.decode("utf-8"), object_pairs_hook=unique_fields)
    except (OSError, ValueError, RecursionError):
        raise ValueError("Provide a fresh private Cursor plan reading with unique fields") from None
    return cursor_windows(reading, now, account)


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
        windows = read_cursor_windows(args.cursor, now, os.environ.get("CURSOR_PLAN_ACCOUNT"))
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
            anchor = handoff_anchor(args.repo, branch, head)
            commit = commit_handoff(args.repo, report, branch, anchor, args.step, args.next_step,
                                    args.failure, expected_parent=head)
            print(f"Handoff committed as {commit}." if commit else "Handoff already current.")
        return STOPPED if report["stop"] else WARNED
    except subprocess.SubprocessError:
        print("Usage tracking failed: Could not verify or record the handoff in Git.", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        detail = SECRET.sub("[redacted]", str(error))
        if PRIVACY.findings_in_text(detail):
            detail = "Error detail withheld to protect private data"
        print(f"Usage tracking failed: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
