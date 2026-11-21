# First Officer handoff

Resume on branch `codex-candace/fo-finish-20261120`. The commit that contains this file is the resume point. Use `git rev-parse HEAD`. Draft PR #36 targets `codex/first-officer-segment-1`.

## Done in this checkout

Steps 1 through 8, the staged rollback drill, the secret and personal-data scan, the review archive, and the review fixes for `0878331` have local evidence in `STATUS.md` and `REVIEW-0878331.md`. Lookup routes use a tool allowlist and restricted fetch, and they do not start the unrestricted agent. An outbound draft clears only after a tool receipt for that exact delivery. A Playwright report-level error fails the gate. Activation compares the approved digest and writes the private rollback before publish. The audit checkpoint rejects a truncated log or an orphan receipt. Routed LiteLLM prices must be finite and nonnegative, with one positive charge. HTTPS proxy URLs must be allowlisted and resolve to private addresses. The secret scan rejects directory symlinks and scans `data` and `artifacts`. The current review archive digest `4aa5e4395d16a0ab01c96784088bcd420f0e89500f5908d088a9deaf9a9708bf` is the pack from before these fixes. This pass did not pack a new archive. The offline gate passed 962 Python tests. The secret scan on this machine printed `Secret scan clean. Files 336. History commits 280. Logs 0.` before this commit. That file count includes the local gitignored `data/` tree, so a clean CI checkout reports fewer files. The Node gate passed 23 tests. Confirm the four pull-request jobs on this head. The live lease was not touched.

## Next

Follow `RESEARCH-SETUP.md`. Leave Phoenix, Restate and Hatchet uninstalled until Jeff approves a host and the licenses are checked. Phoenix 22.0.0 is the tracing target, not Langfuse, and its license is read from the image before any install. The status page stays on loopback and Tailscale Serve. Never Funnel and never a public bind. Cursor plan usage is still open. Do not activate the live pilot or roll back the live gateway. Do not install Llama Prompt Guard unless Jeff asks, and do not add LLM Guard.

## Open failures

None in the local gates at this commit. Gateway activation, timed Telegram checks, the live authority pilot and a live rollback still wait for Jeff. The gateway tar still needs the private prior bundle. The four pull-request jobs still need a green result on this head.

## Do not

No merges, no pushes to main, no force pushes, no deploys, no live gateway or Telegram changes, and no texts. Do not write to bobserver. Do not touch colima-vw, Vaultwarden, Podman, asgard, or Jeff's sessions.
