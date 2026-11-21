# Test what is finished, right now

Everything on this page runs on the Nuc in the PR checkout. Nothing here contacts the gateway, Telegram, Andrea, bobserver or any paid service, and nothing here is a deploy. The live activations that still need your approval are listed in [JEFF-ACTIONS.md](JEFF-ACTIONS.md).

Start in the branch under review.

```bash
cd ~/work/openclaw-finish
git fetch origin
git switch codex-candace/fo-finish-20261120
git log --oneline -1
```

## 1. The offline gate

This is the same gate CI runs. It takes about two minutes.

```bash
bash scripts/test_integration.sh
```

Expect `928 passed` from the Python suite and a final line `All integration checks passed.`. The `Handoff failed.` lines inside the exhaustive check are expected: that check deliberately feeds bad arguments and requires each one to be refused.

## 2. The Segment 1 and captain gate

```bash
node --test tests/first_officer/segment1.test.mjs tests/first_officer/captain.test.mjs
```

Expect `# pass 22` and `# fail 0`.

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

## 4. Step 2: Codex and Claude usage tracking

Build a throwaway repository and two synthetic readings. The Codex file is shaped like a real `account/rateLimits/read` result and the Claude file like the response headers a claude.ai login receives. The five-hour Codex window is set over the 90 percent warning line on purpose.

```bash
rm -rf /tmp/fo-demo && mkdir /tmp/fo-demo && cd /tmp/fo-demo
git init -q -b demo/usage-check . && git config user.email d@example.invalid && git config user.name Demo
echo demo > README.md && git add README.md && git commit -q -m demo
cat > codex.json <<'JSON'
{"accountId": "demo", "ordinaryUsageAllowed": true, "rateLimits": {
  "primary": {"usedPercent": 93.4, "resetsAt": 1800000000, "windowDurationMins": 300},
  "secondary": {"usedPercent": 62, "resetsAt": 1800500000, "windowDurationMins": 10080},
  "credits": {"hasCredits": false, "unlimited": false}}}
JSON
cat > claude.json <<'JSON'
{"anthropic-ratelimit-unified-status": "allowed",
 "anthropic-ratelimit-unified-5h-utilization": "0.41",
 "anthropic-ratelimit-unified-5h-reset": "1800000000",
 "anthropic-ratelimit-unified-7d-utilization": "0.88",
 "anthropic-ratelimit-unified-7d-reset": "1800500000"}
JSON
cd ~/work/openclaw-finish
python3 -B first-officer/usage/track.py \
  --codex /tmp/fo-demo/codex.json --codex-account demo --claude /tmp/fo-demo/claude.json \
  --repo /tmp/fo-demo --step "Step 3" --next "Start step 4"
echo "exit $?"
```

Expect this report, then the handoff line and `exit 3`:

```
Claude session window at 41% used. It resets January 15, 2027 at 2:00:00 a.m. CST.
Claude weekly window at 88% used. It resets January 20, 2027 at 8:53:20 p.m. CST.
Codex 5h window at 93.4% used. It resets January 15, 2027 at 2:00:00 a.m. CST.
Codex weekly window at 62% used. It resets January 20, 2027 at 8:53:20 p.m. CST.
At or above the 90% warning threshold.
Handoff committed as <commit>.
```

Every reset is in your time zone. Exit `3` is the 90 percent warning and exit `4` is the 97 percent stop. Now check what the lane wrote and committed.

```bash
cat /tmp/fo-demo/HANDOFF.md
git -C /tmp/fo-demo log --oneline
git -C /tmp/fo-demo remote
```

The handoff names the branch, the head it describes, the step in progress and what comes next. The log shows one new commit named `Record First Officer usage warning and resume point`. The remote list is empty, which is the point: the lane commits locally and never pushes. To watch it refuse a dangerous case, rerun the same command with `--repo` pointed at a checkout on `main`; it stops with `Refusing to commit a handoff on a protected branch` and writes nothing.

## 5. The single activation command, with nothing activated

The whole release is one command with one rollback. Print the exact plan without running any of it.

```bash
python3 -B first-officer/release.py --dry-run build "$PRIOR_BUNDLE" /tmp/fo-staging
python3 -B first-officer/release.py --dry-run activate "$(printf 'a%.0s' {1..64})" /tmp/x.tar /tmp/fo-backup
```

The build plan stages the candidate from your verified prior bundle, runs the packaged Round 13 canary and the Segment 1 gate against it, and packs it. The activate plan confirms the running gateway, the GitHub login and the Andrea read-only bridge before it touches anything, publishes the candidate read-only under its digest, prepares the private rollback, switches, and confirms readiness. It stops at the first failure. When you are ready to activate for real, drop `--dry-run`; until then nothing in this section changes anything.

## 6. Step 3: Hatchet, then Restate only if Hatchet refuses

This starts nothing outside the test. Two loopback servers stand in for the documented Hatchet trigger and the Restate send ingress. Hatchet answers HTTP 503 and Restate accepts the lane.

```bash
python3 -m pytest -q tests/first_officer/test_lane_queue.py
```

Expect `16 passed`. One of those tests is the failover: the primary is called once, Restate is called once with the same lane and an idempotency key, and a second dispatch sends nothing. The other tests keep a timeout, a redirect and an authentication failure from failing over, and they keep two concurrent dispatchers to a single run. No token is written to the ledger.

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
PY
python3 first-officer/qa/flaky.py produce /tmp/fo-flaky/results.json /tmp/fo-flaky/events.json
python3 first-officer/qa/flaky.py evaluate /tmp/fo-flaky/events.json /tmp/fo-flaky/quarantine.json /tmp/fo-flaky/summary.json
echo "exit $?"
```

Expect:

```
Recorded 1 Playwright tests, including every attempt.
Passing 0. Non-passing 1. Quarantined 0.
exit 1
```

`/tmp/fo-flaky/summary.json` names the test under `flakes` with attempts `failed` then `passed`. Exit 1 is the point: a retry did not make the gate green. Add the same test to `quarantine.json` with an owner, an issue, a reason and a UTC window of at most 14 days, then evaluate again. The summary moves the test to `quarantined` and the exit stays 1.

## What is not provable here yet

Steps 6 through 8 in [STATUS.md](STATUS.md) are still in progress, and the timed Telegram checks and the supervised authority pilot still need a live gateway and your approval. Hatchet, Restate and the Phoenix UI are not installed. The queue and trace commands are ready to point at them when you approve a host.
