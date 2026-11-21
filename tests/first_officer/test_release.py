import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "first-officer"))
spec = importlib.util.spec_from_file_location("fo_release", ROOT / "first-officer/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

ACTIVE = "61ff4565dd5638a886effefbd720c92a17b7388acbc6eeaffb7779f78d3f1b36"
CANDIDATE = "a" * 64
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


def test_dry_run_prints_the_ordered_plan_and_runs_nothing(tmp_path, capsys):
    recorder = Recorder()
    assert release.main(["--dry-run", "build", str(tmp_path / "prior"), str(tmp_path)], recorder) == 0
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
    assert release.main(["build", str(tmp_path / "prior"), str(staging)], recorder) == 0

    assert [what for what, _ in recorder.calls] == [
        "Stage the candidate from the verified prior bundle",
        "Run the packaged Round 13 canary against the candidate",
        "Run the Segment 1 and captain gate",
        "Pack the candidate for the operator",
    ]
    assert recorder.calls[-1][1][-1] == f"first-officer-{CANDIDATE}"
    assert str(staging / f"first-officer-{CANDIDATE}.tar") in recorder.calls[-1][1]
    printed = capsys.readouterr().out
    assert f"Release {CANDIDATE}" in printed
    assert "Nothing was activated." in printed


def test_build_refuses_a_staging_result_that_is_not_a_release_digest(tmp_path, capsys):
    recorder = Recorder({"Stage the candidate from the verified prior bundle": "not-a-digest"})
    assert release.main(["build", str(tmp_path / "prior"), str(tmp_path)], recorder) == 1
    assert "unusable result" in capsys.readouterr().err
    assert [what for what, _ in recorder.calls] == ["Stage the candidate from the verified prior bundle"]


def test_one_activate_command_preflights_publishes_and_switches_in_order(tmp_path, capsys):
    recorder = Recorder(activate_replies())
    assert release.main(["activate", CANDIDATE, str(tmp_path / "a.tar"), str(tmp_path)], recorder) == 0

    order = [what for what, _ in recorder.calls]
    assert order == [
        "Confirm the gateway is running before any change",
        "Confirm the GitHub CLI authenticates as the configured owner",
        "Confirm the existing Andrea read-only bridge answers",
        "Unpack the reviewed archive",
        "Check the candidate inventory against the reviewed digest",
        "Refuse to republish an existing release directory",
        "Reserve a private staging directory under the release store",
        "Copy the candidate into the staging directory",
        "Remove write access from the staged release",
        "Publish the staged release under its digest",
        "Prepare the switch and record the private rollback",
        "Switch the gateway to the reviewed release",
        "Confirm the gateway is active after the switch",
    ]
    calls = dict(recorder.calls)
    assert calls["Copy the candidate into the staging directory"][-1] == f"{PUBLISH}/"
    assert calls["Publish the staged release under its digest"][-2:] == [
        PUBLISH, f"/opt/openclaw-fo/first-officer-{CANDIDATE}"]
    assert calls["Prepare the switch and record the private rollback"][-3:] == [
        "--release", CANDIDATE, "--from-release", ACTIVE][-3:]
    printed = capsys.readouterr().out
    assert f"Keep this private rollback directory: {tmp_path / 'switch'}" in printed
    assert f"rollback {tmp_path / 'switch'}" in printed


def test_activate_stops_at_the_first_failure_and_points_at_the_rollback(tmp_path, capsys):
    recorder = Recorder(activate_replies(), failure="Check the candidate inventory against the reviewed digest")
    assert release.main(["activate", CANDIDATE, str(tmp_path / "a.tar"), str(tmp_path)], recorder) == 1

    assert [what for what, _ in recorder.calls][-1] == "Check the candidate inventory against the reviewed digest"
    assert "Refuse to republish an existing release directory" not in dict(recorder.calls)
    captured = capsys.readouterr()
    assert "Release step failed" in captured.err
    assert "roll back with the printed private backup directory" in captured.err
    assert f"Keep this private rollback directory: {tmp_path / 'switch'}" in captured.out


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
    plans = [release.build_plan(ROOT, private / "prior", private / "staging", private / "home"),
             release.activate_plan(ROOT, CANDIDATE, private / "a.tar", private / "extract",
                                   private / "switch"),
             release.rollback_plan(ROOT, private / "switch")]
    for plan in plans:
        for entry in plan:
            text = " ".join(entry["argv"])
            assert not any(word in text for word in ("push", "merge", "telegram", "sendMessage"))
            assert "sudo" not in text or entry["argv"][0] == "sudo"
