# Build step runner prompt

Paste this into Claude Code after `/clear` to execute one step of `BUILD_PLAN.md`. Change only the `STEP:` number at the top each time.

```
STEP: 1

You are executing one step of docs/BUILD_PLAN.md in this repo. Follow this exact procedure — do not skip parts.

0. Read these files in full before anything else:
   - CLAUDE.md
   - docs/BUILD_PLAN.md (just the section for STEP and the one immediately after)
   - docs/SETUP_MANUAL.md (only the sections referenced by STEP and STEP+1)

1. Confirm STEP is not already done:
   - Run `git log --oneline -20` and check whether STEP's commit message (the one specified in its block) already exists.
   - If it does, STOP and tell me "Step N is already committed as <sha>. Did you mean step N+1?" — do not re-run.

2. Verify STEP's manual prerequisites from SETUP_MANUAL.md are satisfied:
   - List every prerequisite SETUP_MANUAL.md ties to this step (e.g., "Strava section before Step 5", "Render before Step 9").
   - For each, check what you can: env vars present in .env, GitHub Secrets present via `gh secret list`, files on disk, extensions enabled (where checkable).
   - If anything is missing, STOP. Print a checklist of what I need to do manually, with exact section references. Do not start implementing.

3. Execute STEP exactly as written in BUILD_PLAN.md:
   - Spawn an Explore sub-agent first to summarize current state of the files/dirs the step will touch — edge cases, fallbacks, non-happy paths, not just the happy path. Use only the sub-agent's summary in main context for implementation.
   - Implement everything the step lists. Don't add, don't skip, don't refactor unrelated code.
   - Run, in order: `ruff check . && ruff format --check .`, `mypy src/`, `pytest`. All three must pass. If anything fails, fix and re-run — do not commit on red.
   - For steps that touch the database (3, 8, 9, 10), run the relevant `alembic upgrade head` against Supabase and confirm it succeeded.
   - Commit with the exact message specified in the step block. Use the Conventional Commits format already shown there.

4. Final report — print exactly this structure:

   ## Step N complete
   - Commit: <sha> <subject>
   - Files changed: <count>
   - Tests: <passed/total>

   ## Manual actions needed before Step N+1
   - [list anything from SETUP_MANUAL.md for step N+1, with section refs]
   - [list any "manual verification" or "after merge" actions from step N's block that I have to do myself — e.g., add GitHub Secrets, run an OAuth helper script, click something in a dashboard]
   - If none: "None. You can run Step N+1 immediately."

   ## Next prompt
   Tell me to `/clear` and paste this same prompt with `STEP: N+1`.

Rules:
- No print() in src/. No bare except. structlog with context for all errors.
- Don't add Strava/Withings/Notion/Garmin env vars or secrets unless this specific step says to.
- If you hit ambiguity, ask one focused question and stop. Don't guess.
```
