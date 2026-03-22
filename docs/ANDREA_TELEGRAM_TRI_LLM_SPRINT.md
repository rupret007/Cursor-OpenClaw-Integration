# Andrea Telegram Tri-LLM Sprint

Use this mode when you want a deliberately aggressive Telegram session where Andrea/OpenClaw coordinates multiple model lanes and Cursor handles the heavy repo execution.

## What this mode does

- routes the task through the existing Telegram lockstep lane
- keeps Andrea as the narrator in Telegram
- asks OpenClaw to coordinate model usage by strength
- keeps Cursor as the repo-heavy execution specialist
- can expose a much richer collaboration stream in Telegram when you explicitly request full dialogue

## Model role split

For collaborative Telegram sprints, the intended role split is:

- Gemini 2.5: broad planning, decomposition, first-pass reasoning
- Minimax 2.7: alternate analysis, critique, second-opinion checks
- OpenAI: precise synthesis, instruction following, tool-friendly substeps
- Cursor: repo-heavy execution, code changes, implementation follow-through

OpenClaw should not call every model for every request. It should use only the best model needed for each subtask.

## How to trigger it

The current Telegram routing understands:

- `@Andrea`
  - prefer Andrea/OpenClaw-first handling
- `@Cursor`
  - force Cursor-primary collaboration
- `@Andrea @Cursor`
  - request joint collaboration
- `@Gemini`, `@Minimax`, `@OpenAI`, or `@GPT`
  - request a preferred OpenClaw model lane for the coordination pass
- phrases like `work together`, `team up`, `double-check`
  - also request collaboration
- phrases like `show the full dialogue`, `show all handoffs`, `visible collaboration`
  - enable richer Telegram collaboration updates

## Recommended one-hour sprint structure

Ask the system to do all of the following in one message:

1. triage the best 2-4 improvements for a one-hour sprint
2. explicitly split the work across OpenClaw reasoning and Cursor execution
3. show the major handoffs and collaboration notes in Telegram
4. run tests and validation for any changes
5. review any touched docs
6. finish with commit, refresh, restart, test, and push only if you explicitly request it

## What should appear in Telegram

When full visibility is requested, the thread should show:

- the normal Andrea task acknowledgement
- the requested OpenClaw lane when you directly address a model like `@Gemini`
- a richer running update when the collaborative lane starts
- coordination updates when OpenClaw begins triage or hands execution to Cursor, including the active provider/model when available
- the normal final summary with technical details

This still does not mean every private chain-of-thought token is streamed. It means meaningful collaboration and handoff updates are surfaced in the thread instead of only a minimal `queued/running/completed` lifecycle.

## Risks and trade-offs

- full Telegram dialogue is noisier than the default user-friendly mode
- a long repo task can still spend most of its time inside OpenClaw or Cursor execution before another visible update appears
- this mode is best for intentional sprint sessions, not casual everyday assistant turns

## Operator checklist

Before running this mode, confirm:

- `andrea_sync` is healthy
- Telegram webhook is healthy
- OpenClaw gateway is healthy
- `cursor_handoff` is available
- the repo is in a known good state if you expect edits and a push

Recommended commands:

```bash
bash scripts/andrea_wrap_up_prereqs.sh
bash scripts/andrea_communication_smoke.sh
```

## Copy-paste kickoff prompt

Use this in Telegram when you want the aggressive one-hour sprint:

```text
@Andrea @Cursor @Gemini work together on a focused one-hour improvement sprint for this repository and show the full dialogue, major handoffs, and visible collaboration in this Telegram thread:

/Users/andreabot/repos/Cursor-OpenClaw-Integration

I want the most aggressive masterclass version of this session that is still disciplined and useful.

Execution model:
- Andrea/OpenClaw should act as the lead coordinator and deliberately use the best model for each subtask:
  - Gemini 2.5 for broad planning and decomposition
  - Minimax 2.7 for alternate analysis and critique
  - OpenAI for precise synthesis and instruction-following
  - Cursor for heavy repo execution, code edits, implementation, and coding-agent work
- Since I addressed `@Gemini`, start the coordination pass there when available, and note any fallback if a different lane is safer or required.
- Do not use every model for every step. Use each one only where it adds real value.
- Keep one shared task timeline in Telegram and make the coordination visible.

Goals:
1. Spend a few minutes triaging the best 2-4 improvements that can realistically be finished in one hour.
2. Split the work deliberately across OpenClaw reasoning/review and Cursor execution.
3. Improve the repo in the highest-value ways possible for reliability, polish, testing, docs, ops, and overall assistant quality.
4. Prefer real gains over churn. Avoid random cosmetic changes.
5. Review docs/readmes you touch so they stay current and readable.

How to work:
- Start with a short triage and division-of-labor note.
- Show meaningful collaboration updates and handoffs in Telegram as you go.
- Run the right tests and validation for anything you change.
- Do not revert unrelated work.
- Do not use destructive git commands.
- Do not push unless I explicitly ask.
- If the result is truly ready and I later approve it, be prepared to do the normal test, commit, refresh, restart, and push flow cleanly.

Definition of done:
- the chosen sprint improvements are implemented cleanly
- tests were added or updated where needed
- validation was actually run
- docs were updated if behavior changed
- the final summary clearly states:
  - what Andrea/OpenClaw handled
  - what Cursor handled
  - what changed
  - what tests passed
  - any remaining risks or next best steps

Begin now with triage, role split, and the first visible collaboration update.
```
