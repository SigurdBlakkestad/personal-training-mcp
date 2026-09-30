# personal-training-mcp

Personal training data pipeline ingesting Strava, Withings, and Garmin into Postgres (Supabase). Exposes coaching tools to Claude via a custom MCP server. Mirrors readable views to Notion and the iPhone calendar.

## Stack

Python 3.12, Postgres on Supabase, GitHub Actions for scheduling, MCP via FastMCP, structlog for logging.

## Commands

- install: `pip install -e '.[dev]'`
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

- Garmin: fragile dependency; `sync_garmin.yml` fails loud (red run) when the token dies — it's its own workflow, so a red run never blocks the other syncs. The live token is the `garmin` row in `service_credentials`, not `GARMINTOKENS_B64`: Garmin's DI flow issues a new refresh token on every refresh and invalidates the previous one, so each run writes back what it ends up holding and the secret is only the seed. Recovery = re-run `scripts/garmin_auth.py`, update the secret, **and** delete the stored row — otherwise the rejected token keeps being replayed. `mobile+*` strategies are usually 429-rate-limited (account-scoped, hours-long window); login still succeeds via `widget+cffi`, which only persists tokens when `client.login(tokenstore=...)` gets a path (it also accepts inline JSON, which is how the stored row is passed). "Failed to retrieve social profile" is *not* diagnostic on its own — the library raises it for any failed `/userprofile-service/socialProfile` call, so read the chained cause: `API Error 401` = token rejected, whereas a stale `garminconnect` (floor pinned in `pyproject.toml`) fails earlier in the SSO flow.
- Withings: refresh tokens rotate on every refresh, same as Garmin. The live token is the `withings` row in `service_credentials`, saved in its own transaction right after the refresh so a later failure in the run can't lose it; `WITHINGS_REFRESH_TOKEN` is only the seed. Recovery = re-run `scripts/withings_auth.py` (it writes the fresh token to the row) and update the secret. Weigh-ins are fetched by `lastupdate` (when Withings received them), not measurement time, so late-synced weigh-ins are still picked up.
- Strava: API is subscriber-only as of mid-2026. Without an active Strava subscription the app is deactivated and every activity call returns `403` (`Application/Status/Inactive`); token refresh still 200s but data is blocked, and re-auth does not help. `sync_strava.yml` is disabled until/unless a subscription is active. Garmin is the primary activity source.

## Housekeeping

When creating files that don't need scanning every session (generated `.ics` files, downloaded raw payloads under `data/`, ad-hoc one-off scripts), add them to `.claudeignore` immediately so future context loads stay clean.

## Deeper docs

- `docs/SETUP_MANUAL.md` — manual setup steps for forkers (accounts, OAuth, secrets, iPhone calendar)
- `OPERATIONS.md` — local development, daily rhythm, secret rotation, failure recovery
