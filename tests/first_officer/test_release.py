import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "first-officer"))
spec = importlib.util.spec_from_file_location("fo_release", ROOT / "first-officer/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

ACTIVE = "61ff4565dd5638a886effefbd720c92a17b7388acbc6eeaffb7779f78d3f1b36"
CANDIDATE = "a" * 64
REVIEWED = "b" * 40
PUBLISH = "/opt/openclaw-fo/.segment1-publish.Ab3dEf7h"


@pytest.fixture(autouse=True)
def _exported_acceptance_root(tmp_path, monkeypatch):
    """Activation plans need a real acceptance directory, never a literal dollar path."""
    root = tmp_path / "acceptance"
    monkeypatch.setenv("FO_ACCEPTANCE_ROOT", str(root))
    return root


class Recorder:
    """Stands in for the shell so a whole release can be driven without touching a gateway."""

    def __init__(self, replies=None, failure=None):
        self.replies = replies or {}
        self.failure = failure
        self.calls = []

    def __call__(self, entry, substitutions):
        filled = [part.format(**substitutions) if "{" in part else part for part in entry["argv"]]
        self.calls.append((entry["what"], filled))
        if entry["what"] == self.failure:
            raise ValueError(f"Release step failed: {entry['what']}")
        return self.replies.get(entry["what"], "")


def activate_replies():
    return {"Confirm the GitHub CLI authenticates as the configured owner": release.OWNER,
            "Reserve a private staging directory under the release store": PUBLISH}


def write_private_rollback(backup):
    """The files install.py prepare leaves before a rollback path may be printed."""
    backup.mkdir(mode=0o700)
    backup.chmod(0o700)
    for name in (
        "plan.json",
        "original-openclaw.json",
        "candidate-openclaw.json",
        "original-pin.conf",
        "candidate-pin.conf",
    ):
        path = backup / name
        path.write_text('{"ready": true}\n', encoding="utf-8")
        path.chmod(0o600)


def test_dry_run_prints_the_ordered_plan_and_runs_nothing(tmp_path, capsys):
    recorder = Recorder()
    assert release.main(["--dry-run", "build", str(tmp_path / "prior"), str(tmp_path),
                         "--reviewed-head", REVIEWED], recorder) == 0
    printed = capsys.readouterr().out
    assert recorder.calls == []
    assert printed.splitlines()[0] == "1. Stage the candidate from the verified prior bundle"
    assert "first-officer/stage.py" in printed and "{release}" in printed

    assert release.main(["--dry-run", "activate", CANDIDATE, str(tmp_path / "a.tar"),
                         str(tmp_path)], recorder) == 0
    printed = capsys.readouterr().out
    assert recorder.calls == []
    assert "sudo mv -nT '{publish}' /opt/openclaw-fo/first-officer-" + CANDIDATE in printed
    assert "(reads the packaged Andrea preflight on standard input)" in printed
    assert "/opt/openclaw-fo/verify-install.py" in printed
    assert "If the final service check fails after the switch succeeds, roll back and exit 1." in printed
    assert "Confirm the gateway is active after the rollback" in printed


def test_one_build_command_stages_gates_and_packs_in_order(tmp_path, capsys):
    staging = tmp_path / "staging"
    recorder = Recorder({"Stage the candidate from the verified prior bundle": CANDIDATE})
    assert release.main(["build", str(tmp_path / "prior"), str(staging), "--reviewed-head", REVIEWED], recorder) == 0

    assert [what for what, _ in recorder.calls] == [
        "Stage the candidate from the verified prior bundle",
        "Run the packaged Round 13 canary against the candidate",
        "Run the Segment 1 and captain gate",
        "Scan the staged candidate for secrets and personal data",
        "Refuse to replace an existing candidate archive",
        "Pack the candidate for the operator",
    ]
    assert recorder.calls[0][1][-2:] == ["--reviewed-head", REVIEWED]
    assert recorder.calls[-3][1][-3:] == ["--root", str(staging / f"first-officer-{CANDIDATE}"), "--files-only"]
    assert recorder.calls[-1][1][-1] == f"first-officer-{CANDIDATE}"
    assert str(staging / f"first-officer-{CANDIDATE}.tar") in recorder.calls[-1][1]
    printed = capsys.readouterr().out
    assert f"Release {CANDIDATE}" in printed
    assert "Nothing was activated." in printed


def test_build_refuses_a_staging_result_that_is_not_a_release_digest(tmp_path, capsys):
    recorder = Recorder({"Stage the candidate from the verified prior bundle": "not-a-digest"})
    assert release.main(["build", str(tmp_path / "prior"), str(tmp_path), "--reviewed-head", REVIEWED], recorder) == 1
    assert "unusable result" in capsys.readouterr().err
    assert [what for what, _ in recorder.calls] == ["Stage the candidate from the verified prior bundle"]


@pytest.mark.parametrize("variant", ["clean", "dirty", "wrong-head", "sensitive-overlay"])
def test_release_build_only_packs_a_clean_reviewed_overlay(tmp_path, monkeypatch, variant):
    stage = importlib.import_module("stage")
    repo = tmp_path / "checkout"
    overlay = repo / "first-officer/segment1"
    overlay.mkdir(parents=True)
    privacy = repo / "first-officer/privacy"
    privacy.mkdir()
    shutil.copyfile(ROOT / "first-officer/privacy/scan.py", privacy / "scan.py")
    injected = "// sk-" + "reviewfixtureunsafe\n"
    for module in stage.MODULES:
        (overlay / module).write_text("// reviewed overlay\n", encoding="utf-8")
    changed = overlay / stage.MODULES[0]
    if variant == "sensitive-overlay":
        changed.write_text(injected, encoding="utf-8")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-q", "-b", "demo")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=lane@example.invalid", "commit", "-qm", "Review overlay")
    head = git("rev-parse", "HEAD")
    if variant == "dirty":
        changed.write_text(injected, encoding="utf-8")
    elif variant == "wrong-head":
        head = "b" * 40

    baseline = tmp_path / "prior"
    (baseline / stage.RUNTIME).mkdir(parents=True)
    prior_source = baseline / "first-officer-conductor.mjs"
    prior_source.write_text(
        "import { ownerFallbacks } from './round6/runtime/model-failover.mjs';\n"
        "const selection = await routeOwnerModel(event, api.config, routeFor, ownerFallbacks);\n",
        encoding="utf-8",
    )
    manifest = f"{stage.digest(prior_source.read_bytes())}  {prior_source.name}\n".encode()
    (baseline / stage.INVENTORY).write_bytes(manifest)
    monkeypatch.setattr(stage, "SUPPORTED_BASES", (stage.digest(manifest),))
    monkeypatch.setattr(stage, "OVERLAY", overlay)
    staging = tmp_path / "staging"
    staging.mkdir()

    def execute(entry, substitutions):
        argv = entry["argv"]
        if any(part.endswith("/stage.py") for part in argv):
            return stage.stage_release(baseline, staging, reviewed_head=argv[-1])
        if "node" in argv:
            return ""
        return release.execute(entry, substitutions)

    if variant == "clean":
        assert release.build(repo, baseline, staging, execute, False, head) == 0
        assert len(list(staging.glob("*.tar"))) == 1
    else:
        failure = {"dirty": "must be clean", "wrong-head": "does not match the reviewed head",
                   "sensitive-overlay": "Scan the staged candidate"}[variant]
        with pytest.raises(ValueError, match=failure):
            release.build(repo, baseline, staging, execute, False, head)
        assert list(staging.glob("*.tar")) == []


CANARY_PATH = "round6/runtime/addons/index.mjs"
DASHBOARD_SKILL = "round6/runtime/addons/skills/security-ship-gate/SKILL.md"
INSTINCT_SKILL = "round6/runtime/addons/skills/instinct-with-bob-and-andrea/SKILL.md"
DASHBOARD_PLACEHOLDER = "the dashboard verify allowlist address Jeff already set"
INSTINCT_PLACEHOLDER = "the Instinct legal opt-out address in their terms"


def _candidate_scanner():
    spec = importlib.util.spec_from_file_location(
        "fo_stage_candidate_scan", ROOT / "first-officer/privacy/scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _packaged_prior_files(canary_token, dashboard_email, instinct_emails, extra=""):
    first, second = instinct_emails
    return {
        CANARY_PATH: (
            "const logProbe = 'FO_ADDONS_LOG_CANARY ' + 'Authorization: Bearer "
            + canary_token + "';\n" + extra
        ),
        DASHBOARD_SKILL: (
            "Allowlists tight - dashboard verify email only " + dashboard_email
            + " unless Jeff widens.\n"
        ),
        INSTINCT_SKILL: (
            "Arbitration opt-out by emailing " + first + " (or " + second
            + " per some ToS copies) within 30 days.\n"
            "Arbitration opt-out uses " + first + " before the window closes.\n"
        ),
    }


def _stage_synthetic_prior(tmp_path, monkeypatch, files, name):
    """Stage through the real release function. Only the pin allowlist is extended."""
    stage = importlib.import_module("stage")
    root = tmp_path / name
    baseline = root / "prior"
    baseline.mkdir(parents=True)
    (baseline / "first-officer-conductor.mjs").write_text(
        "import { ownerFallbacks } from './round6/runtime/model-failover.mjs';\n"
        "const selection = await routeOwnerModel(event, api.config, routeFor, ownerFallbacks);\n",
        encoding="utf-8",
    )
    for filename, text in files.items():
        path = baseline / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    names = sorted(path.relative_to(baseline).as_posix()
                   for path in baseline.rglob("*") if path.is_file())
    manifest = "".join(
        f"{stage.digest((baseline / filename).read_bytes())}  {filename}\n" for filename in names
    ).encode()
    (baseline / stage.INVENTORY).write_bytes(manifest)
    pin = stage.digest(manifest)
    monkeypatch.setattr(stage, "SUPPORTED_BASES", (*stage.SUPPORTED_BASES, pin))
    staging = root / "staging"
    staging.mkdir()
    release_id = stage.stage_release(baseline, staging)
    candidate = staging / f"first-officer-{release_id}"
    stage.verify_inventory(candidate, release_id)
    return baseline, candidate


def _files_only(scan, root, capsys):
    code = scan.main(["--root", str(root), "--files-only"])
    captured = capsys.readouterr()
    report = captured.err if code else captured.out
    return code, report, captured


def test_stage_rewrites_packaged_canary_and_skill_emails_before_the_scan(tmp_path, monkeypatch, capsys):
    """stage_release rewrites the packaged files, then the real files-only scan is clean."""
    scan = _candidate_scanner()
    canary_token = "synthetic-canary-token" + "-value"
    dashboard_email = "operator" + "@" + "private.test"
    instinct_a = "legal" + "@" + "vendor.test"
    instinct_b = "optout" + "@" + "vendor.test"
    assert scan.bearer_token_is_credential(canary_token)
    assert canary_token not in scan.BEARER_FIXTURES
    assert scan.BEARER_FIXTURE == "ANDREA_SYNC_INTERNAL_TOKEN"
    assert scan.personal_email(dashboard_email)
    assert scan.personal_email(instinct_a)
    assert scan.personal_email(instinct_b)
    files = _packaged_prior_files(canary_token, dashboard_email, (instinct_a, instinct_b))
    baseline, candidate = _stage_synthetic_prior(tmp_path, monkeypatch, files, "clean")

    before, before_report, _before_captured = _files_only(scan, baseline, capsys)
    assert before == 1
    assert set(before_report.splitlines()[1:]) == {
        "credential round6/runtime/addons/index.mjs:1",
        "email round6/runtime/addons/skills/security-ship-gate/SKILL.md:1",
        "email round6/runtime/addons/skills/instinct-with-bob-and-andrea/SKILL.md:1",
        "email round6/runtime/addons/skills/instinct-with-bob-and-andrea/SKILL.md:2",
    }
    for secret in (canary_token, dashboard_email, instinct_a, instinct_b):
        assert secret not in before_report
    for name, text in files.items():
        assert (baseline / name).read_text(encoding="utf-8") == text

    code, report, captured = _files_only(scan, candidate, capsys)
    assert code == 0 and captured.err == ""
    assert report.startswith("Secret scan clean.")
    packaged = "\n".join((candidate / name).read_text(encoding="utf-8") for name in files)
    for secret in (canary_token, dashboard_email, instinct_a, instinct_b):
        assert secret not in report and secret not in packaged
    canary = (candidate / CANARY_PATH).read_text(encoding="utf-8")
    assert "FO_ADDONS_LOG_CANARY" in canary
    matches = list(scan.BEARER.finditer(canary))
    assert len(matches) == 1 and matches[0].group(1) == scan.BEARER_FIXTURE
    assert scan.findings_in_text(canary) == []
    dashboard = (candidate / DASHBOARD_SKILL).read_text(encoding="utf-8")
    instinct = (candidate / INSTINCT_SKILL).read_text(encoding="utf-8")
    assert dashboard.count(DASHBOARD_PLACEHOLDER) == 1 and "@" not in dashboard
    assert instinct.count(INSTINCT_PLACEHOLDER) == 3 and "@" not in instinct
    assert scan.findings_in_text(dashboard) == [] and scan.findings_in_text(instinct) == []


def test_stage_leaves_a_different_bearer_and_email_for_the_files_only_scan(tmp_path, monkeypatch, capsys):
    """A bearer outside the canary line and an email outside the two skills still fail the scan."""
    scan = _candidate_scanner()
    canary_token = "synthetic-canary-token" + "-value"
    other_token = "different-bearer-token" + "-value"
    dashboard_email = "operator" + "@" + "private.test"
    instinct_a = "legal" + "@" + "vendor.test"
    instinct_b = "optout" + "@" + "vendor.test"
    remaining_email = "remain" + "@" + "private.test"
    assert scan.bearer_token_is_credential(other_token)
    assert other_token not in scan.BEARER_FIXTURES
    assert scan.personal_email(remaining_email)
    extra = "const other = 'Authorization: Bearer " + other_token + "';\n"
    files = _packaged_prior_files(canary_token, dashboard_email, (instinct_a, instinct_b), extra)
    files["round6/runtime/addons/other-note.md"] = "Reach " + remaining_email + " for the other note.\n"
    baseline, candidate = _stage_synthetic_prior(tmp_path, monkeypatch, files, "strict")

    before, before_report, _before_captured = _files_only(scan, baseline, capsys)
    assert before == 1
    assert {
        "credential round6/runtime/addons/index.mjs:1",
        "credential round6/runtime/addons/index.mjs:2",
        "email round6/runtime/addons/skills/security-ship-gate/SKILL.md:1",
        "email round6/runtime/addons/skills/instinct-with-bob-and-andrea/SKILL.md:1",
        "email round6/runtime/addons/skills/instinct-with-bob-and-andrea/SKILL.md:2",
        "email round6/runtime/addons/other-note.md:1",
    } == set(before_report.splitlines()[1:])

    code, report, captured = _files_only(scan, candidate, capsys)
    assert code == 1 and captured.out == ""
    assert set(report.splitlines()[1:]) == {
        "credential round6/runtime/addons/index.mjs:2",
        "email round6/runtime/addons/other-note.md:1",
    }
    for secret in (canary_token, other_token, dashboard_email, instinct_a, instinct_b, remaining_email):
        assert secret not in report
    canary = (candidate / CANARY_PATH).read_text(encoding="utf-8")
    canary_line, other_line = canary.splitlines()
    assert scan.BEARER.search(canary_line).group(1) == scan.BEARER_FIXTURE
    assert scan.BEARER.search(other_line).group(1) == other_token
    assert other_token in canary and canary_token not in canary
    assert remaining_email in (candidate / "round6/runtime/addons/other-note.md").read_text(encoding="utf-8")
    assert "@" not in (candidate / DASHBOARD_SKILL).read_text(encoding="utf-8")
    assert "@" not in (candidate / INSTINCT_SKILL).read_text(encoding="utf-8")
    for name, text in files.items():
        assert (baseline / name).read_text(encoding="utf-8") == text


def test_activate_checks_the_digest_and_writes_rollback_before_publish(tmp_path):
    recorder = Recorder(activate_replies())
    assert release.main(["activate", CANDIDATE, str(tmp_path / "a.tar"), str(tmp_path)], recorder) == 0
    order = [what for what, _ in recorder.calls]
    assert "Compare the manifest digest with the approved release" in order
    publish_at = order.index("Place the candidate at its store path")
    assert order.index("Compare the manifest digest with the approved release") < publish_at
    assert order.index("Check the candidate inventory against the reviewed digest") < publish_at
    assert order.index("Prepare the switch and record the private rollback") < publish_at
    assert publish_at < order.index("Verify the candidate with the installed verifier before the gateway switch") < order.index(
        "Switch the gateway to the reviewed release")
    prepare = dict(recorder.calls)["Prepare the switch and record the private rollback"]
    assert "--candidate" in prepare
    assert CANDIDATE in " ".join(prepare)
    digest_step = dict(recorder.calls)["Compare the manifest digest with the approved release"]
    assert digest_step[-1] == CANDIDATE
    assert any(part.endswith("SHA256SUMS") for part in digest_step)

    recorder = Recorder({"Stage the candidate from the verified prior bundle": CANDIDATE})
    assert release.main(["build", str(tmp_path / "prior"), str(tmp_path / "staging"),
                         "--reviewed-head", REVIEWED], recorder) == 0
    built = [what for what, _ in recorder.calls]
    pack_at = built.index("Pack the candidate for the operator")
    assert built[pack_at - 1] == "Refuse to replace an existing candidate archive"
    assert recorder.calls[pack_at - 1][1][:3] == ["test", "!", "-e"]


def _paste(argv):
    parts = []
    for part in argv:
        if part.startswith("$"):
            parts.append(f'"{part}"')
        else:
            parts.append(part)
    return " ".join(parts)


def test_activate_dry_run_prints_the_root_audit_anchor_commands(tmp_path, capsys):
    """Dry-run prints the sudo seal commands and runs none of them."""
    existed = Path("/var/lib/openclaw-fo").exists()
    recorder = Recorder()
    assert release.main(["--dry-run", "activate", CANDIDATE, str(tmp_path / "a.tar"), str(tmp_path)], recorder) == 0
    assert recorder.calls == []
    printed = capsys.readouterr().out
    payload = str(tmp_path / "switch" / "audit-anchor")
    lines = [" ".join(shlex.quote(part) for part in argv) for argv in release.audit_anchor_commands(payload)]
    cursor = printed.index("Export the audit anchor payload")
    assert "export-anchor" in printed[cursor:]
    for line in lines:
        found = printed.index(line, cursor)
        cursor = found + len(line)
    assert cursor < printed.index("verify-candidate")
    paste = [_paste(argv) for argv in release.audit_anchor_commands("$anchor_payload")]
    for name in ("JEFF-ACTIONS.md", "INSTALL.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        doc_at = 0
        for argv, line in zip(release.audit_anchor_commands("$anchor_payload"), paste):
            found = text.index(line, doc_at)
            doc_at = found + len(line)
            assert shlex.split(line) == argv
    assert Path("/var/lib/openclaw-fo").exists() == existed
    acceptance = os.environ["FO_ACCEPTANCE_ROOT"]
    assert f"--root {shlex.quote(acceptance)} export-anchor" in printed
    assert f"--root {shlex.quote(acceptance)} verify" in printed
    assert "'$FO_ACCEPTANCE_ROOT'" not in printed
    assert printed.index("export-anchor") < printed.index(f"--root {shlex.quote(acceptance)} verify")
    guide = (ROOT / "JEFF-ACTIONS.md").read_text(encoding="utf-8")
    install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
    assert "Audit anchor payload written." in guide
    assert "Audit anchor payload written." in install
    assert "/var/lib/openclaw-fo/audit-anchor" in guide
    assert all(value in guide for value in ("root:root", "0755", "0444"))
    section = guide.split("## 2.")[0]
    assert 'export FO_ACCEPTANCE_ROOT="$root"' in section
    assert "OPENCLAW_LITELLM_API_KEY" in section
    assert section.index('export FO_ACCEPTANCE_ROOT="$root"') < section.index("release.py activate")
    prepare_at = install.index('first-officer/install.py prepare "$backup"')
    assert install.index('export FO_ACCEPTANCE_ROOT="$root"') < prepare_at
    seal_at = install.index("sudo chmod 0444 /var/lib/openclaw-fo/audit-anchor", prepare_at)
    verify_at = install.index('cycle.py --root "$FO_ACCEPTANCE_ROOT" verify', seal_at)
    assert seal_at < verify_at < install.index('published="/opt/openclaw-fo/first-officer-$release"', seal_at)


def test_installed_verifier_fails_closed_when_the_audit_anchor_is_untrusted(tmp_path, monkeypatch):
    installer = importlib.import_module("install")
    monkeypatch.setenv("FO_ACCEPTANCE_ROOT", str(tmp_path / "cycle"))
    monkeypatch.setenv("FO_PRODUCTION_AUDIT_ANCHOR", str(tmp_path / "missing-anchor"))
    calls = []

    def boom(*args, **kwargs):
        calls.append(args)
        raise AssertionError("the installed verifier ran")

    monkeypatch.setattr(installer.subprocess, "run", boom)
    with pytest.raises(ValueError, match="missing"):
        installer.verify_config(tmp_path / "openclaw.json", CANDIDATE)
    anchor = tmp_path / "user-anchor"
    anchor.write_text("{}\n", encoding="utf-8")
    anchor.chmod(0o666)
    monkeypatch.setenv("FO_PRODUCTION_AUDIT_ANCHOR", str(anchor))
    with pytest.raises(ValueError, match="not owned by root|not mode-restricted|match|replaced"):
        installer.verify_config(tmp_path / "openclaw.json", CANDIDATE, candidate=tmp_path)
    assert calls == []


def test_install_doc_verifies_the_candidate_after_it_is_read_only():
    text = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
    start = text.index('published="/opt/openclaw-fo/first-officer-$release"')
    end = text.index("systemctl --user is-active --quiet openclaw-gateway.service", start)
    block = text[start:end]
    moved = block.index('sudo mv -nT "$publish_dir" "$published"')
    confirmed = block.index('sudo test ! -e "$publish_dir"')
    chmod_at = block.index('sudo chmod -R a-w,a+rX "$published"')
    verify_at = block.index('first-officer/install.py verify-candidate "$backup"')
    activate_at = block.index('first-officer/install.py activate "$backup"')
    assert moved < confirmed < chmod_at < verify_at < activate_at
    assert "Do not make the directory read-only" not in text
    assert "before that directory is made read-only" not in text


def test_one_activate_command_preflights_publishes_and_switches_in_order(tmp_path, capsys):
    recorder = Recorder(activate_replies())
    backup = tmp_path / "switch"

    def runner(entry, substitutions):
        result = recorder(entry, substitutions)
        if entry["what"] == "Prepare the switch and record the private rollback":
            write_private_rollback(backup)
        return result

    assert release.main(["activate", CANDIDATE, str(tmp_path / "a.tar"), str(tmp_path)], runner) == 0

    order = [what for what, _ in recorder.calls]
    assert order == [
        "Confirm the installed release verifier is a readable root-owned file",
        "Confirm the gateway is running before any change",
        "Confirm the GitHub CLI authenticates as the configured owner",
        "Confirm the existing Andrea read-only bridge answers",
        "Unpack the reviewed archive",
        "Compare the manifest digest with the approved release",
        "Check the candidate inventory against the reviewed digest",
        "Prepare the switch and record the private rollback",
        "Export the audit anchor payload",
        "Create the root-owned audit anchor directory",
        "Install the audit anchor for the service to read",
        "Give the audit anchor to root",
        "Keep the service from replacing the audit anchor directory",
        "Keep the audit anchor readable and not writable by the service",
        "Verify the audit chain after installing the seal",
        "Refuse to republish an existing release directory",
        "Reserve a private staging directory under the release store",
        "Copy the candidate into the staging directory",
        "Place the candidate at its store path",
        "Confirm the move installed this copy",
        "Set the installed read-only permissions for verification",
        "Verify the candidate with the installed verifier before the gateway switch",
        "Switch the gateway to the reviewed release",
        "Confirm the gateway is active after the switch",
    ]
    calls = dict(recorder.calls)
    assert calls["Copy the candidate into the staging directory"][-1] == f"{PUBLISH}/"
    assert calls["Place the candidate at its store path"][-2:] == [
        PUBLISH, f"/opt/openclaw-fo/first-officer-{CANDIDATE}"]
    verify = calls["Verify the candidate with the installed verifier before the gateway switch"]
    assert verify[0].endswith("python3") or verify[0].endswith(sys.executable)
    assert "verify-candidate" in verify
    assert calls["Set the installed read-only permissions for verification"] == [
        "sudo", "chmod", "-R", "a-w,a+rX", f"/opt/openclaw-fo/first-officer-{CANDIDATE}"]
    acceptance = os.environ["FO_ACCEPTANCE_ROOT"]
    exported = calls["Export the audit anchor payload"]
    assert exported[exported.index("--root") + 1] == acceptance
    assert "'$FO_ACCEPTANCE_ROOT'" not in " ".join(exported)
    sealed = calls["Verify the audit chain after installing the seal"]
    assert sealed[-2:] == [acceptance, "verify"]
    assert calls["Keep the audit anchor readable and not writable by the service"] == [
        "sudo", "chmod", "0444", "/var/lib/openclaw-fo/audit-anchor"]
    prepare = calls["Prepare the switch and record the private rollback"]
    assert prepare[-6:] == ["--release", CANDIDATE, "--from-release", ACTIVE, "--candidate", prepare[-1]]
    assert prepare[-1].endswith(f"/first-officer-{CANDIDATE}")
    printed = capsys.readouterr().out
    assert f"Keep this private rollback directory: {backup}" in printed
    assert f"rollback {backup}" in printed
    assert printed.index("-> Prepare the switch and record the private rollback") < printed.index(
        f"Keep this private rollback directory: {backup}")
    assert printed.index(f"Keep this private rollback directory: {backup}") < printed.index(
        "-> Refuse to republish an existing release directory")


def test_activation_copy_is_readable_by_the_operator_at_verification(tmp_path, monkeypatch):
    installer = importlib.import_module("install")
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(release, "STORE", store)
    monkeypatch.setattr(installer, "STORE", store)
    monkeypatch.setattr(installer.Path, "home", lambda: tmp_path)
    (tmp_path / ".openclaw").mkdir()

    # Run the filesystem commands without elevation. The verifier checks the
    # mode bits a non-owner would use as well as reading the file itself.
    sudo = tmp_path / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    sudo.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setattr(installer.os, "umask", lambda *_: 0o077)
    extract = tmp_path / "extract"
    unpacked = extract / f"first-officer-{CANDIDATE}"
    unpacked.mkdir(parents=True, mode=0o700)
    (unpacked / "plugin.mjs").write_text("candidate\n", encoding="utf-8")
    (unpacked / "plugin.mjs").chmod(0o600)
    published = store / unpacked.name
    verifier = store / "verify-install.py"
    verifier.write_text(
        "import json, stat, sys\n"
        "from pathlib import Path\n"
        "config, digest, rollback = sys.argv[1:]\n"
        "plugin = Path(json.loads(Path(config).read_text())['plugins']['load']['paths'][0])\n"
        "assert plugin.parent.name == f'first-officer-{digest}'\n"
        "assert plugin.parent.stat().st_mode & stat.S_IXOTH\n"
        "assert plugin.stat().st_mode & stat.S_IROTH\n"
        "assert plugin.read_text() == 'candidate\\n'\n",
        encoding="utf-8",
    )
    launcher = tmp_path / "openclaw"
    launcher.write_text("#!/bin/sh\necho '{\"valid\": true}'\n", encoding="utf-8")
    launcher.chmod(0o700)
    monkeypatch.setattr(installer, "systemctl", lambda *args, **kwargs: f"argv[]={launcher} gateway ;")

    backup = tmp_path / "switch"
    backup.mkdir(mode=0o700)
    for name, payload in {
        "plan.json": {"release": CANDIDATE},
        "candidate-openclaw.json": {"plugins": {"load": {"paths": [str(published / "plugin.mjs")]}}},
    }.items():
        member = backup / name
        member.write_text(json.dumps(payload), encoding="utf-8")
        member.chmod(0o600)

    def execute(entry, substitutions):
        argv = entry["argv"]
        if "verify-candidate" in argv:
            with monkeypatch.context() as context:
                context.setattr(sys, "argv", argv[2:])
                assert installer.main() == 0
            return ""
        return release.execute(entry, substitutions)

    plan = release.activate_plan(ROOT, CANDIDATE, tmp_path / "archive", extract, backup)
    actions = [entry["what"] for entry in plan]
    release.run(plan[actions.index("Reserve a private staging directory under the release store"):
                     actions.index("Switch the gateway to the reviewed release")], {}, execute)
    assert (published / "plugin.mjs").read_text(encoding="utf-8") == "candidate\n"
    assert unpacked.stat().st_mode & 0o777 == 0o700
    assert published.stat().st_mode & 0o777 == 0o555
    assert (published / "plugin.mjs").stat().st_mode & 0o777 == 0o444


def test_verifier_preflight_refuses_missing_or_symlink_paths(tmp_path):
    plan = release.activate_plan(ROOT, CANDIDATE, tmp_path / "a.tar", tmp_path, tmp_path / "switch")
    entry = plan[0]
    assert entry["argv"][-1] == "/opt/openclaw-fo/verify-install.py"
    check = entry["argv"][:-1]
    readable_root_file = Path("/usr/bin/env").resolve(strict=True)
    assert readable_root_file.is_file() and readable_root_file.stat().st_uid == 0
    assert subprocess.run([*check, readable_root_file], check=False).returncode == 0
    symlink = tmp_path / "verifier.py"
    symlink.symlink_to(readable_root_file)
    assert subprocess.run([*check, symlink], check=False).returncode == 1
    assert subprocess.run([*check, tmp_path / "missing.py"], check=False).returncode == 1


@pytest.mark.parametrize("uid,gid,mode,readable,expected", [
    (0, 0, 0o100644, True, 0),
    (1, 0, 0o100644, True, 1),
    (0, 1, 0o100644, True, 1),
    (0, 0, 0o100664, True, 1),
    (0, 0, 0o100646, True, 1),
    (0, 0, 0o100600, False, 1),
])
def test_verifier_preflight_requires_safe_metadata(monkeypatch, uid, gid, mode, readable, expected):
    fake_info = os.stat_result((mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))
    with monkeypatch.context() as fixture:
        fixture.setattr(os, "lstat", lambda _: fake_info)
        fixture.setattr(os, "access", lambda *_: readable)
        fixture.setattr(sys, "argv", ["-c", "/fixture/verifier.py"])
        with pytest.raises(SystemExit) as result:
            exec(release.VERIFIER_PREFLIGHT, {})
    assert result.value.code == expected


@pytest.mark.parametrize("failure,recovery_failure,last_step,error", [
    ("Confirm the gateway is active after the switch", False, "Confirm the gateway is active after the rollback",
     "Post-activation gateway check failed."),
    ("Confirm the gateway is active after the switch", True, "Restore the exact prior config and pin",
     "Automatic rollback failed."),
    ("Switch the gateway to the reviewed release", False, "Switch the gateway to the reviewed release",
     "Release step failed"),
])
def test_activation_recovers_only_after_successful_switch(tmp_path, capsys, failure, recovery_failure, last_step, error):
    recorder = Recorder(activate_replies(), failure=failure)
    backup = tmp_path / "switch"

    def runner(entry, substitutions):
        result = recorder(entry, substitutions)
        if entry["what"] == "Prepare the switch and record the private rollback":
            write_private_rollback(backup)
        if recovery_failure and entry["what"] == "Restore the exact prior config and pin":
            raise ValueError("The installer could not restore the gateway")
        return result

    assert release.main(["activate", CANDIDATE, str(tmp_path / "a.tar"), str(tmp_path)], runner) == 1
    order = [what for what, _ in recorder.calls]
    assert order[-1] == last_step
    assert order.count("Restore the exact prior config and pin") == (
        failure == "Confirm the gateway is active after the switch"
    )
    captured = capsys.readouterr()
    assert error in captured.err
    assert f"Keep this private rollback directory: {backup}" in captured.out
    assert f"First Officer release {CANDIDATE} is active." not in captured.out
    assert ("The prior config and pin are restored and the gateway is active." in captured.out) == (
        last_step == "Confirm the gateway is active after the rollback"
    )


def test_activate_stops_at_the_first_failure_and_points_at_the_rollback(tmp_path, capsys):
    recorder = Recorder(activate_replies(), failure="Check the candidate inventory against the reviewed digest")
    assert release.main(["activate", CANDIDATE, str(tmp_path / "a.tar"), str(tmp_path)], recorder) == 1

    assert [what for what, _ in recorder.calls][-1] == "Check the candidate inventory against the reviewed digest"
    assert "Prepare the switch and record the private rollback" not in dict(recorder.calls)
    assert "Refuse to republish an existing release directory" not in dict(recorder.calls)
    captured = capsys.readouterr()
    backup = tmp_path / "switch"
    assert "Release step failed" in captured.err
    assert "roll back with the printed private backup directory" not in captured.err
    assert "Keep this private rollback directory" not in captured.out
    assert "Keep this private rollback directory" not in captured.err
    assert str(backup) not in captured.out
    assert str(backup) not in captured.err
    assert not backup.exists()


def test_activate_points_at_the_rollback_only_after_the_backup_exists(tmp_path, capsys):
    recorder = Recorder(activate_replies())
    backup = tmp_path / "switch"

    def runner(entry, substitutions):
        result = recorder(entry, substitutions)
        if entry["what"] == "Prepare the switch and record the private rollback":
            write_private_rollback(backup)
        if entry["what"] == "Refuse to republish an existing release directory":
            raise ValueError("Release step failed: Refuse to republish an existing release directory")
        return result

    assert release.main(["activate", CANDIDATE, str(tmp_path / "a.tar"), str(tmp_path)], runner) == 1
    captured = capsys.readouterr()
    assert "Release step failed" in captured.err
    assert "Switch the gateway to the reviewed release" not in dict(recorder.calls)
    assert captured.out.index("-> Prepare the switch and record the private rollback") < captured.out.index(
        f"Keep this private rollback directory: {backup}")
    assert f"rollback {backup}" not in captured.out
    assert "roll back with the printed private backup directory" in captured.err
    assert str(backup) in captured.err or str(backup) in captured.out


def test_activate_does_not_advertise_an_incomplete_rollback_directory(tmp_path, capsys):
    recorder = Recorder(activate_replies())
    backup = tmp_path / "switch"

    def runner(entry, substitutions):
        result = recorder(entry, substitutions)
        if entry["what"] == "Prepare the switch and record the private rollback":
            backup.mkdir(mode=0o700)
            backup.chmod(0o700)
            plan = backup / "plan.json"
            plan.write_text("{}\n", encoding="utf-8")
            plan.chmod(0o600)
        if entry["what"] == "Place the candidate at its store path":
            raise ValueError("Release step failed: Place the candidate at its store path")
        return result

    assert release.main(["activate", CANDIDATE, str(tmp_path / "a.tar"), str(tmp_path)], runner) == 1
    captured = capsys.readouterr()
    assert "Release step failed" in captured.err
    assert "Switch the gateway to the reviewed release" not in dict(recorder.calls)
    assert "Keep this private rollback directory" not in captured.out
    assert "Keep this private rollback directory" not in captured.err
    assert "roll back with the printed private backup directory" not in captured.err
    assert str(backup) not in captured.out
    assert str(backup) not in captured.err


def test_activate_refuses_a_foreign_github_login_and_an_unexpected_staging_directory(tmp_path, capsys):
    wrong_owner = {**activate_replies(),
                   "Confirm the GitHub CLI authenticates as the configured owner": "someone-else"}
    recorder = Recorder(wrong_owner)
    assert release.main(["activate", CANDIDATE, str(tmp_path / "a.tar"), str(tmp_path)], recorder) == 1
    assert "unusable result" in capsys.readouterr().err
    assert [what for what, _ in recorder.calls] == [
        "Confirm the installed release verifier is a readable root-owned file",
        "Confirm the gateway is running before any change",
        "Confirm the GitHub CLI authenticates as the configured owner",
    ]

    escaped = {**activate_replies(),
               "Reserve a private staging directory under the release store": "/tmp/anywhere"}
    recorder = Recorder(escaped)
    assert release.main(["activate", CANDIDATE, str(tmp_path / "a.tar"), str(tmp_path)], recorder) == 1
    assert "unusable result" in capsys.readouterr().err
    assert "Copy the candidate into the staging directory" not in dict(recorder.calls)


@pytest.mark.parametrize("digest", [ACTIVE, "b" * 63, "B" * 64, "", "../escape"])
def test_activate_refuses_a_digest_that_is_not_a_reviewed_candidate(tmp_path, digest, capsys):
    recorder = Recorder(activate_replies())
    assert release.main(["activate", digest, str(tmp_path / "a.tar"), str(tmp_path)], recorder) == 1
    assert "reviewed candidate digest" in capsys.readouterr().err
    assert recorder.calls == []


def test_unset_acceptance_root_fails_closed_before_any_release_step(tmp_path, monkeypatch, capsys):
    """Dry-run and activate refuse an unset acceptance root and create no literal dollar directory."""
    monkeypatch.delenv("FO_ACCEPTANCE_ROOT", raising=False)
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    monkeypatch.chdir(work)
    literal = work / "$FO_ACCEPTANCE_ROOT"
    called = []

    def runner(entry, _substitutions):
        called.append(entry["what"])
        raise AssertionError(entry["what"])

    assert release.main(["--dry-run", "activate", CANDIDATE, str(tmp_path / "a.tar"), str(tmp_path)], runner) == 1
    dry = capsys.readouterr()
    assert called == []
    assert "'$FO_ACCEPTANCE_ROOT'" not in dry.out
    assert "$FO_ACCEPTANCE_ROOT" not in dry.out
    assert "FO_ACCEPTANCE_ROOT" in dry.err
    assert not literal.exists()
    assert not (tmp_path / "switch").exists()

    assert release.main(["activate", CANDIDATE, str(tmp_path / "a.tar"), str(tmp_path)], runner) == 1
    live = capsys.readouterr()
    assert called == []
    assert "'$FO_ACCEPTANCE_ROOT'" not in live.out
    assert "$FO_ACCEPTANCE_ROOT" not in live.out
    assert "FO_ACCEPTANCE_ROOT" in live.err
    assert not literal.exists()
    assert not (tmp_path / "switch").exists()


def test_rollback_restores_the_prior_release_and_confirms_the_service(tmp_path, capsys):
    recorder = Recorder()
    backup = tmp_path / "switch"
    assert release.main(["rollback", str(backup)], recorder) == 0
    assert [what for what, _ in recorder.calls] == [
        "Restore the exact prior config and pin",
        "Confirm the gateway is active after the rollback",
    ]
    assert dict(recorder.calls)["Restore the exact prior config and pin"][-2:] == ["rollback", str(backup)]
    assert "restored and the gateway is active" in capsys.readouterr().out


def test_no_release_step_pushes_merges_or_sends_telegram_traffic(monkeypatch):
    # Neutral fixed paths, so a temporary directory name cannot mask or fake a match.
    monkeypatch.setenv("FO_ACCEPTANCE_ROOT", "/private/acceptance")
    private = Path("/private")
    plans = [release.build_plan(ROOT, private / "prior", private / "staging", private / "home", REVIEWED),
             release.activate_plan(ROOT, CANDIDATE, private / "a.tar", private / "extract",
                                   private / "switch"),
             release.rollback_plan(ROOT, private / "switch")]
    for plan in plans:
        for entry in plan:
            text = " ".join(entry["argv"])
            assert not any(word in text for word in ("push", "merge", "telegram", "sendMessage"))
            assert "sudo" not in text or entry["argv"][0] == "sudo"
