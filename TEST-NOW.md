# Test what is finished, right now

Everything on this page runs on the Nuc in the PR checkout. Nothing here contacts the gateway, Telegram, Andrea, bobserver or any paid service, and nothing here is a deploy. The live activations that still need your approval are listed in [JEFF-ACTIONS.md](JEFF-ACTIONS.md).

Start in the branch under review.

```bash
cd ~/work/openclaw-finish
git fetch origin
git switch codex-travis/fo-cursor-plan-20261122
git log --oneline -1
```

## 1. The offline gate

This is the same gate CI runs. It takes about two minutes.

```bash
OPENAI_API_ENABLED=0 RUN_COMM_SMOKE=0 bash scripts/test_integration.sh
```

Expect `1128 passed` from the Python suite and a final line `All integration checks passed.`. The `Handoff failed.` lines inside the exhaustive check are expected: that check deliberately feeds bad arguments and requires each one to be refused. Before the unit tests, the gate prints one line, `Secret scan clean. Files <n>. History commits <n>. Logs 0.`

## 2. The Segment 1 and captain gate

```bash
node --test tests/first_officer/segment1.test.mjs tests/first_officer/captain.test.mjs
```

Expect `# pass 23` and `# fail 0`.

## 3. Step 1: LiteLLM failover and per-lane budgets

This starts a throwaway PostgreSQL cluster, a real LiteLLM proxy and two fake providers on loopback, all inside a temporary directory that is deleted when it finishes. Set it up once.

```bash
python3 -m venv /tmp/fo-litellm-venv
/tmp/fo-litellm-venv/bin/pip install --requirement requirements-litellm-test.txt
schema=$(/tmp/fo-litellm-venv/bin/python -c 'from importlib.util import find_spec; from pathlib import Path; print(Path(find_spec("litellm").origin).parent / "proxy/schema.prisma")')
/tmp/fo-litellm-venv/bin/prisma generate --schema "$schema"

mkdir -p /tmp/fo-pg-dl /tmp/fo-litellm-postgres && cd /tmp/fo-pg-dl
apt-get download postgresql-18 postgresql-client-18 postgresql-common libpq5 libnuma1 liburing2
for package in *.deb; do dpkg-deb -x "$package" /tmp/fo-litellm-postgres; done
cd ~/work/openclaw-finish
```

`apt-get download` only fetches packages into the current directory. It needs no root and installs nothing. Then run the acceptance fixture. It takes about 45 seconds.

```bash
FO_TEST_POSTGRES_BIN=/tmp/fo-litellm-postgres/usr/lib/postgresql/18/bin \
LD_LIBRARY_PATH=/tmp/fo-litellm-postgres/usr/lib/x86_64-linux-gnu \
  /tmp/fo-litellm-venv/bin/python tests/first_officer/litellm-acceptance.py
```

Expect exactly this:

```
LiteLLM acceptance passed. Primary HTTP 503 once, fallback HTTP 200 three times.
Two fixture charges took lane A to its 0.20 budget. Its next request
returned HTTP 422 with zero upstream calls. Lane B completed with 0.10 tracked spend.
```

That one run proves four things: an unauthenticated completion is refused before any provider is called, a completion whose primary provider fails is served by the configured fallback, a lane that has spent its budget is refused with no upstream call at all, and a second lane is untouched by the first lane's spending. The spend figures are read out of the proxy's own PostgreSQL rows, not from what the API says about itself.

## 4. Step 2: Codex, Claude, and Cursor usage tracking

The focused gate exercises the tracker, the handoff commit, and the Cursor fallback wrapper with a fixture executable. It passed 41 tests on November 22, 2026 at `efa8cb0096b633b8455d5dcd41aea4f6afc96799`.

```bash
python3 -m pytest -q tests/first_officer/test_usage.py tests/test_cursor_handoff_model_flag.py
```

Cursor uses a private normalized JSON receipt from the existing login. Its fields are `account_binding`, `observed_at`, `period_start`, `period_end`, `included_spend`, `included_limit`, `billing`. The binding is an opaque 64-character lowercase hex identifier that Jeff assigns to the verified login and exports as `CURSOR_PLAN_ACCOUNT`. Keep the file owned by the invoking user with mode 0600, outside the checkout for fallback. The timestamps are integer Unix seconds and the spend and limit are integers in the same units. The billing period must contain the observation and still be open. The observation may be at most 90 seconds old and all five billing flags must be JSON false. If the existing session cannot supply the reading without a new token, leave it unread.

Run the tracker against three synthetic readings in a throwaway Git checkout. Cursor is set at exactly 90 percent.

```bash
fo_usage_demo=$(mktemp -d)
export CURSOR_PLAN_ACCOUNT="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
git -C "$fo_usage_demo" init -q -b demo/usage-check
git -C "$fo_usage_demo" -c user.email=demo@example.invalid -c user.name=Demo \
  commit --allow-empty -qm fixture
python3 - "$fo_usage_demo" <<'FIXTURES'
import json
import os
from pathlib import Path
import sys
import time

root = Path(sys.argv[1])
now = int(time.time())
reset = now + 3600
readings = {
    "codex": {"accountId": "demo", "ordinaryUsageAllowed": True, "rateLimits": {
        "primary": {"usedPercent": 40, "resetsAt": reset, "windowDurationMins": 300},
        "secondary": {"usedPercent": 62, "resetsAt": reset, "windowDurationMins": 10080},
        "credits": {"hasCredits": False, "unlimited": False}}},
    "claude": {"anthropic-ratelimit-unified-status": "allowed",
        "anthropic-ratelimit-unified-5h-utilization": "0.41",
        "anthropic-ratelimit-unified-5h-reset": str(reset),
        "anthropic-ratelimit-unified-7d-utilization": "0.88",
        "anthropic-ratelimit-unified-7d-reset": str(reset)},
    "cursor": {"account_binding": os.environ["CURSOR_PLAN_ACCOUNT"], "observed_at": now, "period_start": now - 86_400, "period_end": reset,
        "included_spend": 900, "included_limit": 1000, "billing": {
            "has_credits": False, "unlimited": False, "on_demand_enabled": False,
            "usage_billing_enabled": False, "auto_top_up_enabled": False}},
    "cursor-unread": {"status": "unread", "reason": "dashboard_token_required"},
}
for name, reading in readings.items():
    path = root / f"{name}.json"
    path.write_text(json.dumps(reading), encoding="utf-8")
    path.chmod(0o600)
FIXTURES

fo_usage_exit=0
python3 -B first-officer/usage/track.py \
  --codex "$fo_usage_demo/codex.json" --codex-account demo \
  --claude "$fo_usage_demo/claude.json" --cursor "$fo_usage_demo/cursor.json" \
  --repo "$fo_usage_demo" --step "Cursor fixture" --next "Resume after review" || fo_usage_exit=$?
echo "exit $fo_usage_exit"
git -C "$fo_usage_demo" show --format= --name-only HEAD
git -C "$fo_usage_demo" remote

fo_usage_exit=0
python3 -B first-officer/usage/track.py \
  --codex "$fo_usage_demo/codex.json" --codex-account demo \
  --claude "$fo_usage_demo/claude.json" --cursor "$fo_usage_demo/cursor-unread.json" || fo_usage_exit=$?
echo "exit $fo_usage_exit"
```

The first report prints `Cursor billing-period window at 90% used.` followed by the period end in Central Time. It exits 3 and commits only `HANDOFF.md`. The remote list is empty. The second report says `The cursor billing-period window was not read.` and exits 1 even with healthy Codex and Claude readings.

The automated cases exercise 89.9, 90, 96.9 and 97 percent, both CST and CDT, each paid flag, malformed or stale data, and a warning with an unrelated staged edit. They also test exact rounding at both boundaries, a repeated warning from the tracker entry point, and private-file and account-binding checks. A healthy 89.9 percent fixture starts one headless Composer 2.5 process in CLI and auto mode. Read-only jobs use Ask mode. The child receives neither ambient model credentials nor caller input. At 90 percent and above, or when billing evidence is unsafe or unread, the fixture records zero launches.

## 5. The single activation command, with nothing activated

The whole release is one activation command. The rollback restores the prior config, pin, and service. It does not delete the published candidate. Print the exact plan without running any of it.

```bash
export FO_ACCEPTANCE_ROOT="$root"
python3 -B first-officer/release.py --dry-run build "$PRIOR_BUNDLE" /tmp/fo-staging --reviewed-head "$REVIEWED_HEAD"
python3 -B first-officer/release.py --dry-run activate "$(printf 'a%.0s' {1..64})" /tmp/x.tar /tmp/fo-backup
```

The build plan requires a clean checkout at the full approved commit in `REVIEWED_HEAD`. It stages the candidate from your verified prior bundle and the approved overlay, runs the packaged Round 13 canary and the Segment 1 gate, scans the staged candidate for secrets and personal data, and packs it only if the destination archive is absent. Export `FO_ACCEPTANCE_ROOT` before the activate dry-run. If it is unset, that dry-run fails closed and does not print a quoted `$FO_ACCEPTANCE_ROOT`. The activate plan confirms the running gateway, the GitHub login and the Andrea read-only bridge, checks the approved inventory, prepares the private rollback, installs the audit seal, runs `cycle.py verify`, and verifies the read-only candidate with the installed verifier before the gateway switch. It stops at the first failure and checks gateway readiness after switching.

## 6. Step 3: Hatchet, then Restate only if Hatchet refuses

This starts nothing outside the test. Two loopback servers stand in for the documented Hatchet trigger and the Restate send ingress. Hatchet fails over only on a connection refusal or HTTP 425 or 429. The failover test uses HTTP 429, and a closed port is the other definite refusal. HTTP 408 and HTTP 503 stay unknown and are not failed over. Restate accepts the lane only after a definite refusal.

```bash
python3 -m pytest -q tests/first_officer/test_lane_queue.py
```

Expect `29 passed`. One of those tests is the failover: the primary is called once, Restate is called once with the same lane and an idempotency key, and a second dispatch sends nothing. The other tests keep a timeout, a redirect and an authentication failure from failing over, and they keep two concurrent dispatchers to a single run. No token is written to the ledger. Recovery tests resume from a recorded Hatchet refusal, retain an unresolved state when a receipt is truncated, and recover Restate acceptance by lookup without another send. The local status check needs neither credentials nor an existing ledger.

## 7. Step 4: one trace arrives at a local collector

This installs the pinned OpenLLMetry and Phoenix exporter packages and sends one span to a loopback collector that the test starts. It does not start Phoenix's UI and it does not contact a hosted collector.

```bash
python3 -m venv /tmp/fo-trace
/tmp/fo-trace/bin/pip install --requirement requirements-tracing-test.txt
/tmp/fo-trace/bin/python tests/first_officer/tracing-acceptance.py
```

The line `Failed to export spans batch code: 500, reason: Internal Server Error` is the refusal drill. Expect it, then these lines:

```
Phoenix refusal was not recorded as delivery.
Phoenix accepted one first-officer.lane trace at the loopback collector.
It carried lane review, the head, backend restate and outcome queued.
An ambient collector, proxy and API key were ignored, and a prompt was not exported.
```

OpenLLMetry also prints that it is exporting to a custom exporter and that no automatic instruments are set. That is this command's configuration: one explicit span, no provider instrumentation, and no cloud endpoint.

## 8. Step 5: a flake stays visible, and so does quarantine

This uses a Playwright JSON report. It does not launch a browser. The test failed once and passed on the retry.

```bash
mkdir -p /tmp/fo-flaky
python3 - <<'PY'
import json
from pathlib import Path
report = {"suites": [{"title": "", "file": "tests/lane.spec.js", "specs": [{
    "title": "blinks", "ok": True, "tests": [{"projectName": "chromium", "status": "flaky",
    "results": [
        {"status": "failed", "retry": 0, "errors": [{"message": "first try"}], "attachments": []},
        {"status": "passed", "retry": 1, "errors": [], "attachments": []}]}]}]}]}
Path("/tmp/fo-flaky/results.json").write_text(json.dumps(report))
Path("/tmp/fo-flaky/quarantine.json").write_text(json.dumps({"entries": []}))
Path("/tmp/fo-flaky/inventory.json").write_text(json.dumps({
    "projects": ["chromium"],
    "tests": ["chromium::tests/lane.spec.js::blinks"],
}))
PY
python3 first-officer/qa/flaky.py produce /tmp/fo-flaky/results.json /tmp/fo-flaky/events.json
python3 first-officer/qa/flaky.py evaluate /tmp/fo-flaky/results.json /tmp/fo-flaky/events.json /tmp/fo-flaky/quarantine.json /tmp/fo-flaky/inventory.json /tmp/fo-flaky/summary.json
echo "exit $?"
```

Expect:

```
Recorded 1 Playwright tests, including every attempt.
Passing 0. Non-passing 1. Quarantined 0.
exit 1
```

`/tmp/fo-flaky/summary.json` names the test under `flakes` with attempts `failed` then `passed`. Exit 1 is the point: a retry did not make the gate green. Add the same test to `quarantine.json` with an owner, an issue, a reason and a UTC window of at most 14 days, then evaluate again. The summary moves the test to `quarantined` and the exit stays 1.

## 9. Step 6: publish a status file the fleet reader accepts

This writes a local snapshot. It does not read the live machines and it does not open a port. The NucBox sample is nine hundred seconds old, so its load must be dropped.

```bash
mkdir -p /tmp/fo-status
python3 - <<'PY'
import json
from pathlib import Path
Path("/tmp/fo-status/evidence.json").write_text(json.dumps({
    "heavy_jobs": 1,
    "lane_counts": {"running": 1},
    "nodes": {
        "mac-mini": {"load1": 1.25, "load5": 1, "load15": 0.5, "codex_jobs": 1, "test_jobs": 0, "age_s": 1},
        "nucbox": {"load1": 99, "load5": 99, "load15": 99, "codex_jobs": 4, "test_jobs": 4, "age_s": 900},
    },
}))
PY
python3 first-officer/status/publish.py publish /tmp/fo-status/evidence.json /tmp/fo-status/STATUS.json
python3 - <<'PY'
import json
from pathlib import Path
path = Path("/tmp/fo-status/STATUS.json")
document = json.loads(path.read_text())
assert document["v"] == 1 and document["host"]["heavy_jobs"] == 1
assert document["nodes"]["nucbox"] == {"missing": True, "stale": True}
assert "99" not in path.read_text()
print("mode", oct(path.stat().st_mode & 0o777))
print("alerts", " ".join(document["alerts"]))
print("mac-mini load", document["nodes"]["mac-mini"]["load1"])
PY
```

Expect a `Published status` line, then:

```
mode 0o600
alerts stale
mac-mini load 1.25
```

The Node gate in section 2 includes the check that `readFleetSnapshot` accepts this publisher's file and that the fleet view leaves the stale NucBox load off the page. `python3 first-officer/status/publish.py serve /tmp/fo-status/STATUS.json --port 8765` serves that file on `127.0.0.1` until you stop it. The automated test checks that POST is refused and that a credential in the file is not shown.

## 10. Step 7: one verified fact, then a routine that sends nothing

This uses a private directory. It does not send a message, call a model, or install a timer. The nonce for this sentence is stable. The digest changes with the clock because the approval includes its expiry.

```bash
rm -rf /tmp/fo-cycle
python3 first-officer/acceptance/cycle.py --root /tmp/fo-cycle init
python3 first-officer/acceptance/cycle.py --root /tmp/fo-cycle propose "The lane review is weekly."
python3 - <<'PY'
import json, subprocess
from pathlib import Path
pending = json.loads(Path("/tmp/fo-cycle/pending.json").read_text())
subprocess.run(["python3", "first-officer/acceptance/cycle.py", "--root", "/tmp/fo-cycle", "confirm", pending["nonce"], pending["digest"]], check=True)
subprocess.run(["python3", "first-officer/acceptance/cycle.py", "--root", "/tmp/fo-cycle", "routine"], check=True)
subprocess.run(["python3", "first-officer/acceptance/cycle.py", "--root", "/tmp/fo-cycle", "verify"], check=True)
refused = subprocess.run(["python3", "first-officer/acceptance/cycle.py", "--root", "/tmp/fo-cycle", "confirm", pending["nonce"], pending["digest"]], check=True, capture_output=True, text=True)
print(refused.stdout, end="")
receipt = json.loads(Path("/tmp/fo-cycle/receipt-lane-review.json").read_text())
assert receipt["sent"] is False and receipt["called_model"] is False and receipt["changed_repo"] is False
print("receipt sent", receipt["sent"])
PY
```

Expect the propose command to print:

```
Pending save mem-b1562a220b03ce99 digest <64 hex> expires <unix>.
No verified saved fact.
```

Then expect:

```
Saved verified fact mem-b1562a220b03ce99.
Verified fact: The lane review is weekly. Approval <first 12 hex of that digest>.
Dry run lane-review at <unix> next <unix>. Sent no message.
Audit chain verified: 2 lines.
Refused save mem-b1562a220b03ce99: not-pending.
Verified fact: The lane review is weekly. Approval <same 12 hex>.
receipt sent False
```

The refusal is the second confirm. The fact stays one line, and the receipt says nothing was sent.

## 11. Step 8: rehearse failover on a staged lease

This creates `/tmp/fo-pilot` and writes one private lease file there. It does not read the live fence or start the watchdog.

```bash
rm -rf /tmp/fo-pilot
python3 first-officer/pilot/rehearse.py rehearse --root /tmp/fo-pilot --epoch 12 --now 1763700000
```

Expect:

```
Armed takeover ON while captain held epoch 12.
Watchdog moved the lease to first-officer at epoch 13.
Granted integration-ci at epoch 13.
One lane accepted for pull 33.
A second submission was a duplicate.
Pause rejected a new lane.
A missed check-in returned the lease to captain at epoch 14.
An old-epoch submit was rejected.
```

`python3 first-officer/pilot/rehearse.py checklist` prints the ten live steps. Those steps are not run by the command.

## 12. Rollback drill: restore the prior pin on a staged copy

This times the file restore only. It does not stop the gateway. The microsecond count will differ from the 114 us measured on November 20, 2026. The sentence shape will not.

```bash
rm -rf /tmp/fo-rollback
python3 first-officer/pilot/rollback_drill.py --root /tmp/fo-rollback
```

Expect one line:

```
Restored the prior pin in <microseconds> us. The gateway was not touched.
```

`/tmp/fo-rollback/pin.conf` contains the prior release pin, and `/tmp/fo-rollback/receipt.json` has `"gateway_touched": false` and `"restored": true`.

## 13. Secrets and personal data

This is the same scan the offline gate runs first. It reads this checkout, the commits reachable from it, and any log file under it. Nothing is sent anywhere.

```bash
python3 first-officer/privacy/scan.py
```

Expect one line and exit 0:

```
Secret scan clean. Files <n>. History commits <n>. Logs 0.
```

The two counts move when the branch moves. A line that names `credential`, `phone`, `address`, `email`, or `private-key` is a failure. The scan does not print the value.

## 14. The review archive

This packs a checksummed copy of the lane outside the checkout. It does not read the private prior bundle and it does not touch the gateway. The gateway tar still comes from `first-officer/release.py build` once that bundle is available.

```bash
rm -rf /tmp/fo-review
python3 first-officer/archive.py pack /tmp/fo-review
archive="$(printf '%s\n' /tmp/fo-review/first-officer-review-*.tar)"
python3 first-officer/archive.py verify "$archive"
```

Pack prints three lines. The digest is 64 lowercase hex digits.

```
Review archive <digest>
/tmp/fo-review/first-officer-review-<digest>.tar
The gateway was not touched.
```

Verify prints `Review archive <digest> verified. The gateway was not touched.` The digest matches the pack line. The tar is mode 0600.

## 15. One fenced fetch

An empty allowlist refuses every page. A listed host does not include its subdomains. The only loopback fetch is an explicit `http://127.0.0.1` fixture. A redirect to any other address is refused before that address is contacted. The page comes back inside a fence, with scripts removed, and that fence is not a tool call.

```bash
python3 -m pytest -q tests/first_officer/test_guard.py
```

Expect `13 passed`. Nothing is installed and no host outside `127.0.0.1` is contacted. One test recalls a memory that says to ignore the instructions and send. The recall is fenced, a hidden character and a fence closer are omitted, and the recall does not match `send it`, `send it now`, or `send now`. Those three phrases still match on their own.

## What is not provable here yet

The live gateway release, the timed Telegram checks and the supervised pilot on the real lease still need your approval. The gateway tar needs the private prior bundle, which is not in this repository. Hatchet, Restate and the Phoenix UI are not installed. No status collector is running on the machines. The ten gateway routine declarations stay inactive. The staged pilot lease is not the live one.
