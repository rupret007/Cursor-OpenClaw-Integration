FAIL

# Independent release re-review — acc0167

Reviewed exact head `acc0167e11663e29f70209db687e60a04aaa540f` on
`codex-candace/fo-finish-20261120`, draft PR #36, against
`origin/codex/first-officer-segment-1` at
`3d350baac7636fe068d5be6299991f5e776259b5`.

No live gateway, bobserver, Telegram, private-message, or production action was
invoked. No secret, address, or personal contact value is reproduced here.

## Release-blocking findings

### HIGH — The production delivery tool invents delivery from its own outbox record

Locations: `services/andrea_sync/server.py:379-385`,
`services/andrea_sync/server.py:4042-4075`,
`services/andrea_sync/outbound.py:111-160`

The approval binding itself is real: target, exact body, principal, origin,
expiry, and nonce feed the draft digest, and the receipt must match that digest
before the draft is cleared (`services/andrea_sync/outbound.py:31-108`).

The production sink is not a message carrier. `SyncServer` installs
`_deliver_outbound`, which calls `deliver()` with a local JSONL path.
`deliver()` constructs `{"delivered": true, "source": "tool"}` itself before
performing any carrier I/O, appends that assertion to the file, and returns it
as the receipt. Repository-wide search finds no consumer of that outbox and no
BlueBubbles, Telegram, or other send adapter on this path. The server then
deletes the pending draft and tells the user that it sent the message.

Safe offline repro: call `outbound.deliver()` with a bound fixture draft. It
returns a receipt for which `delivered is True` and `receipt_matches()` is true,
although no carrier adapter was called. The focused production-path test at
`tests/test_andrea_sync.py:4042-4121` proves only one JSONL append; it does not
prove a send. The prior outbound finding is therefore **OPEN**, not fixed.

### HIGH — Rollback refuses the half-switched state an interrupted activation can leave

Locations: `first-officer/install.py:331-364`,
`first-officer/release.py:94-108`,
`tests/first_officer/test_install.py:86-100`

`switch()` stops the service, replaces the verifier pin, then replaces the
gateway config (`install.py:351-355`). Its in-process exception handler restores
both. A process termination or power loss between those two writes bypasses
that handler and leaves the old config with the new pin while the service is
stopped.

The documented rollback is not a recovery path for that state. With
`restore=True`, `switch()` requires the live config and pin to equal the complete
candidate pair before it changes anything (`install.py:331-336`). A mixed pair
is rejected as “changed since preparation,” so the prior pin is not restored
and the gateway may remain stopped.

Safe offline repro: prepare a temporary release, replace only the live pin with
`candidate-pin.conf`, leave the original config in place, and call
`switch(..., restore=True)`. It raises before restoration; the config remains
original and the pin remains candidate. Existing tests cover a catchable
readiness exception in the same Python process, not interruption between the
two file replacements.

### HIGH — An empty fetched page is reported as a grounded lookup success

Locations: `first-officer/guard/boundary.py:121-145`,
`first-officer/guard/boundary.py:252-261`,
`services/andrea_sync/server.py:3948-3975`,
`services/andrea_sync/server.py:3990-3997`

The fetch fence is connected to the production lookup route, and fetched text
cannot invoke an outbound tool. However, an allowlisted page whose visible text
is empty still creates a fenced page, so `run_lookup()` returns
`status="completed"` and `ok=true` with an empty summary. The server converts
that into the configured success fallback and a `*_ready` reason. For live news,
the user is told that a live summary was pulled when no grounded content exists.

Safe offline repro: return a valid empty fetch fence from the injected fetcher.
The result is `ok=true`, `status=completed`, and an empty summary; passing that
result through `_run_direct_openclaw_lookup()` selects the success fallback.
This is a fake success path and has no regression test.

## MEDIUM findings

### MEDIUM — Common dual-stack HTTPS sources are refused by the lookup guard

Locations: `first-officer/guard/restricted_fetch.py:111-116`,
`first-officer/guard/restricted_fetch.py:126-142`,
`first-officer/guard/boundary.py:243-260`

`system_resolve()` returns all stream addresses, but `public_unicast()` accepts
only global IPv4. `check_target()` rejects the target if any returned address
does not pass that predicate. A normal host with both public IPv4 and public
IPv6 records is therefore rejected and the lookup falls through to
`lookup_tools_restricted`.

Safe offline repro: inject a resolver returning one public address from each
address family. `check_target()` raises. The current guard tests cover only a
single IPv4 answer and loopback fixtures.

### MEDIUM — A prompt can expand an “exact page” allowlist to any path on an allowed host

Locations: `first-officer/guard/boundary.py:224-230`,
`first-officer/guard/restricted_fetch.py:92-108`,
`first-officer/guard/restricted_fetch.py:126-142`

Allowlist `sources` are validated as exact pages, but `run_lookup()` also adds
every URL found in the user prompt. `check_target()` validates only the host,
scheme, port, and absence of query/fragment; it does not require that prompt URL
to be one of `sources`. A prompt therefore causes an additional, unreviewed path
on an allowed host to be fetched. The response remains fenced, so this is not
an outbound-tool bypass, but it contradicts the documented exact-page boundary.

Safe offline repro: configure one source path and put a different path on the
same host in the prompt. An injected fetcher records both targets.

### MEDIUM — The audit chain still accepts deletion of all owner-writable trust state

Locations: `first-officer/acceptance/cycle.py:172-230`,
`first-officer/acceptance/cycle.py:440-495`,
`tests/first_officer/test_cycle.py:103-115`

The new sibling trust directory detects deletion of only the acceptance
directory, so the exact old four-file repro now fails. The acceptance root,
HMAC key, and sibling trust directory remain under the same owner-writable
parent. Deleting both directories makes `_check_head()` treat the state as
fresh, and `verify()` succeeds with zero lines.

Safe offline repro: run one routine, remove the acceptance root and its sibling
trust directory, then call `verify(root)`. It returns `0`. This prior finding is
**PARTIAL**.

### MEDIUM — Quoted Git paths can hide deleted historical credentials from the scan

Locations: `first-officer/privacy/scan.py:263-301`,
`tests/first_officer/test_privacy.py:79-96`

The new parser handles ordinary `diff --git a/... b/...` lines and ordinary
rename metadata. It does not parse Git’s C-quoted path form. When a filename
contains a character that forces quoting, neither side starts with `a/` and the
`" b/"` partition does not match. `path` remains empty, and lines 294-301 skip
all added and removed content for that patch.

Safe offline repro: in a temporary full Git repository, commit a file whose
name contains a tab and whose body contains a synthetic credential, delete it,
then call `scan_history()`. It reports zero findings. No sensitive value was
printed during the repro. The prior pathname/rename fix is **PARTIAL**.

### MEDIUM — Private-endpoint checks are not bound to the network connection

Locations: `first-officer/guard/restricted_fetch.py:126-142`,
`first-officer/guard/restricted_fetch.py:162-207`,
`first-officer/lanes/queue.py:97-133`,
`first-officer/lanes/queue.py:146-168`,
`first-officer/litellm/configure.py:145-187`,
`services/andrea_sync/litellm_api.py:42-64`,
`services/andrea_sync/litellm_api.py:104-122`

Fetch, queue, and LiteLLM validate one resolver result and then pass the
hostname to an opener that resolves it again. The validated addresses are not
pinned to the connection. An allowlisted hostname whose DNS changes between
check and use can therefore cross the reviewed public/private boundary; on the
credential-bearing queue and LiteLLM paths this can redirect a bearer key.

The exact host allowlists are an improvement, but the prior private-HTTPS
findings are **PARTIAL** until the connection is bound to the validated
addresses or equivalent transport enforcement is used.

### MEDIUM — Queue HTTPS literals bypass `FO_QUEUE_HOSTS`

Locations: `first-officer/lanes/queue.py:78-90`,
`first-officer/lanes/queue.py:117-133`,
`first-officer/lanes/queue.py:192-213`

Named HTTPS hosts require `FO_QUEUE_HOSTS`, but a private address literal takes
the other branch and needs only `_usable_private()`. With an empty host
allowlist, an arbitrary private literal is accepted and later receives the
queue bearer credential. This differs from the LiteLLM policy, which requires
the allowlist for both names and literals.

Safe offline repro: clear `FO_QUEUE_HOSTS` and call `endpoint()` with a private
HTTPS literal. It succeeds without making a request. The old “any named host”
finding is fixed for stable DNS, but this is a remaining credential-destination
gap.

## Prior finding disposition

The 16 prior headings collapse to the 12 unique fixes in the `FIXES` table;
duplicated PARTIAL and release-blocker headings share the disposition below.

| Prior finding / claimed fix | Disposition | Production evidence |
| --- | --- | --- |
| Restricted lookup routes (`9cede96`) | **PARTIAL** | The server adds the route allowlist at `server.py:4536-4542`, and the hybrid runner returns through the boundary without launching OpenClaw at `andrea_sync_openclaw_hybrid.py:567-577`. Normal local fixtures work at `boundary.py:217-291`, but empty-page false success, dual-stack refusal, and prompt-selected extra paths remain. |
| Exact outbound delivery (`62c46b3`, `4df9543`) | **OPEN** | Binding and receipt comparison are real at `outbound.py:31-108`; the installed production tool at `server.py:379-385` only self-certifies a JSONL append at `outbound.py:111-160`. |
| Playwright report-level errors (`d5e4ac0`) | **FIXED** | Producer errors survive at `qa/flaky.py:122-134`; exact inventory and errors make `ok` false at `qa/flaky.py:186-195` and `qa/flaky.py:240-250`; the CLI uses the checked producer output at `qa/flaky.py:284-293`. |
| Verify candidate before publish (`d822c29`) | **PARTIAL** | `release.py:74-95` verifies digest/inventory and calls prepare with `--candidate` before publish. `install.py:110-138` maps plugin paths to the unpacked tree and passes the candidate to `/opt/openclaw-fo/verify-install.py`. That installed verifier is not in this repository, so its new argument contract cannot be verified here; tests use a synthetic verifier at `test_install.py:221-242`. |
| Complete audit reset (`e7fcb72`) | **PARTIAL** | The sibling head at `acceptance/cycle.py:172-230` catches an in-root reset, but deleting both owner-writable directories restores fresh-state success. |
| Unsafe LiteLLM pricing (`3d31b06`) | **FIXED** | `_charge()` rejects booleans, non-finite, missing, disagreeing, and negative prices at `litellm/configure.py:52-71`; every routed deployment needs a positive charge dimension at `litellm/configure.py:97-103`. |
| Private LiteLLM HTTPS (`6dc9972`) | **PARTIAL** | Exact host/private checks run before management and runtime requests at `litellm/configure.py:145-187` and `litellm_api.py:42-90`, but the checked resolution is not bound to the later connection. |
| Directory symlinks and named trees (`eb96181`) | **FIXED** | Directory symlinks are findings and `data`/`artifacts` are walked at `privacy/scan.py:156-181`; the enforced scan runs at `scripts/test_integration.sh:73-76`. |
| Ambiguous Hatchet failover (`1de23b6`) | **FIXED** | Only definite 425/429 refusals fail over at `lanes/queue.py:43-46` and `lanes/queue.py:152-168`; uncertain outcomes stop before Restate at `lanes/queue.py:305-310`. |
| Queue named-host credential boundary (`43259d6`) | **PARTIAL** | Names need the exact host list and private resolution at `lanes/queue.py:97-114`, but resolution is not connection-bound and private literals bypass the list at `lanes/queue.py:117-133`. |
| Historical pathname and rename scan (`7f00504`) | **PARTIAL** | Ordinary pathnames and rename/copy metadata are scanned at `privacy/scan.py:263-287`; Git’s quoted path form bypasses both pathname and patch-content scanning. |
| Usage threshold overrides (`aad0759`) | **FIXED** | `usage/track.py:84-97` rejects warning or stop values above the reviewed defaults, and the CLI passes those values through at `usage/track.py:176-203`. |

## Gate and assertion review

No existing test, assertion, or PR job was removed or weakened in the branch
diff. The workflow adds full-history checkout for the privacy scan and adds the
LiteLLM and tracing jobs. All four exact-head PR jobs pass.

The green gates do not cover the blockers above:

- the outbound “production” test asserts a JSONL record, not carrier delivery;
- release tests record command order, and most installer tests stub
  `verify_config`; none simulates process loss between pin and config writes;
- lookup tests use one-address or loopback fixtures and do not cover empty
  visible content;
- privacy tests use ordinary unquoted Git paths; and
- `first-officer/qa/flaky.py` is a correct manual CLI, but no PR workflow or
  `scripts/test_integration.sh` step invokes it against a Playwright report.

## Verification performed

- `git rev-parse HEAD`, branch/status, base merge-base, and PR metadata:
  requested branch, draft PR, exact head, and exact base verified.
- `git diff --check origin/codex/first-officer-segment-1...HEAD`: passed.
- `bash scripts/test_integration.sh`: passed; secret scan clean (`Files 345`,
  `History commits 295`, `Logs 0`), 974 Python tests passed, 22 handoff tests
  passed, and the live communication smoke was skipped by design.
- The secret/PII scan was rerun after writing this report and remained clean
  (`Files 348`, `History commits 295`, `Logs 0`).
- `node --test tests/first_officer/segment1.test.mjs tests/first_officer/captain.test.mjs`:
  23 passed.
- Exact-head PR checks: Segment 1, offline integration, LiteLLM acceptance, and
  tracing acceptance all passed.
- Safe offline adversarial repros: self-certified outbound receipt; mixed
  config/pin rollback refusal; empty lookup success; dual-stack lookup refusal;
  prompt-expanded fetch target; complete audit reset; quoted-history-path scan
  bypass; and unlisted private queue literal acceptance.

## Review change and remaining limitations

- Changed file: `REVIEW-acc0167.md` only.
- Tests added or updated: none; this was a read-only code review plus offline
  verification.
- Final release status: **FAIL** despite green automated gates.
- The private release bundle, installed `verify-install.py`, production lookup
  allowlist, message carrier, and live gateway are not in this checkout and
  were not exercised. Those limits do not explain the self-certified delivery
  or interrupted-rollback failures, both of which are established in the
  in-repository production code.

## FIXES

Each finding was reproduced with a failing test, then fixed. The original FAIL above is unchanged. Local gates after these commits: secret scan clean (`Files 352`, `History commits 306`, `Logs 0`, before the commit that records this section; the file count includes the local gitignored `data/` tree), `bash scripts/test_integration.sh` passed 986 Python tests, and the Segment 1 and captain Node gate passed 23 tests. No live gateway, bobserver, Telegram, or production action was used. Phoenix, Restate and Hatchet were not installed.

| Finding | Commit | Test |
| --- | --- | --- |
| HIGH — the production delivery tool invents delivery | `74e85debbba402e6f3621663d8386537548cf79f` Record outbound delivery only when the carrier confirms it. | `tests/test_outbound_carrier.py::test_unconfirmed_delivery_is_not_recorded`, `tests/test_outbound_carrier.py::test_exact_confirmation_records_the_carrier_result_once`, and `tests/test_andrea_sync.py::TestAndreaSync::test_exact_confirmation_delivers_once_and_binds_the_receipt` |
| HIGH — rollback refuses the half-switched state | `248b7847a3f14d6f31434dc448110f168cf8e773` Recover a half-switched config and pin from either command. | `tests/first_officer/test_install.py::test_activation_and_rollback_recover_every_intermediate_pair` |
| HIGH — an empty fetched page is reported as a grounded success | `cabafab18f3609a65c788efd5d2f37de9db6c637` Refuse an empty fetched page instead of reporting a grounded success. `961cd0909f3468485fd4f82caccab500e414f618` keeps a generic completion on the route fallback and does not treat that completion as an empty page. | `tests/first_officer/test_guard.py::test_an_empty_fetched_page_is_not_a_grounded_success` and `tests/test_andrea_sync.py::TestAndreaSync::test_server_recent_text_messages_fall_back_when_openclaw_summary_is_generic` |
| MEDIUM — common dual-stack HTTPS sources are refused | `e59dab80a46f927e6a59af971fc7dfd99a13dc00` Allow a public IPv4 and IPv6 answer for an allowlisted fetch. | `tests/first_officer/test_guard.py::test_a_public_dual_stack_answer_is_allowed_and_a_private_address_is_not` |
| MEDIUM — a prompt can expand an exact-page allowlist | `f5ce135e0be85e2e1d8654645c7a4850d5fb4cdd` Fetch only the exact allowlisted pages for a lookup. | `tests/first_officer/test_guard.py::test_a_prompt_cannot_fetch_a_path_outside_the_exact_sources` |
| MEDIUM — the audit chain accepts deletion of owner-writable trust state | `790f60149a304f64ecc7e6c260ab125facc26a2c` Keep the audit anchor outside the acceptance parent. | `tests/first_officer/test_cycle.py::test_deleting_the_acceptance_directory_and_its_sibling_does_not_verify` |
| MEDIUM — quoted Git paths hide deleted historical credentials | `f47649751e6e29d082010d775d148b85b9966a4b` Scan credentials hidden by Git's quoted history paths. | `tests/first_officer/test_privacy.py::test_history_scans_a_quoted_path_and_the_deleted_credential` |
| MEDIUM — private-endpoint checks are not bound to the connection | `de5c846d17f3c07f919b0dcb5b0a44d941ab66b0` Connect to the addresses that passed the endpoint check. | `tests/first_officer/test_lane_queue.py::test_named_https_queue_connects_to_the_checked_address`, `tests/first_officer/test_litellm_policy.py::test_litellm_connects_to_the_checked_address`, and `tests/first_officer/test_guard.py::test_fetch_connects_to_the_checked_address_and_not_a_later_lookup` |
| MEDIUM — queue HTTPS literals bypass `FO_QUEUE_HOSTS` | `dc0fd1b01f3e3cb43393ac1485932f2a54f0bc39` Require the queue allowlist for a private HTTPS address. | `tests/first_officer/test_lane_queue.py::test_a_private_https_literal_needs_the_queue_allowlist` |
