# First Officer handoff

Resume on branch `codex-candace/fo-finish-20261120`. The commit that contains this file is the resume point. Use `git rev-parse HEAD`. Draft PR #36 targets `codex/first-officer-segment-1`.

## Done in this checkout

Steps 1 through 8, the staged rollback drill, the secret and personal-data scan, and the review archive have local evidence in `STATUS.md`. The scan printed a clean line for 288 files, 268 commits and 0 logs before this commit. The review archive digest is `1782ce0bdc84b2f969a86d12beaafba64c21968452e30f5cf322130b83836d11`. The offline gate passed 948 Python tests. The Node gate passed 23 tests. Confirm the four pull-request jobs on this head. The live lease was not touched.

## Next

Follow `RESEARCH-SETUP.md`. Build the prompt-injection guard as an explicit step: deterministic allowlists, content fences, and the existing tool-call gate. Llama Prompt Guard 2 is optional. Do not use LLM Guard. Leave Phoenix, Restate and Hatchet uninstalled until Jeff approves a host and the licenses are checked. Cursor plan usage is still open after that guard. Do not activate the live pilot or roll back the live gateway.

## Open failures

None in the local gates at this commit. Gateway activation, timed Telegram checks, the live authority pilot and a live rollback still wait for Jeff. The gateway tar still needs the private prior bundle.

## Do not

No merges, no pushes to main, no force pushes, no deploys, no live gateway or Telegram changes, and no texts. Do not write to bobserver. Do not touch colima-vw, Vaultwarden, Podman, asgard, or Jeff's sessions.
