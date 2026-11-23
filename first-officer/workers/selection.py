"""Choose among independently verified, approved Captain worker lanes."""

from dataclasses import dataclass
import math


PROVIDER_ORDER = {
    "chat": ("local-qwen",),
    "routine-code": ("codex", "cursor", "local-qwen"),
    "hard-code": ("claude", "codex"),
    "critique": ("gemini", "minimax", "local-qwen"),
}
FUNDING = {
    "local-qwen": "local",
    "codex": "subscription",
    "cursor": "subscription",
    "claude": "subscription",
    "gemini": "prepaid",
    "minimax": "prepaid",
}
MAX_AGE_SECONDS = 90
MAX_START_PERCENT = 90


@dataclass(frozen=True)
class WorkerReading:
    lane: str
    provider: str
    funding: str
    observed: int
    credentials_verified: bool
    included_only: bool
    available: bool
    in_use: bool
    used_percent: tuple[float, ...] = ()
    prepaid_remaining: float | None = None
    max_call_cost: float | None = None


@dataclass(frozen=True)
class InterruptedRun:
    lane: str
    reason: str
    process_stopped: bool
    handoff_verified: bool
    switches: int = 0


class AllowanceExhausted(Exception):
    """An adapter independently confirmed that the provider stopped at its allowance."""


class WorkerQueued(ValueError):
    """There is no eligible way to continue this job safely now."""


def _number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _eligible(worker, now):
    if (type(worker.observed) is not int or not now - MAX_AGE_SECONDS <= worker.observed <= now
            or worker.credentials_verified is not True or worker.included_only is not True
            or worker.available is not True or worker.in_use is not False
            or worker.funding != FUNDING[worker.provider]):
        return False
    if worker.funding == "subscription":
        expected = 1 if worker.provider == "cursor" else 2
        return isinstance(worker.used_percent, tuple) and len(worker.used_percent) == expected and all(
            _number(used) and 0 <= used < MAX_START_PERCENT for used in worker.used_percent
        )
    if worker.funding == "prepaid":
        remaining, cost = worker.prepaid_remaining, worker.max_call_cost
        return _number(remaining) and _number(cost) and 0 < cost <= remaining
    return True


def choose_worker(purpose, workers, approved, *, now, interrupted=None):
    """Return an eligible lane, or None to queue without calling a provider.

    Readings come from the caller's independent checks. `included_only` means
    the execution path enforces its funding boundary, including any fallback.
    """
    order = PROVIDER_ORDER.get(purpose)
    if order is None or type(now) is not int or now < 0:
        raise ValueError("Expected a supported task and current clock")
    rows = tuple(workers)
    if len({worker.lane for worker in rows}) != len(rows):
        raise ValueError("Worker lane identities must be unique")
    if interrupted is not None and (interrupted.reason != "allowance-exhausted"
            or interrupted.process_stopped is not True or interrupted.handoff_verified is not True
            or type(interrupted.switches) is not int or interrupted.switches != 0):
        return None
    excluded = interrupted.lane if interrupted is not None else None
    for provider in order:
        for worker in rows:
            if (worker.provider == provider and worker.lane in approved and worker.lane != excluded
                    and _eligible(worker, now)):
                return worker.lane
    return None


def generate_proposal(prompt, purpose, approved, *, readings, now, verify_scope, attempt, confirm_handoff):
    """Get one edit proposal, switching workers only after a confirmed allowance stop."""
    interrupted = None
    approved = frozenset(approved)
    for switches in (0, 1):
        verify_scope()
        lane = choose_worker(purpose, readings(), approved, now=now(), interrupted=interrupted)
        if lane is None:
            raise WorkerQueued("Work is queued until an approved worker has verified allowance")
        try:
            return attempt(lane, prompt)
        except AllowanceExhausted:
            if switches:
                raise WorkerQueued("Both approved attempts reached their allowance") from None
            interrupted = confirm_handoff(lane)
            if not isinstance(interrupted, InterruptedRun) or interrupted.lane != lane:
                raise WorkerQueued("The first worker's stop or checkpoint could not be verified") from None
