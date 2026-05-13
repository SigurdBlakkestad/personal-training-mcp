# personal-training-mcp

Personal training data pipeline ingesting Strava, Withings, and Garmin into Postgres (Supabase). Exposes coaching tools to Claude via a custom MCP server. Mirrors readable views to Notion and the iPhone calendar.

## Stack

Python 3.12, Postgres on Supabase, GitHub Actions for scheduling, MCP via FastMCP, structlog for logging.

## Commands

- install: `pip install -r requirements.txt`
- test: `pytest`
- lint: `ruff check . && ruff format --check .`
- typecheck: `mypy src/`
- migrate: `alembic upgrade head`

## Pre-commit

Ruff, mypy, and pytest must all pass before any commit. Fail loud — no skipping checks.

## Conventions

- snake_case for DB columns
- All errors go through structlog with context; no bare `except`, no `print()` in `src/`
- Every ingestor is idempotent on (source, source_id)
- All external HTTP calls are wrapped in tenacity retry with exponential backoff; retry only on 5xx and connection errors, never on 4xx
- Tests are unit tests with mocked HTTP and DB sessions; no real network calls in CI
- Conventional Commits, subject ≤72 chars; ≤2 files changed = subject only; ≥3 files changed = subject + 2–5 bullets stating what changed

## Architecture landmarks

Search anchors for navigation. Each path is populated by the build step that introduces it.

- `src/training_pipeline/shared/` — config, db session, structlog setup
- `src/training_pipeline/ingestors/` — one module per source (strava, withings, garmin) on a shared base
- `src/training_pipeline/derived/` — TSS, CTL/ATL/TSB, weekly load, weight trend
- `src/training_pipeline/mcp_server/` — FastMCP tools exposed to Claude
- `alembic/versions/` — schema migrations
- `tests/` — unit tests mirroring the `src/` layout

## Existing systems

None yet. This section grows as build steps land.

## Build plan execution rule

Every step in `docs/BUILD_PLAN.md` is executed as follows:

1. Spawn a sub-agent first to investigate the current state of the files being touched. The sub-agent must flag edge cases, fallbacks, conditional branches, and non-happy paths — not just the happy path.
2. Implement in main context using only the sub-agent's summary.
3. Run lint, typecheck, and tests before finishing the step. All three must pass.
4. Commit using the exact message specified in the build step.

## Housekeeping

When creating files that don't need scanning every session (generated `.ics` files, downloaded raw payloads under `data/`, ad-hoc one-off scripts), add them to `.claudeignore` immediately so future context loads stay clean.

## Deeper docs

- `docs/BUILD_PLAN.md` — full build sequence with paste-ready Claude Code prompts
- `docs/SETUP_MANUAL.md` — manual setup steps (accounts, OAuth, secrets, iPhone calendar)
