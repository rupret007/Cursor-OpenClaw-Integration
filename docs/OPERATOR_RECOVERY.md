# Operator readiness recovery

This product slice starts from main `10863501a421fb3d17e52e8b02f0423ccc2318ff`
after #21. It improves the existing local monitor and doctor workflow; it does
not deploy or start a runtime.

## The useful path

1. Read **Who acts first** (or **Who reviews or refreshes next**) and the one
   next action. A failed current check and a missing check are different.
2. For expired evidence, inspect **Last verified result/blockers — historical**.
   Age cannot erase a previously verified owner hold or failed stage. Historical
   facts never count as current authority; unknown history does not mean passed.
3. When the named actor is allowed to proceed, select the offline command in
   the stable field and run it from this repository's terminal:

   ```sh
   bash scripts/andrea_doctor.sh --offline --receipt data/andrea-doctor-receipt.json
   ```

4. Choose **Refresh now** to reread the result. A new verified blocked result
   stays blocked; only new verified evidence can replace the old history.

The command field has no execution or clipboard handler. Its selection and
focus survive normal polling. Overview failure hides it and prior readiness
content until a fresh summary arrives. Existing task selection, deadlines, and
retry behavior from #21 remain unchanged.

## What offline means

Explicit `--offline` propagates to the existing readiness and reliability
capability checks. It skips external OpenClaw enumeration, GitHub auth probes,
model probes/guard, runtime health checks, and inherited live-enforcer/live-probe
options. No skill installation, service restart, settings mutation, or live
message occurs through this path. The legacy `SKIP_OPENCLAW_PROBE=1` variable
still only skips its existing model-probe step; it cannot mint an offline
receipt without the explicit flag.

The operator command still performs local checks: source hygiene, binary and
environment-key presence, and redacted CLI diagnostics. It is not a fixture-only
command and should not be described as surveying no host state. Our tests use
synthetic fixtures and isolate the actual host's runtime/credentials instead.

Unexecuted critical checks are `not_run`, with guidance that they were not
verified. They keep Grade C/no-go without inventing an installation or auth
failure. An offline exit `1` can therefore mean unverified readiness, not a
failed product test. The owner must separately approve any real live check;
passing local code tests never supplies that approval.

## Logic and compatibility

- Reuses `consume_receipt` and its code-owned actor/hold contract. No new store,
  validator, schema bump, or receipt/fingerprint rewrite.
- Failed-stage expiration preserves the owner hold and disallows continued
  offline code under the existing failed-stage policy. Grade C remains distinct:
  unrelated offline code can still be allowed while its owner readiness gate
  stays closed. Neither state authorizes autonomous/live operations.
- The monitor projects a bounded `last_verified` record only from verified
  stale evidence. Missing, invalid, unreadable, or replaced-invalid artifacts
  have no invented history. A new verified result supersedes old history.
- `refresh_command` is fixed source-owned text; arbitrary receipt commands,
  raw probe output, local absolute paths, and fingerprints are not exposed.
  Only the monitor's displayed generic `/tmp/` rerun text is adapted to its
  canonical `data/` destination. Other receipt consumers keep their contract.
- Current Grade B remains **ready with limits**, not blocked. A stale previous
  Grade A/B result is labeled historical and cannot become current readiness.
- Receipt freshness still uses the existing local file modification time.
  This is not cryptographic provenance or protection against a local user
  rewriting/touching files. No such stronger guarantee is claimed here.

## Verification and handoff

Use the pinned Python test dependency and Node runtime from the README. In a
credential-free, isolated checkout, run:

```sh
python3 -m pytest -q tests/test_andrea_dashboard_readiness.py tests/test_andrea_doctor_offline_recovery.py tests/test_andrea_dashboard_runtime.py
bash scripts/test_integration.sh
```

The focused tests cover failed → stale → fresh transitions, Grade C versus
failed gates, no history from invalid receipts, unchanged receipt bytes,
canonical command destination, hostile inherited live flags, legacy/default
behavior, and unverified capability guidance. The doctor test copies the actual
tracked scripts into a synthetic tree with trap providers and a fake local
diagnostic child; it never contacts real providers. The JavaScript tests execute
the actual rendered monitor against fake transport/timers and retain the #21
failure/race scenarios.

The macOS full-suite verification additionally denies actual host runtime/home
content and external network access, directs generated test artifacts to a
temporary tree, and restricts synthetic HTTP servers to an explicit free-port
pool. This is test-only isolation, not deployed product code. Exact test counts,
hosted run links, and tip SHA belong in the draft PR and coord #20 AFTER record.

No packaged installer exists for this Python/static integration; the build gate
is its existing Python/shell/JavaScript compilation and offline integration
suite, not a fabricated release. Karen receives an unmerged draft. Jeff retains
all real host/live decisions. `server.py`, the three exact send-confirm phrases,
and Private API OFF remain untouched.
