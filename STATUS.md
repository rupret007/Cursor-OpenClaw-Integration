# First Officer Segment 1 status

The gateway snapshot pointed to Round 13. Its conductor intentionally removes all model tools from ordinary turns and rejects tool calls except a single exact, approved gate proposal. The Telegram handlers match a smaller set of phrases than Jeff uses. The pinned bundle reproduced the problem on “my open PRs and CI”. The request went unclaimed, and the ordinary model turn had zero tools.

The copied settings showed the coding profile, an off sandbox, one main agent with no additional tool profile, and no provider-specific tool allowlist. The plugin applied the empty allowlist independently of provider support. Takeover and epoch checks applied to the exception for the exact proposal. The gateway’s trace report also recorded a separate latency problem from provider rate limits and retries.

The candidate uses a private Telegram dispatcher and a tool registry for fleet, gate, quota, GitHub, memory and Andrea. It starts typing and a short acknowledgment in parallel with the work. Standalone questions use one bounded Flash completion with a short, static system prompt. Longer and contextual work routes through the existing OpenClaw model path. The existing memory confirmation applies to natural-language saves. Andrea uses its bounded, read-only chat reader and returns aggregate activity and detected themes.

The canary against the pinned bundle failed before the change and passed against the staged candidate. OpenClaw 2026.11.5 then received and answered synthetic Telegram updates in an isolated network. Across seven replies, typing and the acknowledgment both arrived within 1.44 seconds and the final replies arrived within 2.43 seconds. The run covered fleet and gate receipts, the GitHub failure path, the memory confirmation and recall, Andrea GETs, and a standalone model answer. The actual GitHub query was accepted under the configured account on the Nuc. Installer tests also covered a failed readiness check and byte-for-byte restoration of the prior config and pin.

The unchanged baseline reported two Andrea doctor test failures in the local WSL run. The full hosted integration job and the Segment 1 job passed on the pull request. Jeff verified Andrea’s live read API on the Mac Mini, provisioned the restricted SSH key, and verified a live summary from bobserver through that key. The Mac runs only the aggregate summary command and keeps Andrea’s control credential locally. Jeff uploaded and published the revised bundle. Initial preparation exposed a root-owned service pin and an SSH shell with no OpenClaw launcher on its PATH. The installer preserves the pin’s owner, mode and verifier arguments and validates with the launcher from systemd. Activation and [the live Telegram acceptance checks](LIVE-TEST.md) are pending.

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

READY_FOR_JEFF_INSTALL
