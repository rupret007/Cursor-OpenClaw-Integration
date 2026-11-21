FAIL

# Independent release re-review — cfaa966

Reviewed exact head `cfaa966f40953e2c8ef4566bde7b1ae7173817f1` on
`codex-candace/fo-finish-20261120`, draft PR #36, against
`codex/first-officer-segment-1` at
`3d350baac7636fe068d5be6299991f5e776259b5`.

No live gateway, bobserver, Telegram, private-message, or production authority
action was invoked.

## Prior finding disposition

### PARTIAL — HIGH: prompt-injection fence was not connected to live lookup

The production dispatch is now connected to the boundary:
`services/andrea_sync/server.py:4510-4529` derives the lookup allowlist and passes
it to the hybrid runner, and
`scripts/andrea_sync_openclaw_hybrid.py:565-577` sends lookup routes directly to
`boundary.lookup_result()` without launching the unrestricted agent.
`first-officer/guard/boundary.py:56-63` rejects an outbound or non-allowlisted
tool, and `first-officer/guard/boundary.py:117-128` requires fetched text to be
fenced.

That fence is real, but the live features are not. The boundary only fetches a
literal URL found in the prompt (`first-officer/guard/boundary.py:100-129`).
The live-news and grounded-research prompts contain a natural-language query,
not a URL (`services/andrea_sync/server.py:3400-3411`,
`services/andrea_sync/server.py:3426-3436`). The recent-message route allows
`read_messages`, but `run_lookup()` has no implementation for it
(`first-officer/guard/boundary.py:27-32`, `first-officer/guard/boundary.py:107-129`).
The no-tool personal-assistant route likewise has no synthesis implementation.

Offline repro: call `run_openclaw_hybrid()` with ordinary
`structured_live_news` and `structured_recent_text_messages` prompts. Both
return `ok=false`, `status=refused`, no invoked tools, and
`lookup_tools_restricted`. This closes the unsafe path by disabling all four
lookup/synthesis paths, not by safely implementing them.

### PARTIAL — HIGH: outbound approval was not bound to an exact verified delivery

The draft now binds action, exact body, target, principal, origin, expiry, and
nonce (`services/andrea_sync/outbound.py:20-64`), and the production send path
checks context and an exact tool receipt before clearing the draft
(`services/andrea_sync/server.py:4014-4061`). A model-produced `ok` no longer
counts as delivery.

There is, however, no production delivery tool. The server only does
`getattr(self, "outbound_delivery_tool", None)` at
`services/andrea_sync/server.py:4032-4043`; repository-wide search finds no
production assignment, and `serve_forever()` constructs a plain `SyncServer`
at `services/andrea_sync/server.py:6303-6308`. The only assignments are test
stubs at `tests/test_andrea_sync.py:4018` and
`tests/test_andrea_sync.py:4033`. Another test replaces the entire send method
with synthetic success at `tests/test_andrea_sync.py:4084-4091`.

Offline repro: create a valid pending draft and confirm it on an unmodified
`SyncServer`. `_outbound_tool_receipt()` returns `None`, the draft remains, and
the result is `outbound_message_send_failed`. The security check is fail-closed,
but an exact production send has not been implemented or verified.

### FIXED — HIGH: Playwright report-level errors could pass

`first-officer/qa/flaky.py:122-134` preserves top-level report errors.
`first-officer/qa/flaky.py:186-195` validates a nonempty expected inventory, and
`first-officer/qa/flaky.py:240-250` makes any report error or project/test
inventory mismatch non-passing. The CLI requires that inventory at
`first-officer/qa/flaky.py:277-289`. The prior fatal-error/short-inventory repro
now stays red.

### PARTIAL — MEDIUM: activation published before digest validation and rollback

The approved manifest digest, complete inventory, and private rollback are now
ordered before publication (`first-officer/release.py:73-94`), and build refuses
to overwrite an archive (`first-officer/release.py:52-55`). Exact config/pin
restoration remains enforced by `first-officer/install.py:280-337`.

The reordered prepare step is not valid against the installed verifier. The
candidate config points its plugin paths at the final store directory
(`first-officer/install.py:138-161`), but prepare runs before that directory is
published (`first-officer/release.py:82-94`). `--candidate` is used only for
the Python inventory check (`first-officer/install.py:165-171`);
`verify_config()` is still called with the final release digest and has no
candidate-directory argument (`first-officer/install.py:110-136`,
`first-officer/install.py:188-197`). The existing release test records command
names rather than executing prepare, and the installer test stubs
`verify_config`, so neither exercises this ordering.

Result: an installed verifier that checks the release named by the candidate
config must reject before publication. If it does not check that directory,
the documented prepublication verifier validation did not occur. This is also
listed as a HIGH release blocker below.

### PARTIAL — MEDIUM: hash chain accepted complete truncation

The new keyed checkpoint detects deletion of `audit.log` while `audit.head` or
the key remains (`first-officer/acceptance/cycle.py:131-192`), and orphaned
routine receipts now fail (`first-officer/acceptance/cycle.py:441-454`).

The key, checkpoint, log, and receipt are all in the same owner-writable root.
When all four are deleted, `_check_head()` treats the state as fresh
(`first-officer/acceptance/cycle.py:177-183`) and `verify()` succeeds with zero
lines (`first-officer/acceptance/cycle.py:401-458`). Offline repro after one
routine: delete those four state files and call `verify()`; it returns `0`.
The checkpoint fixes partial loss, but it is not an external truncation anchor.

### FIXED — MEDIUM: unsafe LiteLLM pricing was accepted

`first-officer/litellm/configure.py:52-71` requires each price to be a finite,
non-boolean, nonnegative number, and
`first-officer/litellm/configure.py:97-103` requires at least one positive
charge dimension for every routed deployment.

### FIXED — MEDIUM: private LiteLLM HTTPS endpoints were not enforced

Both management and runtime paths require an exact `FO_LITELLM_HOSTS` entry and
private-only resolution before sending credentials
(`first-officer/litellm/configure.py:127-165`,
`first-officer/litellm/configure.py:173-187`,
`services/andrea_sync/litellm_api.py:27-64`,
`services/andrea_sync/litellm_api.py:67-90`).

### FIXED — MEDIUM: secret/PII scan ignored directory symlinks and named trees

`data` and `artifacts` are no longer globally skipped
(`first-officer/privacy/scan.py:21-27`). Directory symlinks are findings
(`first-officer/privacy/scan.py:156-166`), and the regular file walk scans
those named trees (`first-officer/privacy/scan.py:170-181`). The integration
gate invokes the scan before pytest (`scripts/test_integration.sh:75-76`).
A separate history-path gap is listed below.

## Release-blocking findings

### HIGH — All restricted lookup routes are nonfunctional

Locations: `first-officer/guard/boundary.py:27-32`,
`first-officer/guard/boundary.py:100-152`,
`services/andrea_sync/server.py:3400-3436`,
`scripts/andrea_sync_openclaw_hybrid.py:565-577`

The new boundary bypasses OpenClaw entirely, but implements only a literal-URL
fetch. Production live-news and grounded-research prompts provide queries, not
URLs; recent-message reads and personal-assistant synthesis have no executor.
The result is a safe refusal for every normal request. Unit tests that expect
successful news/message summaries mock `_create_openclaw_job`
(`tests/test_andrea_sync.py:3217-3241`,
`tests/test_andrea_sync.py:3508-3537`) and therefore do not exercise the new
production boundary.

Repro: invoke the hybrid runner offline with a normal query on each restricted
route. News, research, recent messages, and synthesis all return refused with
no tool invocation.

### HIGH — Confirmed outbound sends cannot succeed in production

Locations: `services/andrea_sync/server.py:4032-4061`,
`services/andrea_sync/server.py:6303-6308`,
`tests/test_andrea_sync.py:4018-4037`,
`tests/test_andrea_sync.py:4084-4091`

No production code installs `outbound_delivery_tool`. Every exact confirmation
therefore follows the failure branch and leaves the draft pending. The only
successful receipts are test-injected functions, and the end-to-end-named test
fakes `_send_pending_outbound_message` itself.

Repro: on a plain `SyncServer`, draft a message through the structured action
path and confirm it. The result is `outbound_message_send_failed`; no delivery
adapter is called and the draft remains.

### HIGH — Prepublication prepare validates a config that points to an unpublished release

Locations: `first-officer/release.py:73-94`,
`first-officer/install.py:110-136`,
`first-officer/install.py:138-171`,
`first-officer/install.py:188-197`

`release.py activate` calls `install.py prepare` before publishing. Prepare
validates the unpacked inventory, but generates a config whose paths name the
not-yet-published final store directory and passes that config to the installed
startup verifier. The verifier receives only the config, release digest, and
fallback digest; it never receives the unpacked candidate path.

Repro: inspect an activation dry-run and the candidate config generated by
`candidate_config()`. At the prepare step, the final store path does not yet
exist. A verifier that enforces the release inventory cannot pass. The tests
hide this by replacing the command runner and `verify_config`.

### HIGH — A Hatchet server error can dispatch the same lane to Restate

Locations: `first-officer/lanes/queue.py:40-42`,
`first-officer/lanes/queue.py:100-120`,
`first-officer/lanes/queue.py:228-279`,
`tests/first_officer/test_lane_queue.py:162-170`

HTTP server errors and request-timeout statuses do not prove that Hatchet
failed before accepting the workflow. A reverse proxy or application can
accept the trigger and then return an error. The dispatcher classifies these
responses as definite refusal and immediately sends the same lane to a
different backend. The idempotency key is not shared state across Hatchet and
Restate, so this can create two live lanes.

Repro: the existing `test_previously_accepted_restate_run_is_the_same_lane`
returns a server error from Hatchet and asserts that Restate is invoked. Change
the Hatchet fixture to record acceptance before returning that response; both
backends receive the lane.

## MEDIUM findings

### MEDIUM — Queue credentials can be sent to any named HTTPS host

Locations: `first-officer/lanes/queue.py:74-88`,
`first-officer/lanes/queue.py:146-168`, `INSTALL.md:75-86`

`endpoint()` rejects a public literal address, but any syntactically valid
hostname over HTTPS is accepted without an allowlist or address check. The
Hatchet and Restate bearer credentials are then sent to it. This contradicts
the documented private-HTTPS requirement.

Offline repro: `endpoint("https://queue.example.com")` succeeds. No request is
needed to demonstrate the policy bypass.

### MEDIUM — Complete acceptance-state reset still erases the audit history

Locations: `first-officer/acceptance/cycle.py:131-192`,
`first-officer/acceptance/cycle.py:401-458`

The co-located MAC catches a missing log only while another state file remains.
Deleting the complete state set makes a used root indistinguishable from a
fresh root and `verify()` returns success. Keep the trusted head outside the
mutable acceptance directory, or require a provisioned root identity that
cannot silently return to fresh state.

### MEDIUM — Secret/PII history scan suppresses sensitive pathnames instead of failing

Locations: `first-officer/privacy/scan.py:239-279`

For historical patches, a sensitive pathname is passed through
`public_name()` and replaced with `[redacted]`, but no finding is added.
Rename metadata is not parsed either. A file can therefore be committed under
a PII/credential-shaped name, renamed to a safe name, and pass the reachable
history scan as long as its contents are clean.

Offline repro: in a temporary full Git repository, commit a benign file under
a PII-shaped name, rename it to a safe name, then call `scan_history()`. It
returns zero findings.

### MEDIUM — CLI options can weaken the usage stop gate

Locations: `first-officer/usage/track.py:84-97`,
`first-officer/usage/track.py:176-203`, `INSTALL.md:61-73`

The documented warning and stop limits are safety thresholds, and the
documentation says overrides may only make them stricter. `assess()` accepts
any increasing pair up to 100, so the CLI can raise both thresholds and keep a
near-exhausted lane below the stop gate.

Offline repro: pass a 98-percent-used window with both CLI thresholds set to
100. `stop` is false. Enforce overrides no greater than the reviewed defaults.

## Gate and assertion review

No existing CI job or assertion was removed to make the branch green. The PR
adds full-history checkout for the privacy gate and adds separate LiteLLM and
tracing acceptance jobs. The exact-head GitHub checks all pass.

The green result is nevertheless insufficient because:

- lookup success tests mock the function now responsible for the restricted
  production route;
- outbound success is supplied by test-only adapters or by replacing the send
  method;
- release ordering tests record commands while installer tests stub the
  verifier;
- queue tests explicitly treat an ambiguous server error as permission to
  cross-dispatch; and
- the usage CLI can raise its own stop threshold.

## Verification performed

- `git rev-parse HEAD`, branch/status, and PR metadata: exact requested head,
  branch, draft state, and base verified.
- `git diff --check origin/codex/first-officer-segment-1...HEAD`: passed.
- `bash scripts/test_integration.sh`: passed; secret scan clean, 962 Python
  tests passed, handoff tests passed, live communication smoke skipped by
  design.
- `node --test tests/first_officer/segment1.test.mjs tests/first_officer/captain.test.mjs`:
  23 passed.
- Focused guard, release, cycle, flaky, LiteLLM, privacy, queue, and Andrea
  tests: passed.
- Safe offline adversarial repros: normal restricted lookups refused; public
  named queue HTTPS accepted; weakened usage stop stayed open; complete
  acceptance-state reset verified as fresh; historical PII pathname produced
  no scan finding.
- Exact-head PR checks: offline integration, Segment 1, LiteLLM acceptance, and
  tracing acceptance all passed.

## Remaining limitations

The prohibited live paths were not exercised. In particular, the installed
gateway verifier and private release bundle are not present in this checkout,
so the activation finding is established from the command ordering and
interfaces rather than a live activation attempt.
