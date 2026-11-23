"""Broker-owned review and coding queues using Captain's existing dispatcher."""

from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
from threading import Lock
import time

from lanes import queue
from standby import LeaseRejected, PRIMARY, open_private, read_private_json


QUEUE_TIMEOUT = 2.5
MAX_SCOPE_SECONDS = 3600
MAX_ACTIVE_PROJECTS = 100
OBSERVATION_INTERVAL = 30


class ReviewScopeRejected(ValueError):
    pass


class ProjectScopeRejected(ValueError):
    pass


class ProjectBusy(ValueError):
    pass


class BrokerQueue:
    def __init__(self, directory: Path, config, name, fields, *, clock):
        self.config = queue.validate_config(config)
        self.ledger = directory / f"{name}-queue-receipts.jsonl"
        self.scope = directory / f"{name}-scope.json"
        self.fields = fields
        self.clock = clock

    def key(self, request):
        if not isinstance(request, dict) or request.keys() != self.fields:
            raise ValueError("Expected an exact approved queue request")
        return queue.idempotency_key(request)

    def receipt(self, request):
        key = self.key(request)
        try:
            with queue.locked_ledger(self.ledger, read_only=True) as file:
                return queue.queue_state(file, request, key)
        except FileNotFoundError:
            return None

    def scope_matches(self, grant, request):
        fields = {"epoch", "request", "expires"}
        try:
            scope = read_private_json(self.scope)
            now = int(self.clock())
            return (isinstance(scope, dict) and scope.keys() == fields
                    and type(scope["epoch"]) is int and scope["epoch"] == grant.epoch
                    and self.key(scope["request"]) == self.key(request)
                    and type(scope["expires"]) is int and now < scope["expires"] <= now + MAX_SCOPE_SECONDS)
        except (OSError, ValueError, RecursionError):
            return False

    def dispatch(self, request, authorize):
        try:
            return queue.dispatch(self.config, request, self.ledger, timeout=QUEUE_TIMEOUT, authorize=authorize)
        except (LeaseRejected, ReviewScopeRejected, ProjectScopeRejected):
            raise
        except (OSError, ValueError):
            receipt = self.receipt(request)
            if receipt is None:
                raise OSError("Queue submission unavailable") from None
            return receipt


class ReviewQueue(BrokerQueue):
    def __init__(self, directory: Path, config, *, clock=time.time):
        super().__init__(directory, config, "review", {"lane", "head", "pull_request"}, clock=clock)
        if (self.config["hatchet"]["workflow"], self.config["restate"]["service"],
                self.config["restate"]["handler"]) != ("fo-captain-review", "CaptainReview", "verify"):
            raise ValueError("The standby broker needs the model-free review routes")

    def poll(self, request):
        return queue.poll_review(lambda: self.config, request, self.receipt(request))

    def submit(self, authority, grant, request):
        self.key(request)

        def authorize():
            authority.require_grant(grant)
            if not self.scope_matches(grant, request):
                raise ReviewScopeRejected("The exact review needs a current broker approval")

        authorize()
        return self.dispatch(request, authorize)


class ProjectQueue(BrokerQueue):
    def __init__(self, directory: Path, config, *, clock=time.time):
        super().__init__(directory, config, "project", queue.PROJECT_REQUEST, clock=clock)
        if self.config["hatchet"]["workflow"] != queue.PROJECT_WORKFLOW:
            raise ValueError("The standby broker needs the scoped Hatchet coding workflow")
        self.reservations = directory / "project-reservations.sqlite"
        self.observing = Lock()
        with open_private(directory, self.reservations.name), closing(sqlite3.connect(self.reservations)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS active_repo (repo TEXT PRIMARY KEY, lane_key TEXT NOT NULL, "
                       "request TEXT NOT NULL, plan_sha TEXT)")
            if "plan_sha" not in {column[1] for column in db.execute("PRAGMA table_info(active_repo)")}:
                db.execute("ALTER TABLE active_repo ADD COLUMN plan_sha TEXT")
            db.execute("CREATE TABLE IF NOT EXISTS project_observation (lane_key TEXT PRIMARY KEY, "
                       "state TEXT NOT NULL, commit_sha TEXT, observed INTEGER NOT NULL)")

    def reserve(self, request, key, plan):
        repo = request["repository"]
        with closing(sqlite3.connect(self.reservations)) as db, db:
            active = db.execute("SELECT lane_key, plan_sha FROM active_repo WHERE repo = ?", (repo,)).fetchone()
            if active is not None:
                if active[0] != key:
                    raise ProjectBusy("An earlier coding run is still reserved for this repository")
                if active[1] != plan:
                    raise ProjectScopeRejected("The reserved coding plan changed")
                return
            if db.execute("SELECT count(*) FROM active_repo").fetchone()[0] >= MAX_ACTIVE_PROJECTS:
                raise ProjectBusy("The coding reservation ledger is full")
            db.execute("INSERT INTO active_repo (repo, lane_key, request, plan_sha) VALUES (?, ?, ?, ?)",
                       (repo, key, queue.canonical(request), plan))

    def clear_refused(self, request, key):
        with closing(sqlite3.connect(self.reservations)) as db, db:
            db.execute("DELETE FROM active_repo WHERE repo = ? AND lane_key = ?", (request["repository"], key))

    def approved_plan(self, authority, grant, request):
        authority.require_grant(grant)
        key = self.key(request)
        if grant.node != PRIMARY or request["epoch"] != grant.epoch:
            raise ProjectScopeRejected("The exact coding run needs a current Nuc approval")
        try:
            scope = read_private_json(self.scope)
            now = int(self.clock())
            if (isinstance(scope, dict) and scope.keys() == {"epoch", "request", "expires", "worker", "planSha256"}
                    and type(scope["epoch"]) is int and scope["epoch"] == grant.epoch
                    and scope["worker"] == "local-qwen" and self.key(scope["request"]) == key
                    and type(scope["expires"]) is int and now < scope["expires"] <= now + MAX_SCOPE_SECONDS
                    and isinstance(scope["planSha256"], str) and re.fullmatch(r"[a-f0-9]{64}", scope["planSha256"])):
                return scope["planSha256"]
        except (OSError, ValueError, RecursionError):
            pass
        raise ProjectScopeRejected("The coding run needs an exact approved plan")

    def worker_plan(self, authority, grant, request):
        plan = self.approved_plan(authority, grant, request)
        key = self.key(request)
        with closing(sqlite3.connect(self.reservations)) as db:
            reserved = db.execute("SELECT lane_key, plan_sha FROM active_repo WHERE repo = ?",
                                  (request["repository"],)).fetchone()
        receipt = self.receipt(request)
        if reserved != (key, plan) or not receipt or receipt["phase"] not in ("pending", "uncertain", "queued"):
            raise ProjectScopeRejected("The worker needs the original reserved run and coding plan")
        authority.require_grant(grant)
        return plan

    def submit(self, authority, grant, request, expected_plan=None):
        key = self.key(request)
        plan = self.approved_plan(authority, grant, request)
        if expected_plan is not None and expected_plan != plan:
            raise ProjectScopeRejected("The Nuc and broker coding plan approvals differ")

        def authorize():
            if self.approved_plan(authority, grant, request) != plan:
                raise ProjectScopeRejected("The approved coding plan changed before submission")

        # Resolve the configured route before taking a repository reservation.
        queue.submission("hatchet", self.config["hatchet"], request, key)
        self.reserve(request, key, plan)
        receipt = self.dispatch(request, authorize)
        if receipt["phase"] == "refused":
            self.clear_refused(request, key)
        return receipt

    def active(self):
        with closing(sqlite3.connect(self.reservations)) as db:
            stored = db.execute("""
                SELECT a.repo, a.lane_key, a.request, o.state, o.commit_sha, o.observed
                FROM active_repo a LEFT JOIN project_observation o ON a.lane_key = o.lane_key ORDER BY a.repo
            """).fetchall()
        jobs = []
        for repository, key, raw, state, commit, observed in stored:
            request = json.loads(raw)
            if self.key(request) != key or request["repository"] != repository:
                raise ValueError("Coding reservation cannot be verified")
            if state is None:
                receipt = self.receipt(request)
                phase = receipt["phase"] if receipt else "pending"
                state = {"queued": "running", "refused": "refused"}.get(phase, "unresolved")
            job = {"request": request, "state": state}
            if observed is not None:
                job["observed"] = observed
            if state == "patch-ready":
                job["commit"] = commit
            jobs.append(job)
        return jobs

    def refresh(self, stop):
        while not stop.is_set():
            try:
                waiting = [job for job in self.active() if job["state"] in
                           ("running", "patch-ready", "worker-evidence-unavailable")]
                if waiting:
                    oldest = min(waiting, key=lambda job: job.get("observed", 0))
                    if self.clock() - oldest.get("observed", 0) >= OBSERVATION_INTERVAL:
                        self.inspect(oldest["request"])
            except (OSError, ValueError, sqlite3.Error):
                pass
            stop.wait(OBSERVATION_INTERVAL)

    def inspect(self, request):
        with self.observing:
            return self._inspect(request)

    def _inspect(self, request):
        key = self.key(request)
        with closing(sqlite3.connect(self.reservations)) as db:
            owned = db.execute("SELECT lane_key FROM active_repo WHERE repo = ?", (request["repository"],)).fetchone()
            previous = db.execute("SELECT commit_sha FROM project_observation WHERE lane_key = ?", (key,)).fetchone()
        if owned != (key,):
            raise ValueError("The exact coding reservation is unavailable")
        prior_commit = previous[0] if previous else None
        receipt = self.receipt(request)
        if not receipt or receipt["phase"] != "queued":
            return {"request": request, "state": "unresolved"}
        state, commit = "worker-evidence-unavailable", None
        try:
            row = queue.read_project_run(self.config, request, receipt, receipt["run"], QUEUE_TIMEOUT)
            if row.get("status") in ("FAILED", "CANCELLED"):
                state = "worker-failed"
            elif row.get("status") in ("QUEUED", "RUNNING"):
                state = "running"
            elif row.get("status") == "COMPLETED":
                commit = queue.project_artifact(request, row.get("output"), previous=prior_commit)
                state = "patch-ready" if commit else "invalid-artifact"
        except (OSError, ValueError):
            pass
        observed = int(self.clock())
        with closing(sqlite3.connect(self.reservations)) as db, db:
            db.execute("""
                INSERT INTO project_observation (lane_key, state, commit_sha, observed) VALUES (?, ?, ?, ?)
                ON CONFLICT(lane_key) DO UPDATE SET state=excluded.state, commit_sha=excluded.commit_sha,
                observed=excluded.observed
            """, (key, state, commit or prior_commit, observed))
        job = {"request": request, "state": state, "observed": observed}
        if commit is not None:
            job["commit"] = commit
        return job

    def reconcile(self, request, run):
        self.key(request)
        # The original run is read and matched to the old request's epoch.
        # The current lease grants lookups, never resubmission of an old epoch.
        return queue.reconcile_project(self.config, request, self.ledger, run, timeout=QUEUE_TIMEOUT)
