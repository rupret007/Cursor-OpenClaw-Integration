"""Offline checks for the Captain's provider selection policy."""

from dataclasses import replace
from pathlib import Path
import runpy

import pytest


selection = runpy.run_path(str(Path(__file__).resolve().parents[2] / "first-officer/workers/selection.py"))
Worker = selection["WorkerReading"]
Interrupted = selection["InterruptedRun"]
choose = selection["choose_worker"]
NOW = 10_000


def worker(lane, provider, funding="subscription", *, used=(10, 20), **values):
    return Worker(lane, provider, funding, values.get("observed", NOW), True, True, True, False, used,
                  values.get("remaining"), values.get("cost"))


def test_routing_matches_the_task_and_uses_an_approved_lane():
    local = worker("local", "local-qwen", "local", used=())
    codex = worker("codex-1", "codex")
    cursor = worker("cursor-1", "cursor", used=(20,))
    claude = worker("claude-1", "claude")
    rows = [cursor, claude, codex, local]
    approval = {row.lane for row in rows}
    assert choose("chat", rows, approval, now=NOW) == "local"
    assert choose("routine-code", rows, approval, now=NOW) == "codex-1"
    assert choose("hard-code", rows, approval, now=NOW) == "claude-1"
    assert choose("routine-code", rows, {"cursor-1"}, now=NOW) == "cursor-1"
    assert choose("routine-code", rows, {"claude-1"}, now=NOW) is None


@pytest.mark.parametrize("changes", [
    {"credentials_verified": False}, {"included_only": False}, {"available": False}, {"in_use": True},
    {"observed": NOW - 91}, {"observed": NOW + 1}, {"used_percent": (10,)}, {"used_percent": (90, 1)},
    {"used_percent": (1, float("nan"))}, {"used_percent": None}, {"funding": "prepaid"},
])
def test_ineligible_worker_queues_without_a_route(changes):
    row = replace(worker("coding", "codex"), **changes)
    assert choose("routine-code", [row], {"coding"}, now=NOW) is None


def test_prepaid_route_needs_a_verified_balance_and_cost_ceiling():
    gemini = worker("g", "gemini", "prepaid", remaining=0, cost=0.01)
    minimax = worker("m", "minimax", "prepaid", remaining=0.01, cost=0.02)
    assert choose("critique", [gemini, minimax], {"g", "m"}, now=NOW) is None
    assert choose("critique", [replace(gemini, prepaid_remaining=1)], {"g"}, now=NOW) == "g"


def test_failover_requires_a_confirmed_stop_and_exact_handoff():
    rows = [worker("before", "codex"), worker("after", "codex")]
    approved = {"before", "after"}
    checkpoint = Interrupted("before", "allowance-exhausted", True, True)
    assert choose("routine-code", rows, approved, now=NOW, interrupted=checkpoint) == "after"
    for change in ({"process_stopped": False}, {"handoff_verified": False}, {"reason": "unconfirmed-outcome"},
                   {"switches": 1}):
        assert choose("routine-code", rows, approved, now=NOW,
                      interrupted=replace(checkpoint, **change)) is None
