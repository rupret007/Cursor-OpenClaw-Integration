#!/usr/bin/env python3
"""Run the whole First Officer release as one reviewed operation with one rollback."""

import argparse
import importlib.util
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile

from stage import ANDREA_SUMMARY, SUPPORTED_BASES

STORE = Path("/opt/openclaw-fo")
SERVICE = "openclaw-gateway.service"
OWNER = "rupret007"
RELEASE = "{release}"
DIGEST = re.compile(r"[0-9a-f]{64}")
VERIFIER_PREFLIGHT = """\
import os
import stat
import sys

path = sys.argv[1]
try:
    info = os.lstat(path)
except OSError:
    sys.exit(1)
safe = stat.S_ISREG(info.st_mode) and (info.st_uid, info.st_gid) == (0, 0) and not info.st_mode & 0o022
sys.exit(0 if safe and os.access(path, os.R_OK) else 1)
"""
ANDREA_PREFLIGHT = """\
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
import { sshAndreaSummary } from './first-officer/segment1/andrea-ssh.mjs';
if (await sshAndreaSummary(AbortSignal.timeout(15000))) process.exit(0);
const { createAndreaReader } = await import(pathToFileURL(join(process.argv[2], 'round6/runtime/andrea.mjs')));
await createAndreaReader().chats();
"""


def step(what, argv, env=None, cwd=None, stdin=None, store=None, expect=None):
    """One reviewed command. `store` names the substitution its output fills, `expect` pins it."""
    return {"what": what, "argv": [str(part) for part in argv], "env": env or {},
            "cwd": None if cwd is None else str(cwd), "stdin": stdin, "store": store,
            "expect": expect, "capture": store is not None or expect is not None}


def build_plan(repo, bundle, staging, test_home, reviewed_head):
    """Stage the candidate on the Nuc, gate it, and pack it. Nothing here touches the gateway."""
    candidate = staging / f"first-officer-{RELEASE}"
    archive = staging / f"first-officer-{RELEASE}.tar"
    return [
        step("Stage the candidate from the verified prior bundle",
             [sys.executable, "-B", repo / "first-officer/stage.py", bundle, staging,
              "--reviewed-head", reviewed_head],
             store="release", expect=DIGEST),
        step("Run the packaged Round 13 canary against the candidate",
             ["nice", "-n", "10", "node", repo / "tests/first_officer/round13-canary.mjs"],
             env={"HOME": str(test_home), "FO_CANARY_HOME": str(test_home),
                  "FO_RELEASE_ROOT": str(candidate)}, cwd=repo),
        step("Run the Segment 1 and captain gate",
             ["nice", "-n", "10", "node", "--test", repo / "tests/first_officer/segment1.test.mjs",
              repo / "tests/first_officer/captain.test.mjs"], cwd=repo),
        step("Scan the staged candidate for secrets and personal data",
             [sys.executable, "-B", repo / "first-officer/privacy/scan.py", "--root", candidate, "--files-only"]),
        step("Refuse to replace an existing candidate archive",
             ["test", "!", "-e", archive]),
        step("Pack the candidate for the operator",
             ["nice", "-n", "10", "tar", "-C", staging, "-cf", archive, f"first-officer-{RELEASE}"]),
    ]


def _acceptance_cycle():
    path = Path(__file__).resolve().parent / "acceptance" / "cycle.py"
    spec = importlib.util.spec_from_file_location("fo_acceptance_cycle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def audit_anchor_commands(payload):
    """Sudo commands that create or update the root-owned seal. Dry-run prints them and runs none."""
    cycle = _acceptance_cycle()
    directory = str(cycle.PRODUCTION_ANCHOR_DIR)
    target = str(cycle.PRODUCTION_ANCHOR)
    return [
        ["sudo", "install", "-d", "-o", "root", "-g", "root", "-m", "0755", directory],
        ["sudo", "install", "-o", "root", "-g", "root", "-m", "0444", str(payload), target],
        ["sudo", "chown", "root:root", directory, target],
        ["sudo", "chmod", "0755", directory],
        ["sudo", "chmod", "0444", target],
    ]


def require_acceptance_root():
    """Return the acceptance directory. An unset or unexpanded value is refused."""
    acceptance = os.environ.get("FO_ACCEPTANCE_ROOT", "").strip()
    path = Path(acceptance) if acceptance else Path()
    if not acceptance or acceptance.startswith("$") or path.name.startswith("$"):
        raise ValueError("Export FO_ACCEPTANCE_ROOT to the acceptance directory before activate.")
    return acceptance


def activate_plan(repo, release, archive, extract, backup, baseline=ANDREA_SUMMARY):
    """Preflight, publish and switch on bobserver. Jeff runs this once, after approving the digest."""
    candidate = extract / f"first-officer-{release}"
    published = STORE / f"first-officer-{release}"
    staged = "{publish}"
    acceptance = require_acceptance_root()
    payload = Path(backup) / "audit-anchor"
    anchor = [
        step("Export the audit anchor payload",
             [sys.executable, "-B", repo / "first-officer/acceptance/cycle.py",
              "--root", acceptance, "export-anchor", payload]),
    ]
    anchor_actions = (
        "Create the root-owned audit anchor directory",
        "Install the audit anchor for the service to read",
        "Give the audit anchor to root",
        "Keep the service from replacing the audit anchor directory",
        "Keep the audit anchor readable and not writable by the service",
    )
    for what, argv in zip(anchor_actions, audit_anchor_commands(payload)):
        anchor.append(step(what, argv))
    anchor.append(step(
        "Verify the audit chain after installing the seal",
        [sys.executable, "-B", repo / "first-officer/acceptance/cycle.py",
         "--root", acceptance, "verify"],
    ))
    return [
        step("Confirm the installed release verifier is a readable root-owned file",
             [sys.executable, "-I", "-B", "-c", VERIFIER_PREFLIGHT, STORE / "verify-install.py"]),
        step("Confirm the gateway is running before any change",
             ["systemctl", "--user", "is-active", "--quiet", SERVICE]),
        step("Confirm the GitHub CLI authenticates as the configured owner",
             ["gh", "api", "user", "--hostname", "github.com", "--jq", ".login"], expect=OWNER),
        step("Confirm the existing Andrea read-only bridge answers",
             ["node", "--input-type=module", "-", STORE / f"first-officer-{baseline}"],
             stdin=ANDREA_PREFLIGHT, cwd=repo),
        step("Unpack the reviewed archive",
             ["tar", "-xf", archive, "-C", extract, "--no-same-owner"]),
        step("Compare the manifest digest with the approved release",
             [sys.executable, "-B", "-c",
              "import hashlib, pathlib, sys\n"
              "digest = hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest()\n"
              "sys.exit(0 if digest == sys.argv[2] else 1)",
              candidate / "SHA256SUMS", release]),
        step("Check the candidate inventory against the reviewed digest",
             ["sha256sum", "--quiet", "--strict", "--check", "SHA256SUMS"], cwd=candidate),
        step("Prepare the switch and record the private rollback",
             [sys.executable, "-B", repo / "first-officer/install.py", "prepare", backup,
              "--release", release, "--from-release", baseline, "--candidate", candidate], cwd=repo),
        *anchor,
        step("Refuse to republish an existing release directory",
             ["sudo", "test", "!", "-e", published]),
        step("Reserve a private staging directory under the release store",
             ["sudo", "mktemp", "-d", STORE / ".segment1-publish.XXXXXXXX"],
             store="publish", expect=re.compile(re.escape(str(STORE)) + r"/\.segment1-publish\.[A-Za-z0-9]{8}")),
        step("Copy the candidate into the staging directory",
             ["sudo", "cp", "-a", "--no-preserve=ownership", f"{candidate}/.", f"{staged}/"]),
        step("Place the candidate at its store path",
             ["sudo", "mv", "-nT", staged, published]),
        step("Confirm the move installed this copy",
             ["sudo", "test", "!", "-e", staged]),
        step("Set the installed read-only permissions for verification",
             ["sudo", "chmod", "-R", "a-w,a+rX", published]),
        step("Verify the candidate with the installed verifier before the gateway switch",
             [sys.executable, "-B", repo / "first-officer/install.py", "verify-candidate", backup],
             cwd=repo),
        step("Switch the gateway to the reviewed release",
             [sys.executable, "-B", repo / "first-officer/install.py", "activate", backup], cwd=repo),
        step("Confirm the gateway is active after the switch",
             ["systemctl", "--user", "is-active", "--quiet", SERVICE]),
    ]


def rollback_plan(repo, backup):
    return [
        step("Restore the exact prior config and pin",
             [sys.executable, "-B", repo / "first-officer/install.py", "rollback", backup], cwd=repo),
        step("Confirm the gateway is active after the rollback",
             ["systemctl", "--user", "is-active", "--quiet", SERVICE]),
    ]


def rollback_backup_ready(backup):
    """True when prepare has written a private rollback directory the installer can use."""
    backup = Path(backup)
    names = (
        "plan.json",
        "original-openclaw.json",
        "candidate-openclaw.json",
        "original-pin.conf",
        "candidate-pin.conf",
    )
    try:
        info = backup.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        return False
    for name in names:
        path = backup / name
        try:
            member = path.lstat()
        except OSError:
            return False
        if (not stat.S_ISREG(member.st_mode) or member.st_uid != os.getuid() or member.st_nlink != 1
                or stat.S_IMODE(member.st_mode) & 0o077 or member.st_size == 0):
            return False
    return True


def describe(plan):
    lines = []
    for index, entry in enumerate(plan, 1):
        prefix = " ".join(f"{name}={shlex.quote(value)}" for name, value in sorted(entry["env"].items()))
        command = " ".join(shlex.quote(part) for part in entry["argv"])
        lines.append(f"{index}. {entry['what']}")
        lines.append(f"   {prefix + ' ' if prefix else ''}{command}")
        if entry["stdin"] is not None:
            lines.append("   (reads the packaged Andrea preflight on standard input)")
    return "\n".join(lines)


def execute(entry, substitutions):
    def fill(text):
        for name, value in substitutions.items():
            text = text.replace("{" + name + "}", value)
        return text

    argv = [fill(part) for part in entry["argv"]]
    env = {**os.environ, **{name: fill(value) for name, value in entry["env"].items()}}
    result = subprocess.run(argv, env=env, cwd=fill(entry["cwd"]) if entry["cwd"] else None,
                            input=entry["stdin"], text=True, timeout=1800,
                            stdout=subprocess.PIPE if entry["capture"] else None)
    if result.returncode:
        raise ValueError(f"Release step failed: {entry['what']}")
    return (result.stdout or "").strip()


def run(plan, substitutions, runner=execute):
    for entry in plan:
        print(f"-> {entry['what']}")
        output = runner(entry, substitutions)
        expected = entry["expect"]
        if expected is not None and not (expected.fullmatch(output) if hasattr(expected, "fullmatch")
                                         else expected == output):
            raise ValueError(f"Release step reported an unusable result: {entry['what']}")
        if entry["store"] is not None:
            substitutions[entry["store"]] = output


def build(repo, bundle, staging, runner, dry_run, reviewed_head):
    with tempfile.TemporaryDirectory(prefix="fo-canary-home-") as home:
        plan = build_plan(repo, bundle, staging, Path(home), reviewed_head)
        if dry_run:
            print(describe(plan))
            return 0
        substitutions = {}
        run(plan, substitutions, runner)
    release = substitutions["release"]
    print(f"Release {release}\nPrivate bundle {staging / f'first-officer-{release}.tar'}")
    print("Give Jeff the digest and the archive through a private channel. Nothing was activated.")
    return 0


def activate(repo, release, archive, backup_parent, runner, dry_run, baseline=ANDREA_SUMMARY):
    if not DIGEST.fullmatch(release) or release in SUPPORTED_BASES:
        raise ValueError("Expected a reviewed candidate digest distinct from the supported releases")
    if baseline not in SUPPORTED_BASES:
        raise ValueError("Expected a supported prior release")
    require_acceptance_root()
    backup = Path(backup_parent) / "switch"
    with tempfile.TemporaryDirectory(prefix="fo-release-") as extract:
        plan = activate_plan(repo, release, Path(archive), Path(extract), backup, baseline)
        if dry_run:
            print(describe(plan))
            print("\nIf the final service check fails after the switch succeeds, roll back and exit 1.")
            print(describe(rollback_plan(repo, backup)))
            return 0

        def run_and_record_backup(entry, substitutions):
            output = runner(entry, substitutions)
            if (entry["what"] == "Prepare the switch and record the private rollback"
                    and rollback_backup_ready(backup)):
                print(f"Keep this private rollback directory: {backup}")
            return output

        switch_steps, postcheck = plan[:-1], plan[-1:]
        run(switch_steps, {}, run_and_record_backup)
        try:
            run(postcheck, {}, runner)
        except (OSError, ValueError, subprocess.SubprocessError):
            print("Post-activation gateway check failed.", file=sys.stderr)
            if not rollback_backup_ready(backup):
                print("The saved rollback is unavailable.", file=sys.stderr)
                return 1
            try:
                rollback(repo, backup, runner, dry_run=False)
            except (OSError, ValueError, subprocess.SubprocessError):
                print("Automatic rollback failed. Use the printed private rollback directory.", file=sys.stderr)
            return 1
    print(f"First Officer release {release} is active.")
    if rollback_backup_ready(backup):
        print(f"Roll back with: python3 -B first-officer/release.py rollback {backup}")
    return 0


def rollback(repo, backup, runner, dry_run):
    plan = rollback_plan(repo, Path(backup))
    if dry_run:
        print(describe(plan))
        return 0
    run(plan, {}, runner)
    print("The prior config and pin are restored and the gateway is active.")
    return 0


def main(argv=None, runner=execute):
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the exact ordered commands without running any of them")
    commands = parser.add_subparsers(dest="command", required=True)
    staged = commands.add_parser("build", help="Stage, gate and pack a candidate on the Nuc")
    staged.add_argument("prior_bundle", type=Path)
    staged.add_argument("staging_directory", type=Path)
    staged.add_argument("--reviewed-head", required=True, help="Full approved Git commit for the overlay")
    switch = commands.add_parser("activate", help="Publish and switch the gateway in one operation")
    switch.add_argument("release")
    switch.add_argument("archive", type=Path)
    switch.add_argument("backup_parent", type=Path)
    switch.add_argument("--from-release", default=ANDREA_SUMMARY, choices=SUPPORTED_BASES)
    restore = commands.add_parser("rollback", help="Restore the exact prior config and pin")
    restore.add_argument("backup", type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == "build":
            return build(repo, args.prior_bundle, args.staging_directory, runner, args.dry_run, args.reviewed_head)
        if args.command == "activate":
            return activate(repo, args.release, args.archive, args.backup_parent, runner,
                            args.dry_run, args.from_release)
        return rollback(repo, args.backup, runner, args.dry_run)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        message = str(error) if type(error) is ValueError else "The release operation failed"
        print(message, file=sys.stderr)
        if args.command == "activate" and not args.dry_run:
            backup = Path(args.backup_parent) / "switch"
            if rollback_backup_ready(backup):
                print(f"Keep this private rollback directory: {backup}", file=sys.stderr)
                print("If the gateway was switched, roll back with the printed private backup directory.",
                      file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
