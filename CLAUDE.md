# training-pipeline

Personal training data platform. Ingests Strava, Withings, and Garmin into Postgres on Supabase. Exposes coaching tools to Claude via a custom MCP server. Mirrors readable views to Notion and the iPhone calendar.

## Status

In progress. See `BUILD_PLAN.md` for the staged build sequence. This `CLAUDE.md` is a **starter** — Step 2 of the build plan regenerates it once the scaffold exists.

## Stack

Python 3.12 · Postgres (Supabase) · GitHub Actions · MCP (FastMCP) · structlog

## Commands

These commands exist after Step 1. Until then, only the project root and these doc files are present.

- install: `pip install -r requirements.txt`
- test: `pytest`
- lint: `ruff check . && ruff format --check .`
- typecheck: `mypy src/`
- migrate: `alembic upgrade head`

## Pre-commit

Ruff, mypy, and pytest must pass before any commit. Fail loud.

## Conventions

- snake_case for DB columns
- All errors go through structlog with context (no bare exceptions, no print)
- Every ingestor is idempotent on (source, source_id)
- All external HTTP calls are wrapped in tenacity retry with exponential backoff (only 5xx and connection errors retried, never 4xx)
- No `print()` anywhere in src/
- Tests are unit tests with mocked HTTP and DB sessions — no real network calls in CI

## Architecture landmarks

Search anchors for navigation. These exist after the relevant build step.

- Entry CLI: `src/training_pipeline/cli.py`
- Shared infra: `src/training_pipeline/shared/`
- Ingestors (one per source): `src/training_pipeline/ingestors/`
- Derived metrics: `src/training_pipeline/derived/`
- MCP server: `src/training_pipeline/mcp_server/`
- Notion mirror: `src/training_pipeline/notion_sync/`
- Calendar publisher: `src/training_pipeline/calendar_publish/`
- Schema migrations: `alembic/versions/`
- Tests: `tests/`
- One-off scripts (NOT in src/): `scripts/`
- GitHub Actions workflows: `.github/workflows/`

## Existing systems

Grows as we build. Currently: none — only project docs.

## Build plan execution rule

Every step in `BUILD_PLAN.md` is executed as follows:

1. Spawn a sub-agent to investigate the current state of the files being touched. The sub-agent flags edge cases, fallbacks, conditional branches, and non-happy paths — not just the happy path.
2. Implement using only the sub-agent's summary in main context.
3. Run lint + typecheck + tests before finishing.
4. Commit with the message specified in the build step.

## Housekeeping

When creating files that don't need scanning every session (generated `.ics` files, downloaded raw payloads under `data/`, ad-hoc one-off scripts), add them to `.claudeignore` immediately so future context loads stay clean.

## Deeper docs

- `BUILD_PLAN.md` — full build sequence with paste-ready Claude Code prompts
- `SETUP_MANUAL.md` — manual setup steps (accounts, OAuth, secrets, iPhone calendar)
- `OPERATIONS.md` — running the system, secret rotation, recovery from common failures
