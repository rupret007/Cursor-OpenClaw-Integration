# First Officer Segment 1 status

The gateway snapshot pointed to Round 13. Its conductor intentionally removes all model tools from ordinary turns and rejects tool calls except a single exact, approved gate proposal. The Telegram handlers match a smaller set of phrases than Jeff uses. The pinned bundle reproduced the problem on “my open PRs and CI”. The request went unclaimed, and the ordinary model turn had zero tools.

The copied settings showed the coding profile, an off sandbox, one main agent with no additional tool profile, and no provider-specific tool allowlist. The plugin applied the empty allowlist independently of provider support. Takeover and epoch checks applied to the exception for the exact proposal. The gateway’s trace report also recorded a separate latency problem from provider rate limits and retries.

The candidate uses a private Telegram dispatcher and a tool registry for fleet, gate, quota, GitHub, memory and Andrea. It starts typing and a short acknowledgment in parallel with the work. Standalone questions use one bounded Flash completion with a short, static system prompt. Longer and contextual work routes through the existing OpenClaw model path. The existing memory confirmation applies to natural-language saves. Andrea uses its bounded, read-only chat reader and returns aggregate activity and detected themes.

The canary against the pinned bundle failed before the change and passed against the staged candidate. OpenClaw 2026.11.5 then received and answered synthetic Telegram updates in an isolated network. Across seven replies, typing and the acknowledgment both arrived within 1.44 seconds and the final replies arrived within 2.43 seconds. The run covered fleet and gate receipts, the GitHub failure path, the memory confirmation and recall, Andrea GETs, and a standalone model answer. The actual GitHub query was accepted under the configured account on the Nuc. Installer tests also covered a failed readiness check and byte-for-byte restoration of the prior config and pin.

The unchanged baseline reported two Andrea doctor test failures in the local WSL run. The full hosted integration job and the Segment 1 job passed on the pull request. Jeff verified Andrea’s live read API on the Mac Mini, provisioned the restricted SSH key, and verified a live summary from bobserver through that key. The Mac runs only the aggregate summary command and keeps Andrea’s control credential locally. Jeff uploaded and published the revised bundle. Initial preparation exposed a root-owned service pin and an SSH shell with no OpenClaw launcher on its PATH. The installer preserves the pin’s owner, mode and verifier arguments and validates with the launcher from systemd.

Jeff activated release `ed545847333ee1691824cd8e3b0a528882b2b11c7f2479c1f63af9a0e8def456` with installer commit `e0da3f1`. The installer reported a successful readiness check and the gateway was active. Jeff has the private rollback directory. Jeff reported the timed [live Telegram acceptance checks](LIVE-TEST.md) passed except for the Andrea text summary.

During follow-up, the deployed reader connected to Andrea and returned no recent previews. A later read also confirmed a partial chat list and zero recent-preview evidence, with no reported read errors. A Mac-local diagnostic now measures counts before and after the eligibility and freshness filters. The candidate can read up to four previews when chat metadata is stale and still enforces the 48-hour message window. The effect on Jeff's live data remains unverified.

Jeff ran the Mac-local diagnostic. Andrea returned 100 indexed chats, including 83 eligible one-to-one chats. None of those had metadata in the last 48 hours. All four selected chat reads succeeded and returned 27 valid preview rows, with zero recent previews. The sync diagnostic showed a connected channel, registered webhook, recent shadow poll, an allowlist and an older last inbound event. It could not classify the transport state. Bob the Bot is the deployed OpenClaw Telegram account. The candidate can fetch fresh history on the Mac from Andrea's configured BlueBubbles provider for a known one-to-one chat on the current allowlist. Automated tests exercise that fallback. The operator refresh requires positive recent preview evidence before switching the reader.

The authenticated follow-up found two self-chat entries and two groups on Andrea's allowlist, with no non-self one-to-one entry. The scoped live reader fetched 20 recent previews from the approved groups without read failures. With Jeff's authenticated Mac session, the reader refresh switched the existing restricted key and verified the group aggregate from Bobserver. The existing gateway can still parse the one-to-one reply. It needs the candidate gateway release to display the group aggregate in Telegram.

## Captain upgrade

Jeff approved gateway activation and completed sudo authentication. The first attempt stopped during Andrea preflight when the reader's scope could not be verified. The retry used the documented reader connectivity check. Bobserver now runs release `2df63b9d08cb2a6741c626db5469d5fd71add93a31ed87abed9fdfbc061ac802` from reviewed commit `07348e9`. Both plugin paths match the release digest, the gateway service is active and `/readyz` reports ready. The private rollback plan contains the exact previous Segment 1 config and pin. Use the [installer rollback procedure](INSTALL.md#roll-back-to-the-exact-prior-config-and-pin) with the private backup printed by the activation terminal. Timed Telegram acceptance is pending.

The live authority check showed Grok Bot still holds the shared lease. Takeover is off, the watchdog timer is inactive and the latest matching Grok check-in is stale. The pilot arming command requires a fresh Grok check-in and an explicit handoff at the expected epoch. The captain candidate reuses that lease and the existing lane runner. Jeff can grant the named Integration CI scope at the current epoch, pause dispatch and request handback in the private Telegram chat. The runner uses a separate subscription account from the two fleet quota receipts. The candidate reads the runner's own subscription quota before dispatch.

The captain can read current fleet, gate, PR and quota evidence, follow an exact PR head, launch one review lane for it and verify the result against the current GitHub head. The local tests exercise stale grants, duplicate submissions, pause, failed lanes, mismatched heads and the Andrea diagnostic. A private Nuc rehearsal using the installed fence and watchdog code confirmed explicit handoff, epoch advancement, pause, handback, stale-grant rejection and disarm. Production authority remained with Grok Bot. The [Captain guide](CAPTAIN.md) covers the Mac check, operator activation, supervised pilot and rollback.

## Later segments

- LiteLLM for all API calls with per-lane budgets and provider fallback.
- Codex 5-hour and weekly usage, Claude session and weekly usage, Central Time resets, 90% warnings and `HANDOFF.md`.
- Hatchet lane queue pilot with Restate as fallback.
- Phoenix tracing and OpenLLMetry.
- Playwright flaky reporter and quarantine.
- Shared status view and admin console.
- Prompt-injection guard, `sanitize.py`, `restricted_fetch.py`, grounded memory, skills and routines, approval binding and audit log.
- Dead-man watcher and tested failover.

The backlog references the bobserver lanes `infra-20261118`, `consolidate-20261119` and `arch-research-20261118`.

BLOCKED: The reviewed gateway is active. Timed Telegram acceptance and the supervised authority pilot remain pending.
