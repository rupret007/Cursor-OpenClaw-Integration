import importlib.util
import json
import os
from pathlib import Path
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


def test_activate_checks_the_digest_and_writes_rollback_before_publish(tmp_path):
    recorder = Recorder(activate_replies())
    assert release.main(["activate", CANDIDATE, str(tmp_path / "a.tar"), str(tmp_path)], recorder) == 0
    order = [what for what, _ in recorder.calls]
    assert "Compare the manifest digest with the approved release" in order
    publish_at = order.index("Place the candidate at its store path")
    assert order.index("Compare the manifest digest with the approved release") < publish_at
    assert order.index("Check the candidate inventory against the reviewed digest") < publish_at
    assert order.index("Prepare the switch and record the private rollback") < publish_at
    assert publish_at < order.index("Verify the candidate with the installed verifier before publish") < order.index(
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
        "Confirm the gateway is running before any change",
        "Confirm the GitHub CLI authenticates as the configured owner",
        "Confirm the existing Andrea read-only bridge answers",
        "Unpack the reviewed archive",
        "Compare the manifest digest with the approved release",
        "Check the candidate inventory against the reviewed digest",
        "Prepare the switch and record the private rollback",
        "Refuse to republish an existing release directory",
        "Reserve a private staging directory under the release store",
        "Copy the candidate into the staging directory",
        "Place the candidate at its store path",
        "Confirm the move installed this copy",
        "Set the installed read-only permissions for verification",
        "Verify the candidate with the installed verifier before publish",
        "Switch the gateway to the reviewed release",
        "Confirm the gateway is active after the switch",
    ]
    calls = dict(recorder.calls)
    assert calls["Copy the candidate into the staging directory"][-1] == f"{PUBLISH}/"
    assert calls["Place the candidate at its store path"][-2:] == [
        PUBLISH, f"/opt/openclaw-fo/first-officer-{CANDIDATE}"]
    verify = calls["Verify the candidate with the installed verifier before publish"]
    assert verify[0].endswith("python3") or verify[0].endswith(sys.executable)
    assert "verify-candidate" in verify
    assert calls["Set the installed read-only permissions for verification"] == [
        "sudo", "chmod", "-R", "a-w,a+rX", f"/opt/openclaw-fo/first-officer-{CANDIDATE}"]
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
    assert len(recorder.calls) == 2

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


def test_no_release_step_pushes_merges_or_sends_telegram_traffic():
    # Neutral fixed paths, so a temporary directory name cannot mask or fake a match.
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
