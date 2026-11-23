
from pathlib import Path
import runpy
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
