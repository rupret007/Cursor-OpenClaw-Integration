
import json
from pathlib import Path
import runpy
import subprocess
import sys
import time

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "first-officer/workers/project-worker.py"


@pytest.fixture
def prepared(tmp_path):
    root = tmp_path / "authority"
    root.mkdir(mode=0o700)
    (root / "reviews").mkdir(mode=0o700)
    checkout = tmp_path / "source"
    checkout.mkdir(mode=0o700)
    worker = runpy.run_path(str(SOURCE))
    git = worker["git"]
    git(checkout, "init", "--quiet")
    git(checkout, "remote", "add", "origin", "https://github.com/example/integration.git")
    file = "review.mjs"
    source = checkout / file
    source.write_text('const status = "old";\n', encoding="utf-8")
    git(checkout, "add", file)
    git(checkout, "commit", "-qm", "Fixture")
    head = git(checkout, "rev-parse", "HEAD").decode().strip()
    identity = {"repository": "example/integration", "issue": 40, "head": head, "epoch": 2}
    request = {**identity, "lane": worker["queue"]["project_lane"](**identity)}
    save = worker["save"]
    save(root / "lease.json", {"active": True, "node": "nucbox", "epoch": 2})
    plan = {"request": identity, "checkout": str(checkout), "file": file, "before": '"old"',
            "prefix": "", "suffix": "", "prompt": "Write a clearer delivery status"}
    save(root / "project-worker.json", plan)
    fingerprint = worker["digest"]((root / "project-worker.json").read_bytes())
    save(root / "project-grant.json", {**identity, "worker": "local-qwen", "branch": "main",
                                      "expires": int(time.time()) + 90, "planSha256": fingerprint})
    save(root / "reviews/state.json", {"projectJobs": [
        {"request": request, "state": "reserved", "worker": "local-qwen",
         "branch": "main", "planSha256": fingerprint},
    ]})
    return worker, root, checkout, source, request, plan


def test_worker_writes_one_isolated_commit_and_reuses_receipt(prepared):
    worker, root, checkout, source, request, _ = prepared
    calls = []

    def model(prompt):
        calls.append(prompt)
        return 'Delivery was "unconfirmed".'

    run = worker["run"]
    git = worker["git"]
    result = run(root, request, model)
    assert set(result) == {"lane", "head", "commit"}
    assert run(root, request, model) == result
    assert len(calls) == 1
    assert source.read_text() == 'const status = "old";\n'
    assert git(checkout, "rev-parse", "HEAD").decode().strip() == request["head"]
    assert git(checkout, "rev-parse", result["commit"] + "^").decode().strip() == request["head"]
    assert git(checkout, "diff-tree", "--no-commit-id", "--name-only", "-r", result["commit"]).strip() == b"review.mjs"
    assert git(checkout, "show", result["commit"] + ":review.mjs") == b'const status = "Delivery was \\"unconfirmed\\".";\n'


@pytest.mark.parametrize("stop", ["valid", "wrong-checkpoint", "scope-revoked"])
def test_forced_allowance_stop_reuses_one_worktree_and_never_duplicates(prepared, stop):
    worker, root, checkout, source, request, plan = prepared
    policy = runpy.run_path(str(SOURCE.with_name("selection.py")))
    readings = [policy["WorkerReading"](name, provider, "subscription", 100, True, True, True, False, limits)
                for name, provider, limits in (("first", "codex", (10, 20)), ("second", "cursor", (20,)))]
    calls = []
    completed = []
    checkpoint = root / "handoff.json"

    def attempt(lane, _prompt):
        calls.append(lane)
        if lane == "second":
            return "Delivery was unconfirmed"
        head = "c" * 40 if stop == "wrong-checkpoint" else request["head"]
        fixture = "import sys; from pathlib import Path; Path(sys.argv[1]).write_text(sys.argv[2]); sys.exit(29)"
        completed.append(subprocess.run(
            [sys.executable, "-c", fixture, str(checkpoint), json.dumps({"base": head, "next": "draft"})],
            timeout=4, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
        if stop == "scope-revoked":
            worker["save"](root / "lease.json", {"active": False, "node": "nucbox", "epoch": request["epoch"]})
        assert completed[0].returncode == 29
        raise policy["AllowanceExhausted"]

    def verify_scope():
        assert worker["load_scope"](root, request) == plan

    def confirm(lane):
        saved = json.loads(checkpoint.read_text())
        return policy["InterruptedRun"](lane, "allowance-exhausted", completed[0].returncode == 29,
                                        saved == {"base": request["head"], "next": "draft"})

    def respond(prompt):
        return policy["generate_proposal"](prompt, "routine-code", {"first", "second"}, readings=lambda: readings,
                                           now=lambda: 100, verify_scope=verify_scope, attempt=attempt,
                                           confirm_handoff=confirm)

    if stop == "valid":
        result = worker["run"](root, request, respond)
        assert worker["run"](root, request, respond) == result
        assert calls == ["first", "second"]
        assert worker["git"](checkout, "rev-parse", result["commit"] + "^").decode().strip() == request["head"]
    else:
        with pytest.raises(ValueError, match="quota|allowance|scope"):
            worker["run"](root, request, respond)
        assert calls == ["first"]
    assert len(list((root / "project-runs").iterdir())) == 1
    assert source.read_text() == 'const status = "old";\n'


def test_worker_stops_on_invalid_preimage_or_parked_lease_before_model(prepared):
    worker, root, _, _, request, plan = prepared
    save, run = worker["save"], worker["run"]

    def no_model(_prompt):
        pytest.fail("The worker must stop before starting the model")

    unknown = {**plan, "before": '"unknown"'}
    with pytest.raises(ValueError, match="preimage"):
        worker["prepare"](unknown, request)
    save(root / "project-worker.json", unknown)
    with pytest.raises(ValueError, match="plan changed"):
        run(root, request, no_model)
    grant = worker["read_private"](root / "project-grant.json", 4096)
    save(root / "project-grant.json", {**grant, "planSha256": worker["digest"]((root / "project-worker.json").read_bytes())})
    with pytest.raises(ValueError, match="scope"):
        run(root, request, no_model)
    save(root / "project-worker.json", plan)
    save(root / "project-grant.json", grant)
    save(root / "lease.json", {"active": False, "node": "nucbox", "epoch": 2})
    with pytest.raises(ValueError, match="scope"):
        run(root, request, no_model)


def test_worker_crash_preserves_reservation_and_refuses_a_second_model_call(prepared):
    worker, root, checkout, _, request, _ = prepared

    def interrupted(_prompt):
        raise SystemExit(19)

    with pytest.raises(SystemExit):
        worker["run"](root, request, interrupted)
    restarted = runpy.run_path(str(SOURCE))
    with pytest.raises(ValueError, match="prior worker attempt"):
        restarted["run"](root, request, lambda _: pytest.fail("The restarted worker must not call the model"))
    assert worker["git"](checkout, "rev-parse", "HEAD").decode().strip() == request["head"]


@pytest.mark.parametrize("when", ["before-ref", "after-ref"])
def test_restart_finishes_the_same_checkpoint_without_calling_the_model(prepared, monkeypatch, when):
    worker, root, checkout, _, request, _ = prepared
    calls = []
    original_git = worker["git"]

    def model(prompt):
        calls.append(prompt)
        return "Delivery is unconfirmed"

    def interrupt(worktree, *args, **kwargs):
        if args[:2] == ("update-ref", "HEAD"):
            if when == "after-ref":
                original_git(worktree, *args, **kwargs)
            raise SystemExit(19)
        return original_git(worktree, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setitem(worker["run"].__globals__, "git", interrupt)
        with pytest.raises(SystemExit):
            worker["run"](root, request, model)
    restarted = runpy.run_path(str(SOURCE))
    result = restarted["run"](root, request, model)
    assert restarted["run"](root, request, model) == result
    assert len(calls) == 1
    candidate = root / "project-runs" / request["lane"] / "candidate"
    assert original_git(candidate, "rev-parse", "HEAD").decode().strip() == result["commit"]
    assert original_git(checkout, "rev-parse", "HEAD").decode().strip() == request["head"]
    assert original_git(candidate, "rev-parse", "HEAD^").decode().strip() == request["head"]


def test_restart_preserves_a_candidate_head_that_moved_past_the_checkpoint(prepared):
    worker, root, _, _, request, _ = prepared
    result = worker["run"](root, request, lambda _: "Delivery is unconfirmed")
    git = worker["git"]
    candidate = root / "project-runs" / request["lane"] / "candidate"
    git(candidate, "commit", "--allow-empty", "-qm", "Independent change")
    head = git(candidate, "rev-parse", "HEAD").decode().strip()
    assert head != result["commit"]
    restarted = runpy.run_path(str(SOURCE))
    with pytest.raises(ValueError, match="advanced beyond its checkpoint"):
        restarted["run"](root, request, lambda _: pytest.fail("Recovery must not call the model"))
    assert git(candidate, "rev-parse", "HEAD").decode().strip() == head


def test_worker_rechecks_scope_before_committing_a_model_result(prepared):
    worker, root, checkout, _, request, _ = prepared

    def model(_prompt):
        worker["save"](root / "lease.json", {"active": False, "node": "nucbox", "epoch": 2})
        return "Delivery is unconfirmed"

    with pytest.raises(ValueError, match="scope"):
        worker["run"](root, request, model)
    candidate = root / "project-runs" / request["lane"] / "candidate"
    assert worker["git"](candidate, "rev-parse", "HEAD").decode().strip() == request["head"]
    assert worker["git"](checkout, "rev-parse", "HEAD").decode().strip() == request["head"]
