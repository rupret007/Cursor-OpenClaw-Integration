#!/usr/bin/env python3
"""Turn a Playwright JSON report into a flaky report and a gate that quarantine cannot turn green.

Every test is kept, with every attempt. A clean first attempt outside quarantine is the only
passing result, and only when the report lists no top-level error and matches the expected
project and test inventory. A quarantine entry stays visible and counts as non-passing even
when the test itself passed. Expired, malformed and orphaned entries do the same. The command
exits 0 only when every expected test passed on its first attempt, the report has no fatal
error, and the quarantine list adds nothing.
"""

import argparse
import copy
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys


NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ISSUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,40}")
FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,159}")
SECRET = re.compile(r"sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|\b(?:\d{1,3}\.){3}\d{1,3}\b")
ATTEMPT = {"passed", "failed", "timedOut", "skipped", "interrupted"}
RANK = {"expected": 0, "skipped": 1, "flaky": 2, "unexpected": 3, "interrupted": 3, "unreadable": 4}
MAX_DAYS = 14


def redact(text):
    return SECRET.sub("[redacted]", str(text).replace("\n", " "))[:300]


def _walk(suite, inherited_file, out):
    file = suite.get("file") or inherited_file
    title = suite.get("title") or ""
    for spec in suite.get("specs") or []:
        spec_title = spec.get("title")
        if not isinstance(spec_title, str):
            raise ValueError("A Playwright spec has no title")
        titles = [part for part in (title, spec_title) if part]
        for test in spec.get("tests") or []:
            out.append((file, titles, test))
    for child in suite.get("suites") or []:
        child_title = child.get("title") or ""
        nested = dict(child)
        if title and child_title:
            nested["title"] = f"{title} > {child_title}"
        elif title:
            nested["title"] = title
        _walk(nested, file, out)


def _attempts(results):
    if not isinstance(results, list) or not results:
        return []
    attempts = []
    for result in results:
        if not isinstance(result, dict) or result.get("status") not in ATTEMPT:
            raise ValueError("A Playwright attempt has no usable status")
        errors = result.get("errors") or []
        message = ""
        if errors and isinstance(errors, list) and isinstance(errors[0], dict):
            message = redact(errors[0].get("message") or "")
        attempts.append({"retry": result.get("retry", 0), "status": result["status"],
                         "error": message, "trace": any(
                             isinstance(item, dict) and item.get("name") == "trace"
                             for item in result.get("attachments") or [])})
    return attempts


def _derived(attempts):
    if not attempts:
        return "unreadable"
    statuses = [item["status"] for item in attempts]
    last = statuses[-1]
    earlier_failed = any(status in ("failed", "timedOut", "interrupted") for status in statuses[:-1])
    if earlier_failed and last == "passed":
        return "flaky"
    if len(statuses) > 1 and last == "passed":
        return "flaky"
    if len(statuses) == 1 and last == "passed":
        return "expected"
    if len(statuses) == 1 and last == "skipped":
        return "skipped"
    if last == "interrupted":
        return "interrupted"
    if last in ("failed", "timedOut"):
        return "unexpected"
    return "unreadable"


def produce(report):
    """Keep every test and every attempt. A later pass does not erase an earlier failure."""
    if not isinstance(report, dict) or not isinstance(report.get("suites"), list):
        raise ValueError("A Playwright report needs its suites")
    found = []
    for suite in report["suites"]:
        if isinstance(suite, dict):
            _walk(suite, "", found)
    tests = []
    seen = set()
    for file, titles, test in found:
        if not isinstance(file, str) or not FILE.fullmatch(file) or ".." in file.split("/"):
            raise ValueError("A Playwright test has no usable file")
        project = test.get("projectName")
        if not isinstance(project, str) or not NAME.fullmatch(project):
            raise ValueError("A Playwright test has no usable project")
        if not titles or any(not isinstance(part, str) or not part.strip() or len(part) > 160 for part in titles):
            raise ValueError("A Playwright test has no usable title")
        attempts = _attempts(test.get("results"))
        reported = test.get("status") if test.get("status") in RANK else "unreadable"
        outcome = reported if RANK[reported] >= RANK[_derived(attempts)] else _derived(attempts)
        identity = f"{project}::{file}::{' > '.join(titles)}"
        if identity in seen:
            raise ValueError("A Playwright report repeats a test")
        seen.add(identity)
        tests.append({"id": identity, "project": project, "file": file, "title": titles,
                      "outcome": outcome, "attempts": attempts})
    if not tests:
        raise ValueError("A Playwright report collected no tests")
    raw_errors = report.get("errors") or []
    if not isinstance(raw_errors, list):
        raise ValueError("A Playwright report has an unreadable error")
    errors = []
    for item in raw_errors:
        if isinstance(item, str) and item.strip():
            errors.append(redact(item))
        elif isinstance(item, dict):
            errors.append(redact(item.get("message") or item.get("error") or "report error"))
        else:
            errors.append("report error")
    projects = sorted({test["project"] for test in tests})
    digest = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"bound": copy.deepcopy(tests), "errors": errors, "projects": projects,
            "source_digest": digest, "tests": tests}


def _day(value, label):
    if not isinstance(value, str):
        raise ValueError(f"A quarantine {label} is not a UTC date")
    try:
        found = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"A quarantine {label} is not a UTC date") from None
    if found.isoformat() != value:
        raise ValueError(f"A quarantine {label} is not a UTC date")
    return found


def load_quarantine(document):
    if not isinstance(document, dict) or set(document) != {"entries"} or not isinstance(document["entries"], list):
        raise ValueError("The quarantine list needs its entries")
    entries = []
    seen = set()
    for raw in document["entries"]:
        fields = {"project", "file", "title", "owner", "issue", "reason", "addedOn", "expiresOn"}
        if not isinstance(raw, dict) or set(raw) != fields:
            raise ValueError("A quarantine entry is incomplete")
        if (not NAME.fullmatch(str(raw["project"])) or not FILE.fullmatch(str(raw["file"])) or
                ".." in str(raw["file"]).split("/") or not isinstance(raw["title"], list) or
                not raw["title"] or any(not isinstance(part, str) or not part.strip() for part in raw["title"])):
            raise ValueError("A quarantine entry does not name a test")
        if not NAME.fullmatch(str(raw["owner"])) or not ISSUE.fullmatch(str(raw["issue"])):
            raise ValueError("A quarantine entry needs an owner and an issue")
        reason = raw["reason"]
        if not isinstance(reason, str) or not 8 <= len(reason) <= 200 or SECRET.search(reason) or any(
                ord(char) < 32 for char in reason):
            raise ValueError("A quarantine entry needs a plain reason")
        added, expires = _day(raw["addedOn"], "start"), _day(raw["expiresOn"], "expiry")
        if (expires - added).days < 0 or (expires - added).days > MAX_DAYS:
            raise ValueError("A quarantine entry lasts longer than 14 days")
        identity = f"{raw['project']}::{raw['file']}::{' > '.join(raw['title'])}"
        if identity in seen:
            raise ValueError("A quarantine entry is repeated")
        seen.add(identity)
        entries.append({**raw, "id": identity, "added": added, "expires": expires})
    return entries


def _clean(test):
    attempts = test["attempts"]
    return (test["outcome"] == "expected" and len(attempts) == 1 and attempts[0]["status"] == "passed" and
            attempts[0]["retry"] == 0)


def load_expected(document):
    """The gate is green only for this exact project and test inventory."""
    if not isinstance(document, dict) or set(document) != {"projects", "tests"}:
        raise ValueError("The gate needs the expected projects and tests")
    projects, tests = document["projects"], document["tests"]
    if (not isinstance(projects, list) or not projects or not isinstance(tests, list) or not tests or
            any(not isinstance(name, str) or not NAME.fullmatch(name) for name in projects) or
            any(not isinstance(item, str) or not item or len(item) > 400 for item in tests) or
            len(projects) != len(set(projects)) or len(tests) != len(set(tests))):
        raise ValueError("The gate needs the expected projects and tests")
    return {"projects": list(projects), "tests": list(tests)}


def evaluate(events, entries, today, expected):
    """Count quarantine, report errors, and a short inventory as non-passing."""
    if not isinstance(today, date):
        raise ValueError("The gate needs today's UTC date")
    expected = load_expected(expected)
    report_errors = events.get("errors")
    if not isinstance(report_errors, list) or any(not isinstance(item, str) for item in report_errors):
        raise ValueError("The flaky report has unreadable errors")
    by_id = {test["id"]: test for test in events["tests"]}
    active, expired, orphaned = [], [], []
    claimed = set()
    for entry in entries:
        test = by_id.get(entry["id"])
        if test is None:
            orphaned.append(entry["id"])
            continue
        claimed.add(entry["id"])
        row = {"id": entry["id"], "outcome": test["outcome"], "owner": entry["owner"],
               "issue": entry["issue"], "expiresOn": entry["expires"].isoformat()}
        (expired if today > entry["expires"] else active).append(row)
    passing, failures, flakes, skipped, interrupted = [], [], [], [], []
    quarantined = []
    active_ids = {row["id"] for row in active}
    expired_ids = {row["id"] for row in expired}
    for test in events["tests"]:
        if test["id"] in active_ids:
            quarantined.append({"id": test["id"], "outcome": test["outcome"],
                                "attempts": [item["status"] for item in test["attempts"]]})
            continue
        if test["id"] in expired_ids:
            continue
        if _clean(test):
            passing.append(test["id"])
        elif test["outcome"] == "flaky":
            flakes.append({"id": test["id"], "attempts": [item["status"] for item in test["attempts"]]})
        elif test["outcome"] == "skipped":
            skipped.append(test["id"])
        elif test["outcome"] == "interrupted":
            interrupted.append(test["id"])
        else:
            failures.append({"id": test["id"], "outcome": test["outcome"],
                             "attempts": [item["status"] for item in test["attempts"]]})
    actual_ids = [test["id"] for test in events["tests"]]
    actual_projects = {test["project"] for test in events["tests"]}
    inventory_mismatch = (
        sorted(actual_ids) != sorted(expected["tests"]) or len(actual_ids) != len(expected["tests"]) or
        sorted(actual_projects) != sorted(expected["projects"])
    )
    nonpassing = (len(failures) + len(flakes) + len(skipped) + len(interrupted) +
                  len(quarantined) + len(expired) + len(orphaned) + len(report_errors) +
                  (1 if inventory_mismatch else 0))
    ok = (nonpassing == 0 and len(passing) == len(events["tests"]) and bool(events["tests"]) and
          not report_errors and not inventory_mismatch)
    return {"ok": ok, "passing": len(passing), "nonpassing": nonpassing,
            "quarantined": quarantined, "failures": failures, "flakes": flakes,
            "skipped": skipped, "interrupted": interrupted, "expired": expired,
            "orphaned": orphaned, "errors": report_errors,
            "inventory_mismatch": inventory_mismatch}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, document):
    target = Path(path)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    produce_command = commands.add_parser("produce")
    produce_command.add_argument("report", type=Path)
    produce_command.add_argument("events", type=Path)
    evaluate_command = commands.add_parser("evaluate")
    evaluate_command.add_argument("report", type=Path)
    evaluate_command.add_argument("events", type=Path)
    evaluate_command.add_argument("quarantine", type=Path)
    evaluate_command.add_argument("inventory", type=Path)
    evaluate_command.add_argument("summary", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "produce":
            events = produce(_read(args.report))
            _write(args.events, events)
            print(f"Recorded {len(events['tests'])} Playwright tests, including every attempt.")
            return 0
        events = produce_events(_read(args.events), _read(args.report))
        result = evaluate(events, load_quarantine(_read(args.quarantine)),
                          datetime.now(timezone.utc).date(), load_expected(_read(args.inventory)))
        _write(args.summary, result)
        print(f"Passing {result['passing']}. Non-passing {result['nonpassing']}. "
              f"Quarantined {len(result['quarantined'])}.")
        return 0 if result["ok"] else 1
    except (OSError, ValueError, json.JSONDecodeError) as error:
        message = str(error) if type(error) is ValueError else "The flaky report could not be read"
        print(message, file=sys.stderr)
        return 2


def produce_events(document, report=None):
    """Accept only the producer output, so a hand-edited pass cannot erase an attempt or a report error.

    When the Playwright report is supplied, it is the source of truth. The events
    file's attempts and outcome are not.
    """
    fields = {"bound", "errors", "projects", "source_digest", "tests"}
    if not isinstance(document, dict) or set(document) != fields or not isinstance(document["tests"], list):
        raise ValueError("The flaky report needs its tests")
    digest = document.get("source_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("The flaky report is not bound to its source")
    if report is not None:
        derived = produce(report)
        if derived["source_digest"] != digest:
            raise ValueError("The flaky report does not match its source")
        return derived
    if not isinstance(document["bound"], list):
        raise ValueError("The flaky report is not bound to its source")
    document = {**document, "tests": copy.deepcopy(document["bound"])}
    if not isinstance(document["errors"], list) or any(not isinstance(item, str) for item in document["errors"]):
        raise ValueError("The flaky report has unreadable errors")
    if not isinstance(document["projects"], list):
        raise ValueError("The flaky report needs its projects")
    checked = []
    for test in document["tests"]:
        fields = {"attempts", "file", "id", "outcome", "project", "title"}
        if not isinstance(test, dict) or set(test) != fields or test["outcome"] not in RANK:
            raise ValueError("The flaky report has an unreadable test")
        identity = f"{test['project']}::{test['file']}::{' > '.join(test['title'])}"
        attempts = test["attempts"]
        if identity != test["id"] or not isinstance(attempts, list) or not attempts:
            raise ValueError("The flaky report has an unreadable test")
        if any(not isinstance(item, dict) or item.get("status") not in ATTEMPT or type(item.get("retry")) is not int
               for item in attempts):
            raise ValueError("The flaky report has an unreadable test")
        derived = _derived(attempts)
        outcome = test["outcome"] if RANK[test["outcome"]] >= RANK[derived] else derived
        checked.append({**test, "outcome": outcome})
    if not checked:
        raise ValueError("The flaky report collected no tests")
    projects = sorted({test["project"] for test in checked})
    if sorted(str(name) for name in document["projects"]) != projects:
        raise ValueError("The flaky report needs its projects")
    return {"errors": list(document["errors"]), "projects": projects, "tests": checked}


if __name__ == "__main__":
    raise SystemExit(main())
