#!/usr/bin/env python3
"""Prove one grounded memory save, its approval, the audit chain, and a routine dry run.

A fact is not verified until confirm presents the digest of that exact text and nonce
and the approval is still inside its ten-minute window. The audit line is written
before the fact. A second confirm, a different digest, or an expired approval appends
a refusal and does not save again. The lane-review routine has a fixed 07:00
America/Chicago schedule. This module can run it only as a dry run: it writes a
receipt and an audit line, and it does not send a message, call a model, or change a
repository. No timer is installed.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time


GENESIS = "0" * 64
TTL = 600
ZONE = ZoneInfo("America/Chicago")
ROUTINE = "lane-review"
ROUTINE_HOUR = 7
SECRET = re.compile(r"sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|\b(?:\d{1,3}\.){3}\d{1,3}\b")
TEXT = re.compile(r"[\x20-\x7e]{1,200}")
NONCE = re.compile(r"mem-[0-9a-f]{16}")
DIGEST = re.compile(r"[0-9a-f]{64}")


def canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _digest(payload):
    return hashlib.sha256(canonical(payload).encode()).hexdigest()


def _clock(now):
    if type(now) is not int or not 0 <= now < 1 << 42:
        raise ValueError("The acceptance clock is unusable")
    return now


def _root(path):
    root = Path(path)
    if root.is_symlink():
        raise ValueError("The acceptance root must be a private directory")
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    info = root.lstat()
    if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("The acceptance root must be a private directory")
    return root


def _check_private(info):
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("The acceptance file must be a private owner file")
    if info.st_size > 1024 * 1024:
        raise ValueError("The acceptance file is too large")


@contextmanager
def _locked(path):
    try:
        handle = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("The acceptance file must be a private owner file") from None
        raise
    file = os.fdopen(handle, "r+", encoding="utf-8")
    try:
        info = os.fstat(file.fileno())
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_size == 0:
            os.fchmod(file.fileno(), 0o600)
            info = os.fstat(file.fileno())
        _check_private(info)
        fcntl.flock(file, fcntl.LOCK_EX)
        yield file
    finally:
        file.close()


def _rows(file):
    file.seek(0)
    rows = []
    previous = GENESIS
    raw_lines = file.read().splitlines()
    for seq, raw in enumerate(raw_lines):
        digest, separator, payload = raw.partition("\t")
        if not separator or _digest_text(payload) != digest:
            raise ValueError("The audit line was altered")
        body = json.loads(payload)
        if canonical(body) != payload or body.get("prev") != previous or body.get("seq") != seq:
            raise ValueError("The audit chain was altered")
        keys = {"action", "at", "digest", "kind", "nonce", "prev", "seq"}
        if body.get("kind") == "refused":
            keys.add("reason")
        elif body.get("action") == "routine-dry-run":
            keys.add("next_run")
        if set(body) != keys or body.get("kind") not in {"accepted", "refused"}:
            raise ValueError("The audit line was altered")
        if body.get("action") not in {"save-memory", "routine-dry-run"} or type(body.get("at")) is not int:
            raise ValueError("The audit line was altered")
        if body["kind"] == "refused" and body.get("reason") not in {"credential", "unusable", "not-pending", "digest", "expired"}:
            raise ValueError("The audit line was altered")
        if not DIGEST.fullmatch(str(body.get("digest"))):
            raise ValueError("The audit line was altered")
        if "next_run" in body and type(body["next_run"]) is not int:
            raise ValueError("The audit line was altered")
        previous = digest
        rows.append((digest, body))
    return rows, previous


def _digest_text(payload):
    return hashlib.sha256(payload.encode()).hexdigest()


def _append(file, previous, seq, body):
    full = {**body, "prev": previous, "seq": seq}
    payload = canonical(full)
    line = _digest_text(payload) + "\t" + payload + "\n"
    file.seek(0, os.SEEK_END)
    file.write(line)
    file.flush()
    os.fsync(file.fileno())
    return _digest_text(payload)


def _read_json(path, expected):
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError("The acceptance file must be a private owner file")
    handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(handle, "r", encoding="utf-8") as file:
        _check_private(os.fstat(file.fileno()))
        document = json.loads(file.read())
    if not isinstance(document, dict) or set(document) != expected:
        raise ValueError("The acceptance file is unreadable")
    return document


def _write_json(path, document):
    temporary = path.with_name(f".{path.name}.tmp")
    payload = canonical(document) + "\n"
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _approval(nonce, text, expires_at):
    return {"action": "save-memory", "expires_at": expires_at, "nonce": nonce, "text": text}


def _nonce(text):
    return "mem-" + hashlib.sha256(text.encode()).hexdigest()[:16]


def _pending_binding(current):
    if type(current.get("expires_at")) is not int or not TEXT.fullmatch(str(current.get("text", ""))):
        raise ValueError("The pending approval was altered")
    if current.get("nonce") != _nonce(current["text"]) or not DIGEST.fullmatch(str(current.get("digest", ""))):
        raise ValueError("The pending approval was altered")
    if current["digest"] != _digest(_approval(current["nonce"], current["text"], current["expires_at"])):
        raise ValueError("The pending approval was altered")


def propose(root, text, now):
    """Record one fact as waiting. It is not verified, and a credential is refused."""
    moment = _clock(now)
    root = _root(root)
    if type(text) is not str or SECRET.search(text) or not TEXT.fullmatch(text) or text != text.strip():
        reason = "credential" if type(text) is str and SECRET.search(text) else "unusable"
        _audit_refusal(root, "save-memory", _nonce(text) if type(text) is str else "mem-" + "0" * 16,
                       reason, moment)
        raise ValueError("A memory fact must be one plain sentence")
    nonce = _nonce(text)
    expires_at = moment + TTL
    card = _approval(nonce, text, expires_at)
    card_digest = _digest(card)
    pending_path = root / "pending.json"
    with _locked(root / "audit.log") as audit:
        rows, _previous = _rows(audit)
        if any(body["nonce"] == nonce and body["kind"] == "accepted" for _digest, body in rows):
            raise ValueError("That memory fact is already verified")
        current = _read_json(pending_path, {"digest", "expires_at", "nonce", "text"})
        if current:
            _pending_binding(current)
            if current["expires_at"] >= moment and current["nonce"] != nonce:
                raise ValueError("A memory fact is already waiting for approval")
            if current["nonce"] == nonce and current["expires_at"] >= moment:
                return {"digest": current["digest"], "expires_at": current["expires_at"], "nonce": nonce, "text": text}
            if current["expires_at"] < moment:
                _append(audit, _rows(audit)[1], len(rows), {
                    "action": "save-memory", "at": moment, "digest": current["digest"],
                    "kind": "refused", "nonce": current["nonce"], "reason": "expired",
                })
                rows, _previous = _rows(audit)
        _write_json(pending_path, {"digest": card_digest, "expires_at": expires_at, "nonce": nonce, "text": text})
        return {"digest": card_digest, "expires_at": expires_at, "nonce": nonce, "text": text}


def _audit_refusal(root, action, nonce, reason, moment):
    root = _root(root)
    with _locked(root / "audit.log") as audit:
        rows, previous = _rows(audit)
        _append(audit, previous, len(rows), {
            "action": action, "at": moment, "digest": GENESIS, "kind": "refused", "nonce": nonce, "reason": reason,
        })


def recall(root):
    """Return verified facts only. A pending card is not a saved fact."""
    root = _root(root)
    memory = _read_json(root / "memory.json", {"facts"})
    facts = [] if memory is None else memory["facts"]
    if not isinstance(facts, list) or not facts:
        return "No verified saved fact."
    lines = []
    for fact in facts:
        if set(fact) != {"digest", "expires_at", "nonce", "saved_at", "text"} or not TEXT.fullmatch(fact["text"]):
            raise ValueError("The memory file is unreadable")
        lines.append(f"Verified fact: {fact['text']} Approval {fact['digest'][:12]}.")
    return "\n".join(lines)


def confirm(root, nonce, presented, now):
    """Save the pending fact only when the presented digest is that fact's approval."""
    moment = _clock(now)
    root = _root(root)
    if not NONCE.fullmatch(str(nonce)) or not DIGEST.fullmatch(str(presented)):
        raise ValueError("The approval is unusable")
    pending_path = root / "pending.json"
    memory_path = root / "memory.json"
    with _locked(root / "audit.log") as audit:
        rows, previous = _rows(audit)
        accepted = [body for _digest, body in rows if body["nonce"] == nonce and body["kind"] == "accepted"]
        pending = _read_json(pending_path, {"digest", "expires_at", "nonce", "text"})
        if accepted or pending is None or pending["nonce"] != nonce:
            _append(audit, previous, len(rows), {
                "action": "save-memory", "at": moment, "digest": presented, "kind": "refused",
                "nonce": nonce, "reason": "not-pending",
            })
            return {"saved": False, "reason": "not-pending"}
        try:
            _pending_binding(pending)
        except ValueError:
            _append(audit, previous, len(rows), {
                "action": "save-memory", "at": moment, "digest": presented, "kind": "refused",
                "nonce": nonce, "reason": "digest",
            })
            return {"saved": False, "reason": "digest"}
        recomputed = _digest(_approval(pending["nonce"], pending["text"], pending["expires_at"]))
        if presented != pending["digest"] or presented != recomputed:
            _append(audit, previous, len(rows), {
                "action": "save-memory", "at": moment, "digest": presented, "kind": "refused",
                "nonce": nonce, "reason": "digest",
            })
            return {"saved": False, "reason": "digest"}
        if moment > pending["expires_at"]:
            _append(audit, previous, len(rows), {
                "action": "save-memory", "at": moment, "digest": presented, "kind": "refused",
                "nonce": nonce, "reason": "expired",
            })
            pending_path.unlink()
            return {"saved": False, "reason": "expired"}
        memory = _read_json(memory_path, {"facts"}) or {"facts": []}
        if any(fact.get("nonce") == nonce for fact in memory["facts"]):
            raise ValueError("The memory file already has this fact")
        fact = {
            "digest": presented, "expires_at": pending["expires_at"], "nonce": nonce,
            "saved_at": moment, "text": pending["text"],
        }
        memory["facts"].append(fact)
        _append(audit, previous, len(rows), {
            "action": "save-memory", "at": moment, "digest": presented, "kind": "accepted", "nonce": nonce,
        })
        _write_json(memory_path, memory)
        pending_path.unlink()
        return {"saved": True, "text": fact["text"]}


def next_run(now):
    """Return the next 07:00 America/Chicago strictly after now."""
    moment = _clock(now)
    current = datetime.fromtimestamp(moment, ZONE)
    candidate = current.replace(hour=ROUTINE_HOUR, minute=0, second=0, microsecond=0)
    if candidate <= current:
        candidate += timedelta(days=1)
    return int(candidate.timestamp())


def run_routine(root, now, dry_run=True):
    """Record the scheduled lane review. The live action is not available here."""
    if dry_run is not True:
        raise ValueError("The scheduled routine can only be proven as a dry run")
    moment = _clock(now)
    root = _root(root)
    following = next_run(moment)
    payload = {
        "action": "routine-dry-run", "dry_run": True, "name": ROUTINE,
        "next_run": following, "ran_at": moment,
    }
    receipt = {
        "called_model": False, "changed_repo": False, "dry_run": True, "next_run": following,
        "ran_at": moment, "routine": ROUTINE, "sent": False,
    }
    with _locked(root / "audit.log") as audit:
        rows, previous = _rows(audit)
        _append(audit, previous, len(rows), {
            "action": "routine-dry-run", "at": moment, "digest": _digest(payload),
            "kind": "accepted", "next_run": following, "nonce": f"routine-{moment}",
        })
        _write_json(root / "receipt-lane-review.json", receipt)
    return receipt


def verify(root):
    """Accept a clean chain. A changed, missing, or extra line is a failure."""
    root = _root(root)
    with _locked(root / "audit.log") as audit:
        rows, _previous = _rows(audit)
    seen = set()
    memory = _read_json(root / "memory.json", {"facts"}) or {"facts": []}
    if not isinstance(memory.get("facts"), list):
        raise ValueError("The memory file is unreadable")
    facts = {}
    for fact in memory["facts"]:
        if not isinstance(fact, dict) or fact.get("nonce") in facts:
            raise ValueError("The memory file repeats a fact")
        facts[fact["nonce"]] = fact
    routine_lines = []
    for _line, body in rows:
        if body["kind"] == "accepted" and body["action"] == "save-memory":
            if body["nonce"] in seen:
                raise ValueError("The audit accepted the same fact twice")
            seen.add(body["nonce"])
            fact = facts.get(body["nonce"])
            if fact is None:
                raise ValueError("A verified fact is missing from the audit binding")
            expected = _digest(_approval(fact["nonce"], fact["text"], fact["expires_at"]))
            if fact.get("digest") != body["digest"] or fact["digest"] != expected:
                raise ValueError("A verified fact is missing from the audit binding")
        if body["kind"] == "accepted" and body["action"] == "routine-dry-run":
            expected = _digest({
                "action": "routine-dry-run", "dry_run": True, "name": ROUTINE,
                "next_run": body["next_run"], "ran_at": body["at"],
            })
            if body["digest"] != expected:
                raise ValueError("The routine receipt does not match the audit")
            routine_lines.append(body)
    if routine_lines:
        last = routine_lines[-1]
        receipt = _read_json(root / "receipt-lane-review.json", {
            "called_model", "changed_repo", "dry_run", "next_run", "ran_at", "routine", "sent",
        })
        if receipt is None or receipt["ran_at"] != last["at"] or receipt["next_run"] != last["next_run"]:
            raise ValueError("The routine receipt does not match the audit")
        if receipt["sent"] is not False or receipt["dry_run"] is not True or receipt["routine"] != ROUTINE:
            raise ValueError("The routine receipt does not match the audit")
        if receipt["called_model"] is not False or receipt["changed_repo"] is not False:
            raise ValueError("The routine receipt does not match the audit")
    for nonce in facts:
        if nonce not in seen:
            raise ValueError("A saved fact has no accepted audit line")
    return len(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    propose_command = commands.add_parser("propose")
    propose_command.add_argument("text")
    commands.add_parser("recall")
    confirm_command = commands.add_parser("confirm")
    confirm_command.add_argument("nonce")
    confirm_command.add_argument("digest")
    commands.add_parser("routine")
    commands.add_parser("verify")
    args = parser.parse_args(argv)
    moment = int(time.time())
    try:
        if args.command == "propose":
            card = propose(args.root, args.text, moment)
            print(f"Pending save {card['nonce']} digest {card['digest']} expires {card['expires_at']}.")
            print(recall(args.root))
        elif args.command == "recall":
            print(recall(args.root))
        elif args.command == "confirm":
            result = confirm(args.root, args.nonce, args.digest, moment)
            if result["saved"]:
                print(f"Saved verified fact {args.nonce}.")
            else:
                print(f"Refused save {args.nonce}: {result['reason']}.")
            print(recall(args.root))
        elif args.command == "routine":
            receipt = run_routine(args.root, moment)
            print(f"Dry run {receipt['routine']} at {receipt['ran_at']} next {receipt['next_run']}. Sent no message.")
        else:
            count = verify(args.root)
            print(f"Audit chain verified: {count} lines.")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        message = str(error) if type(error) is ValueError else "The acceptance record could not be written"
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
