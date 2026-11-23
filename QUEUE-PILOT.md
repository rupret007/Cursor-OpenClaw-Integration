# Hatchet and Restate queue pilot

The isolated Nuc pilot passed on 22 November 2026. It used synthetic jobs, local receipts, and two real workers sharing a scratch SQLite effect table. It made no production changes.

## Reproduce

Use an unprivileged x86-64 Ubuntu shell with user and network namespaces, Python with venv, and Node with `node:sqlite`. The Nuc used Ubuntu 26.04, Python 3.14.4, and Node 22.23.3. The runner downloads local copies of the public packages, creates a private scratch directory, and runs the servers inside a namespace with only loopback. It stops the servers when the run ends. Scratch data and logs stay on the Nuc.

```bash
cd /home/rupret/work/openclaw-finish
nice -n 10 bash tests/first_officer/queue-pilot/run.sh
```

The runner verifies the Hatchet Lite v0.120.19 amd64 image manifest and all layer digests before unpacking it. Hatchet uses a disposable PostgreSQL 18.6 database with UTC for both migration and runtime. The worker uses Hatchet Python SDK 2.0.0a6. The Restate server and CLI are 1.8.1 and matched their published archive checksums. The worker uses Restate TypeScript SDK 1.15.0. The Restate handler has five seconds of idempotency retention for the expiry test.

## Backend contracts

- Hatchet’s stable trigger returns the ID under `run.metadata.id`. Sending `returnOnlyId: true` avoids waiting for the run to appear in Hatchet’s read store. The status endpoint returns a JSON status string. The pilot required `COMPLETED` and a committed effect after acceptance. The pilot workflow sets a five-minute idempotency window using `additional_metadata.idempotencyKey`. A direct duplicate trigger returned HTTP 500 after the original run completed and the engine logged an idempotency collision. An ambiguous error stays on Hatchet. [Pinned trigger source](https://github.com/hatchet-dev/hatchet/blob/v0.120.19/api/v1/server/handlers/v1/workflow-runs/trigger.go) · [Hatchet idempotency expressions](https://docs.hatchet.run/v1/cel-expressions)
- Restate’s scoped send returns an invocation ID and an acceptance status. `POST /restate/lookup` derives an ID for the submitted route, scope, and key, including when the invocation is missing or expired. Only a recognized stage from `GET /restate/status/{id}` confirms the receipt. Both calls are read-only. The offline fixture previously treated a lookup ID as proof of acceptance. The new regression failed for both scoped and unscoped policies before the status check was added. [Pinned lookup source](https://github.com/restatedev/restate/blob/v1.8.1/crates/ingress-http/src/handler/lookup.rs) · [Pinned status source](https://github.com/restatedev/restate/blob/v1.8.1/crates/ingress-http/src/handler/invocation.rs) · [Restate flow control](https://docs.restate.dev/services/flow-control)

## Observed results

| Exercise | Evidence |
| --- | --- |
| Primary dispatch and definite refusal | Real Hatchet accepted and completed the jobs. A non-listening Hatchet endpoint caused a definite refusal. Real Restate accepted and completed the fallback jobs. |
| Reply lost after Hatchet accepted | The relay forwarded the job and withheld Hatchet’s accepted reply. The worker completed that run. A repeat stayed unresolved without another Hatchet request or a Restate request. |
| Reply lost after Restate accepted | The relay forwarded the job and withheld Restate’s accepted reply. Lookup and status recovered the same invocation on the original route and scope. A direct repeat on that scope returned the original ID. The same key under an alternate scope gave a different derived ID with HTTP 404 on status. |
| Missing and expired invocation | A send dropped before forwarding and a completed invocation that aged out both produced deterministic lookup IDs. Status provided no acceptance evidence. The ledger stayed unresolved and the expired case rejected a repeated send. |
| Restart after the side effect | The Restate worker was killed after committing its SQLite row and before acknowledging the durable step. The restarted worker completed the same invocation. The logical request still had one row, with the original timestamps. |
| Hatchet coding child interrupted after checkpoint | The pilot killed the real project worker subprocess after it saved the candidate commit and before it advanced the isolated worktree. With one task retry, Hatchet completed the original run. The duplicate submission reused that run ID. The parent checkout stayed intact and the loopback model received one call. The Hatchet server stayed up for this exercise. |
| Hatchet server restart during coding | The pilot confirmed the exact run was `RUNNING` while the loopback model request was open. It stopped Hatchet Lite, confirmed its listener was down, then started a fresh server against the same scratch PostgreSQL database. The original run returned one isolated commit from one model call. A duplicate submission reused that run ID. PostgreSQL and the SDK worker stayed up. |
| Concurrent duplicate and unrelated lane | The duplicate waited while the first reply was held and reused the receipt. The relay saw one POST for the pair. The unrelated lane was accepted in 1,064 ms while the reply was held. Both jobs completed. |
| Late reply | The pilot wrote the real acceptance receipt while the relay held the reply. The dispatcher refused the late transition and preserved the newer ledger row. |

Three jobs per backend used a 1.5-second synthetic slow dependency. The intervals record wall-clock time between submission and the start of the worker step. That includes ingress time and is an upper estimate of backend queue age. Acceptance times measure from dispatcher call to its receipt and include ledger writes and, for Restate, the refused primary connect.

| Backend | Acceptance times | Submit to worker start | Peak active worker steps |
| --- | --- | --- | --- |
| Hatchet | 1,098 / 1,050 / 1,047 ms | 61 / 523 / 1,045 ms | 1 |
| Restate | 38 / 34 / 32 ms | 45 / 1,535 / 3,038 ms | 1 |

## Limits and remaining risks

- Hatchet owned the pilot’s single worker slot. Add workflow-level concurrency in Hatchet when multiple workers need to share a downstream cap. Restate owned the limit of one invocation inside the pilot scope. Each scope has its own budget. Use downstream admission control if both backends need a shared cap.
- The scratch worker's `INSERT OR IGNORE` is safe because the row is the whole side effect and its primary key is the logical request. A real external side effect needs a destination idempotency key or a durable outbox and consumer deduplication. The pilot jobs did not invoke any live lane actions.
- Keep the file ledger for a single dispatcher host while lock contention and the size cap remain acceptable. Move to PostgreSQL before allowing multiple dispatcher hosts, or when measured lock contention or growth starts breaking the status or admission budget. Use a unique logical key and compare-and-set versions, and commit claims before network calls. Treat an expired claim as a reason to reconcile any pending send.
- A Hatchet submission that lost its reply still needs operator reconciliation. An expired or missing Restate receipt remains unresolved. The pilot’s short retention is for the test. Configure the deployed retention to cover the operational recovery window.

## Local regression gates

| Command | Result |
| --- | --- |
| `nice -n 10 python3 -B -m pytest -q tests/first_officer/test_lane_queue.py` | 33 passed |
| `OPENAI_API_ENABLED=0 RUN_COMM_SMOKE=0 nice -n 10 bash scripts/test_integration.sh` | 1,132 Python tests, 22 handoff skill tests, and all offline integration checks passed |
| `nice -n 10 node --test tests/first_officer/segment1.test.mjs tests/first_officer/captain.test.mjs` | 23 passed |
| `nice -n 10 python3 -B first-officer/privacy/scan.py` | Clean |
