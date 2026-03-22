---
name: cursor_handoff
description: Handoff large coding and repository tasks to Cursor
metadata:
  openclaw:
    os:
      - darwin
    requires:
      bins:
        - python3
---

# Cursor Handoff

## Purpose

Delegate repository-heavy coding work to Cursor Cloud Agents (preferred) or local Cursor CLI (fallback) while keeping handoffs explicit, auditable, and safe.

Use this skill when a request requires broad codebase context, multi-file edits, branch/PR workflows, large refactors, failing test investigation, or deep repo analysis.

## When To Use

- Add or refactor features across many files
- Diagnose and fix failing tests in a repository
- Create branch-based implementation work and PR-ready outputs
- Perform architecture or code review analysis over large repos
- Any request that is too code-heavy for direct in-chat execution

## When Not To Use

- Tiny one-off shell commands
- Simple factual/local questions
- Non-coding tasks
- Small single-file edits that OpenClaw can safely do directly

## Decision Rules

1. Prefer direct OpenClaw execution for small, local, low-risk tasks.
2. Use `cursor_handoff` for large, repo-aware tasks.
3. If the user asks for analysis/review/planning or intent is ambiguous, use read-only mode.
4. Only use edit mode when the user clearly asks for code changes.
5. Never infer permission for commits, PR creation, or destructive git actions unless explicitly requested.
6. When Andrea/OpenClaw marked the turn as `cursor_primary` or collaborative, involve Cursor before the final answer even if you can partially reason about the task yourself.

## Branch Guidance

- If user provides a branch name, use it.
- If no branch is provided, generate:
  - `openclaw/task-YYYYMMDD-HHMMSS`
- Keep branch names deterministic and readable.

## Prompt Construction Rules

Before handoff, convert the user request into a clean implementation prompt:

- Include objective, constraints, acceptance criteria, and expected outputs.
- Include repository context and any relevant paths.
- State whether task is read-only analysis or edit implementation.
- Ask Cursor to summarize changes/results concisely.
- Avoid leaking secrets or unrelated private context.

### Intent templates (recommended)

For repeatable quality, pass **`--intent`** with optional extra detail in **`--prompt`**:

| Intent | Use when |
|--------|----------|
| `code-review` | Read-only review, risks, test gaps |
| `refactor` | Safe cleanup with behavior preserved |
| `release-notes` | User-facing notes from recent work |
| `brief` | Product/creative brief structure |

Example (API mode, local clone):  
`python3 scripts/cursor_handoff.py --repo /path/to/clone --intent code-review --prompt "Focus on auth and payments" --read-only true --dry-run --json`

### Pre-handoff triage

With a **local** repo path, add **`--triage`** to prepend a short non-secret snapshot (git branch/status, top-level files, common project markers). Improves first-pass context without pasting secrets.

## Execution Workflow

1. Resolve repo path (or repository URL for API mode).
2. Resolve branch (user-specified or generated default).
3. Select mode:
   - `api` when Cursor API credentials are available
   - `cli` if local Cursor CLI exists
   - `auto` to prefer API and fallback to CLI
4. Run:
   - `python3 scripts/cursor_handoff.py --repo "<repo>" --prompt "<prompt>" --mode auto --read-only <true|false> --json`
   - Or with intent/triage: add `--intent code-review` and/or `--triage` (local `--repo` only).
5. Return compact chat summary in this order:
   - backend used
   - read-only vs edit
   - branch name
   - agent/job ID (if available)
   - status + URL (if available)
   - one next step

## Safety Guidance

- Default to read-only for ambiguous requests.
- Do not assume destructive actions are allowed.
- Never hardcode API keys or secrets in prompts.
- Keep shell usage quoted and path-safe.
- Report failures clearly with next actions.

## Examples

### Analysis Handoff (Read-Only)

User asks: "Review this repo and propose a refactor plan."

- Use read-only: `true`
- Output should be plan/findings only, no edits

### Implementation Handoff (Edit)

User asks: "Fix failing tests and push branch for PR."

- Use read-only: `false`
- Provide branch and ask Cursor to produce test summary + PR-ready result

## Related operator docs

- Hybrid OpenClaw expansion (Apple/Google productivity, execution lane, optional voice): repo `docs/ANDREA_OPENCLAW_HYBRID_SKILLS.md`
- Day-to-day gates: `docs/ANDREA_OPERATIONS_PLAYBOOK.md`
- **Lockstep bus** (Telegram/Alexa/Cursor shared task timeline): `docs/ANDREA_LOCKSTEP_ARCHITECTURE.md` — after spawning a Cursor job, report lifecycle with `scripts/andrea_sync_cursor_report.py` so all channels stay aligned.
- Telegram intent hints: `@Andrea` means keep it direct when possible, `@Cursor` means Cursor-first collaboration, and `@Andrea @Cursor` / `work together` / `double-check` means OpenClaw and Cursor should collaborate before the final answer.
