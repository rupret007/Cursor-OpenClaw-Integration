# First Officer handoff

Resume on branch `codex-candace/fo-finish-20261120`. The commit that contains this file is the resume point. Use `git rev-parse HEAD`. Draft PR #36 targets `codex/first-officer-segment-1`.

## Done in this checkout

Steps 1 through 8, the staged rollback drill, the secret and personal-data scan, the review archive, and the prompt-injection guard have local evidence in `STATUS.md`. The scan is enforced by the offline gate. Allowlisted fetches and recalled memory lines are fenced. A line that cannot be fenced is omitted. The current review archive digest is `4aa5e4395d16a0ab01c96784088bcd420f0e89500f5908d088a9deaf9a9708bf`. The offline gate passed 953 Python tests. The Node gate passed 23 tests. The four pull-request jobs were green on `1e2b085`. Confirm them again on this head. The live lease was not touched.

## Next

Follow `RESEARCH-SETUP.md`. Leave Phoenix, Restate and Hatchet uninstalled until Jeff approves a host and the licenses are checked. Phoenix 22.0.0 is the tracing target, not Langfuse, and its license is read from the image before any install. The status page stays on loopback and Tailscale Serve. Never Funnel and never a public bind. Cursor plan usage is still open. Do not activate the live pilot or roll back the live gateway. Do not install Llama Prompt Guard unless Jeff asks, and do not add LLM Guard.

## Open failures

None in the local gates at this commit. Gateway activation, timed Telegram checks, the live authority pilot and a live rollback still wait for Jeff. The gateway tar still needs the private prior bundle.

## Do not

No merges, no pushes to main, no force pushes, no deploys, no live gateway or Telegram changes, and no texts. Do not write to bobserver. Do not touch colima-vw, Vaultwarden, Podman, asgard, or Jeff's sessions.
