# First Officer handoff

Resume on branch `codex-candace/fo-finish-20261120`. The commit that contains this file is the resume point. Use `git rev-parse HEAD`. Draft PR #36 targets `codex/first-officer-segment-1`.

## Done in this checkout

Steps 1 through 8, the staged rollback drill, the secret and personal-data scan, the review archive, and the first prompt-injection slice have local evidence in `STATUS.md`. The scan is enforced by the offline gate. The current review archive digest is `d8f777a6f46812defffb3c1d277b558f35cd48e58136bcb6625e8c401e94d6bd`. The offline gate passed 952 Python tests. The Node gate passed 23 tests. The four pull-request jobs were green on `355747f`. Confirm them again on this head. The live lease was not touched.

## Next

Follow `RESEARCH-SETUP.md`. Finish the prompt-injection guard by fencing memory notes before they are injected. Keep the existing tool-call gate. Llama Prompt Guard 2 stays optional. Do not use LLM Guard. Leave Phoenix, Restate and Hatchet uninstalled until Jeff approves a host and the licenses are checked. Cursor plan usage is still open. Do not activate the live pilot or roll back the live gateway.

## Open failures

None in the local gates at this commit. Gateway activation, timed Telegram checks, the live authority pilot and a live rollback still wait for Jeff. The gateway tar still needs the private prior bundle.

## Do not

No merges, no pushes to main, no force pushes, no deploys, no live gateway or Telegram changes, and no texts. Do not write to bobserver. Do not touch colima-vw, Vaultwarden, Podman, asgard, or Jeff's sessions.
