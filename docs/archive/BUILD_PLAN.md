# Training Pipeline — Build Plan

> **Status: archived 2026-05-15.** All 12 steps are committed; the code is in the repo. If you've forked this project and want to *run* it, see `docs/SETUP_MANUAL.md` instead — you don't need to re-execute these steps. This file is kept as a reference for understanding how the project was built, and as a template if you want to extend it (e.g., add a fourth data source) by following the same step pattern.

Staged build plan with paste-ready Claude Code prompts. Each step is independent; `/clear` between steps when noted. Garmin (the fragile dependency) is **last** — the other sources will be working before you touch it.

**Stack:** Python 3.12 · Supabase Postgres · GitHub Actions · MCP Python SDK (FastMCP) · Notion API · Strava API · Withings API · `python-garminconnect` (final phase)

**Companion docs:**
- `SETUP_MANUAL.md` — everything you do by hand (accounts, OAuth, Supabase, Notion, iPhone calendar)
- `CLAUDE.md` — generated in Step 2

**Phases:**
1. Foundation (Steps 1–3)
2. Strava ingestion (Steps 4–5)
3. Withings ingestion (Steps 6–7)
4. Derived metrics (Step 8)
5. MCP server for Claude (Step 9)
6. Notion mirror (Step 10)
7. iPhone calendar (Step 11)
8. **Garmin (Step 12 — run after the rest is stable)**

---

## Before Step 1

You must have completed every section of `SETUP_MANUAL.md` up to and including **Supabase**. Specifically:
- A public GitHub repo named `personal-training-mcp` pushed to your account
- A Supabase project with the `DATABASE_URL` connection string
- Python 3.12 installed locally
- Claude Code installed and authenticated

Strava / Withings / Notion / Garmin credentials are added at the steps that need them — not now.

---

## Step 1 — Project scaffold

`/clear` before this step.

```
Step 1 of 12. Upcoming: step 2 generates CLAUDE.md, step 3 sets up Postgres schema with Alembic.

We are building a personal training data pipeline. Stack: Python 3.12, Postgres on Supabase, GitHub Actions for scheduling, MCP Python SDK (FastMCP) for Claude integration.

Use a sub-agent to verify the current repo state (likely just README.md + .git). Then create:

- pyproject.toml: project metadata, requires-python = ">=3.12", dev deps (pytest, pytest-asyncio, ruff, mypy)
- requirements.txt: runtime deps psycopg[binary]>=3.2, sqlalchemy>=2.0, alembic>=1.13, pydantic>=2.7, pydantic-settings>=2.5, httpx>=0.27, python-dotenv>=1.0, structlog>=24.4, tenacity>=9.0
- src/training_pipeline/__init__.py (empty)
- src/training_pipeline/shared/__init__.py
- src/training_pipeline/shared/config.py: pydantic-settings Settings class. Fields for now: DATABASE_URL, LOG_LEVEL. Other source credentials are added per step. Settings reads .env in dev, env vars in CI.
- src/training_pipeline/shared/db.py: SQLAlchemy engine (psycopg driver) and session factory. Read DATABASE_URL from Settings. Expose a contextmanager get_session().
- src/training_pipeline/shared/logging.py: structlog setup; JSON renderer when CI env detected, pretty renderer locally.
- .env.example with DATABASE_URL placeholder and LOG_LEVEL=INFO, with brief comments
- .gitignore: Python standard plus .env, .env.local, config/local/, *.ics, data/, .DS_Store, .venv/, htmlcov/, .ruff_cache/, .mypy_cache/, .pytest_cache/
- .claudeignore: node_modules/, .venv/, __pycache__/, *.egg-info/, .mypy_cache/, .pytest_cache/, .ruff_cache/, data/, *.ics, htmlcov/, build/, dist/
- README.md (replace stub): one paragraph description, pointer to BUILD_PLAN.md and SETUP_MANUAL.md, "Status: in progress, see BUILD_PLAN.md"
- tests/__init__.py
- tests/test_smoke.py: one test that imports training_pipeline and asserts True

Run `python -m pip install -r requirements.txt` then `pytest`. Confirm the smoke test passes. Commit with message "feat: project scaffold".

Do NOT add Strava, Withings, Garmin, or Notion env vars yet — those land in their own steps.
```

---

## Step 2 — Generate CLAUDE.md

`/clear` before this step.

```
Step 2 of 12. Upcoming: step 3 creates Postgres schema with Alembic.

Use a sub-agent to read pyproject.toml, requirements.txt, .env.example, and the src/ tree.

Generate a CLAUDE.md under 100 lines for this project, following these rules strictly:
- No markdown tables anywhere
- No line numbers anywhere (lines drift)
- Architecture landmarks section uses file paths as search anchors only
- Plain lists, not nested complexity
- Include the build plan execution rule
- Include the housekeeping rule

Sections in order:
1. One-line project description
2. Stack: Python 3.12, Postgres on Supabase, GitHub Actions, MCP via FastMCP, structlog
3. Commands: dev install (`pip install -r requirements.txt`), test (`pytest`), lint (`ruff check . && ruff format --check .`), typecheck (`mypy src/`), migrate (`alembic upgrade head`)
4. Pre-commit: ruff + mypy + pytest must pass before any commit. Fail loud.
5. Conventions: snake_case for DB columns, all errors go through structlog with context, every ingestor must be idempotent on source IDs, no print() statements anywhere, all external HTTP calls wrapped in tenacity retry with exponential backoff
6. Architecture landmarks: src/training_pipeline/shared/, src/training_pipeline/ingestors/ (one module per source), src/training_pipeline/derived/, src/training_pipeline/mcp_server/, sql/migrations/, tests/
7. Existing systems: (empty list for now; this section grows as we build)
8. Build plan execution rule: every build step uses a sub-agent for investigation first (flag edge cases, fallbacks, non-happy paths), then implements in main context using only the summary. Run lint + typecheck + tests before finishing any step.
9. Housekeeping: when creating files that don't need scanning every session (generated .ics files, downloaded raw payloads under data/, ad-hoc scripts), add them to .claudeignore.
10. Deeper docs: BUILD_PLAN.md, SETUP_MANUAL.md

Commit with message "docs: add CLAUDE.md".
```

---

## Step 3 — Postgres schema and Alembic migrations

`/clear` before this step. Have your Supabase `DATABASE_URL` in `.env`.

```
Step 3 of 12. Upcoming: step 4 builds shared ingestor base, step 5 adds Strava.

Read CLAUDE.md first.

Use a sub-agent to confirm no existing Alembic config in the repo.

Goal: set up Alembic and create the full schema in one migration. The schema must accommodate Strava (step 5), Withings (step 7), and Garmin (step 12) without future ALTERs — design source-agnostic columns now with JSONB for source-specific extras.

Tasks:
1. `pip install alembic` then `alembic init alembic` at repo root. Configure alembic.ini and alembic/env.py to read DATABASE_URL from training_pipeline.shared.config.Settings.
2. Create SQLAlchemy models in src/training_pipeline/shared/models.py for the tables below.
3. Generate the initial migration with `alembic revision --autogenerate -m "init schema"`.
4. Inspect the generated migration; add indexes explicitly.
5. Run `alembic upgrade head` against Supabase. Verify tables exist.

Tables:

activities — canonical workout table, source-agnostic.
- id uuid pk default gen_random_uuid()
- source text not null (values: 'strava' | 'withings' | 'garmin' | 'manual')
- source_id text not null
- start_time timestamptz not null
- end_time timestamptz
- sport_type text (normalized: 'cycling' | 'running' | 'swimming' | 'lifting' | 'walking' | 'other')
- name text
- duration_seconds integer
- distance_meters real
- elevation_gain_meters real
- avg_hr smallint
- max_hr smallint
- avg_power smallint
- normalized_power smallint
- calories integer
- avg_cadence smallint
- training_load real (TSS or TRIMP, computed in step 8)
- raw jsonb not null (full source payload)
- ingested_at timestamptz default now()
- updated_at timestamptz default now()
- Unique (source, source_id)
- Indexes: (start_time desc), (sport_type, start_time desc), (source, start_time desc)

body_measurements
- id uuid pk
- source text not null
- measured_at timestamptz not null
- weight_kg real
- body_fat_pct real
- muscle_mass_kg real
- bone_mass_kg real
- water_pct real
- visceral_fat real
- bmi real
- raw jsonb not null
- ingested_at timestamptz default now()
- Index (measured_at desc)

daily_summary — one row per (date, source)
- id uuid pk
- date date not null
- source text not null
- sleep_score smallint
- sleep_duration_seconds integer
- resting_hr smallint
- hrv_ms real
- stress_avg smallint
- body_battery_high smallint
- body_battery_low smallint
- steps integer
- raw jsonb not null
- ingested_at timestamptz default now()
- Unique (date, source). Index (date desc)

manual_logs
- id uuid pk
- logged_at timestamptz not null
- activity_id uuid fk activities.id nullable
- rpe smallint
- pain_score smallint
- notes text
- tags text[]
- created_at timestamptz default now()
- Index (logged_at desc), index on activity_id

weekly_plans
- id uuid pk
- week_of date not null (Monday of the week)
- version integer not null default 1
- plan jsonb not null (array of {date, session_type, description, duration_min, intensity, notes, time})
- notes text
- is_current boolean not null default true
- created_at timestamptz default now()
- Unique (week_of, version). Index (week_of desc)

ingestion_runs
- id uuid pk
- source text not null
- started_at timestamptz not null
- finished_at timestamptz
- status text (values: 'running' | 'success' | 'partial' | 'failed')
- records_processed integer default 0
- records_inserted integer default 0
- records_updated integer default 0
- error text
- cursor text (last sync cursor; per-source format, e.g. JSON for rotated refresh tokens)
- Index (source, started_at desc)

Add `sql/seed.sql` as an empty placeholder.

Run lint, typecheck, tests. Commit with message "feat: initial schema with all source tables".
```

---

## Step 4 — Shared ingestor base

`/clear` before this step.

```
Step 4 of 12. Upcoming: step 5 uses this base to build the Strava ingestor; steps 7 and 12 reuse it for Withings and Garmin.

Read CLAUDE.md first. Use a sub-agent to summarize the current shared/ tree and the activities + ingestion_runs models.

Build src/training_pipeline/ingestors/base.py:
- Abstract base class IngestorBase:
  - name property (override per source)
  - source_key property ('strava' | 'withings' | 'garmin')
  - run(since: datetime | None = None) → IngestionResult method that wraps everything
  - run() creates an ingestion_runs row at start, updates it at end with counts + status
  - Catch all exceptions, log with structlog including source + ingestion_run_id context, set status='failed', re-raise
- IngestionResult dataclass: records_processed, records_inserted, records_updated, cursor
- Helper upsert_activity(session, activity_dict) → INSERT ... ON CONFLICT (source, source_id) DO UPDATE. Returns 'inserted' | 'updated'.
- Helper upsert_body_measurement(session, measurement_dict) — body_measurements has no perfect natural key; dedupe by (source, measured_at, weight_kg) for now; add TODO comment for revisit.
- Helper upsert_daily_summary(session, summary_dict) — conflict on (date, source).

Build src/training_pipeline/ingestors/http.py:
- HttpClient wrapping httpx.Client with tenacity retries (3 attempts, exponential backoff, only on 5xx and connection errors — never on 4xx).
- Logs every request: method, url path only (no query string secrets), status, duration_ms.

Tests:
- tests/ingestors/test_base.py: a FakeIngestor subclass returning canned data; verify ingestion_runs rows are created and finalized, upsert helpers called correctly, exceptions surface and mark status='failed'.
- tests/ingestors/test_http.py: mock httpx, verify retry on 503 then 200 success, no retry on 401.

Run lint, typecheck, tests. Commit with message "feat: shared ingestor base with idempotency and retries".
```

---

## Step 5 — Strava ingestor and workflow

`/clear` before this step. Complete the **Strava** section of `SETUP_MANUAL.md` first — you need `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN` in your `.env` and in GitHub Secrets.

```
Step 5 of 12. Upcoming: step 6 sets up Withings OAuth helper, step 7 builds the Withings ingestor.

Read CLAUDE.md. Use a sub-agent to summarize ingestors/base.py and ingestors/http.py.

Add Strava env vars to shared/config.py: STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN. Update .env.example.

Build src/training_pipeline/ingestors/strava.py:
- StravaIngestor extends IngestorBase, source_key='strava'.
- Token management: at the start of each run, exchange the refresh token for a fresh access token via POST https://www.strava.com/oauth/token. Strava rotates refresh tokens — capture the new refresh_token from the response and write it to ingestion_runs.cursor as JSON. Always log a WARN if Strava issued a new refresh_token so the operator knows to update GitHub Secrets.
- Fetch activities since the last successful ingestion_runs.finished_at, or last 30 days if no prior run. Use GET /api/v3/athlete/activities with `after` epoch timestamp. Paginate with `page` + `per_page=200` until empty.
- For each activity, map Strava JSON to the activities table shape. Store the full payload in `raw`. Use Strava activity id (as string) for source_id.
- Sport type normalization: map Strava's `sport_type` to canonical set ('cycling', 'running', 'swimming', 'lifting' (from "WeightTraining"), 'walking', 'other'). Keep Strava's raw value in raw.
- Honor rate limits: parse X-RateLimit-Usage; if 15-min usage > 90% of limit, pause for the remaining window. In-memory tracking only — we're well under limits for one user.

Tests/ingestors/test_strava.py with httpx mocking: refresh token flow, pagination, sport_type normalization, refresh token rotation captured in cursor, rate-limit pause behavior.

Add a CLI entrypoint src/training_pipeline/cli.py with `python -m training_pipeline.cli sync --source strava`. Use argparse — no extra deps.

Add .github/workflows/sync_strava.yml:
- Trigger: schedule cron `0 */2 * * *` (every 2 hours) and workflow_dispatch
- Python 3.12, pip install -r requirements.txt
- Env from secrets: DATABASE_URL, STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN
- Step: `python -m training_pipeline.cli sync --source strava`
- On failure: default behavior (logs visible in Actions UI). No notifications.

Run lint, typecheck, tests. Commit with message "feat: Strava ingestor with token rotation and CI workflow".

Manual verification (after merge):
1. Add STRAVA_* secrets in GitHub repo settings → Secrets and variables → Actions
2. Trigger sync_strava.yml manually via Actions tab
3. Confirm rows appear in Supabase activities table
```

---

## Step 6 — Withings OAuth helper

`/clear` before this step. Complete the **Withings developer account** section of `SETUP_MANUAL.md` first — you need `WITHINGS_CLIENT_ID` and `WITHINGS_CLIENT_SECRET`. You do **not** yet have access/refresh tokens; this step generates them.

```
Step 6 of 12. Upcoming: step 7 builds the Withings ingestor using the tokens this step generates.

Read CLAUDE.md.

Withings uses OAuth 2.0 web flow. First auth requires the user to visit an authorization URL in a browser. We'll build a one-shot local helper script that:
1. Starts a tiny local HTTP server on http://localhost:8765/callback
2. Prints the Withings authorization URL to the terminal for the user to open
3. Captures the `code` query param when Withings redirects back
4. Exchanges the code for access + refresh tokens
5. Prints them with copy-paste-ready format for GitHub Secrets

Build scripts/withings_auth.py (NOT in src/ — one-time tool):
- Use stdlib http.server only; no Flask
- Scopes: user.activity, user.metrics, user.info
- Redirect URI: http://localhost:8765/callback (must match what's registered in Withings developer portal)
- Authorize URL: https://account.withings.com/oauth2_user/authorize2 with params response_type=code, client_id, state (random), scope, redirect_uri
- Token exchange: POST https://wbsapi.withings.net/v2/oauth2 with body action=requesttoken, grant_type=authorization_code, client_id, client_secret, code, redirect_uri
- Print: WITHINGS_ACCESS_TOKEN, WITHINGS_REFRESH_TOKEN, WITHINGS_USERID (Withings returns userid in the token response — store it for future API calls)

Add scripts/README.md explaining one-time auth: `python scripts/withings_auth.py`, follow prompts, paste tokens into .env and add as GitHub Secrets.

Add Withings env vars to shared/config.py: WITHINGS_CLIENT_ID, WITHINGS_CLIENT_SECRET, WITHINGS_ACCESS_TOKEN, WITHINGS_REFRESH_TOKEN, WITHINGS_USERID. Update .env.example.

No new automated tests (one-shot tool with manual browser step). But add a structural test that the script can be imported and the URL builder produces valid query strings.

Run lint, typecheck. Commit with message "feat: Withings OAuth bootstrap helper".

Action for me after merge: run `python scripts/withings_auth.py`, complete the browser flow, paste tokens into .env, add as GitHub Secrets.
```

---

## Step 7 — Withings ingestor and workflow

`/clear` before this step. You need valid Withings tokens in `.env` and GitHub Secrets after running Step 6's helper.

```
Step 7 of 12. Upcoming: step 8 builds derived metrics across all ingested sources.

Read CLAUDE.md. Use a sub-agent to summarize ingestors/base.py and ingestors/strava.py.

Build src/training_pipeline/ingestors/withings.py:
- WithingsIngestor extends IngestorBase, source_key='withings'.
- Token management: on 401 from any endpoint, POST https://wbsapi.withings.net/v2/oauth2 with action=requesttoken, grant_type=refresh_token to get new access_token + refresh_token. Withings returns both. Store new refresh_token + userid in ingestion_runs.cursor as JSON. Log a WARN so the operator knows to update GitHub Secrets.
- Withings response wrapper: all responses are JSON with `status` (0 = success, non-zero = error) and `body`. Wrap in a helper that raises on non-zero status with the message from `error`.
- Fetch body measurements: POST https://wbsapi.withings.net/measure with action=getmeas, startdate (epoch since last run or 30 days ago), enddate (now). Convert measure values using value * 10^unit. Type map: 1=weight kg, 5=fat-free mass kg, 6=fat ratio %, 8=fat mass kg, 76=muscle mass kg, 77=hydration %, 88=bone mass kg.
- Fetch daily activity: POST https://wbsapi.withings.net/v2/measure with action=getactivity, startdateymd, enddateymd. Map to daily_summary.steps.
- Fetch sleep summary: POST https://wbsapi.withings.net/v2/sleep with action=getsummary, startdateymd, enddateymd. Map sleep_score (sleep_score field on the row), total sleep duration, wakeupduration etc., merging into daily_summary by date.

Tests/ingestors/test_withings.py with httpx mocking: refresh on 401, measurement type mapping, sleep + activity merge into daily_summary.

Add CLI route `sync --source withings`.

Add .github/workflows/sync_withings.yml:
- Schedule cron `30 5 * * *` (daily 05:30 UTC)
- Same Python setup as Strava
- Env: WITHINGS_CLIENT_ID, WITHINGS_CLIENT_SECRET, WITHINGS_ACCESS_TOKEN, WITHINGS_REFRESH_TOKEN, WITHINGS_USERID, DATABASE_URL
- Run sync command

Run lint, typecheck, tests. Commit with message "feat: Withings ingestor with body, activity, and sleep".

Manual verification: add WITHINGS_* secrets in GitHub, trigger workflow manually, confirm body_measurements + daily_summary rows in Supabase.
```

---

## Step 8 — Derived metrics

`/clear` before this step.

```
Step 8 of 12. Upcoming: step 9 builds the MCP server that exposes both raw and derived data to Claude.

Read CLAUDE.md. Use a sub-agent to summarize schema (activities, daily_summary, body_measurements).

Build src/training_pipeline/derived/:
- __init__.py
- training_load.py: compute TSS per activity. With normalized_power + FTP: TSS = (duration_sec * NP * IF) / (FTP * 3600) * 100, where IF = NP/FTP. Without power: HR-based TRIMP (Banister) using sex-specific b coefficient (1.92 for male). Athlete FTP comes from env ATHLETE_FTP (add to config.py). Store result in activities.training_load.
- ctl_atl_tsb.py: compute CTL (42-day EWMA of training_load), ATL (7-day EWMA), TSB = CTL - ATL. Output is daily series.
- weekly_load.py: aggregate activities into weekly volume per sport_type (duration, distance, count, total_load) by ISO week starting Monday.
- weight_trend.py: 7-day and 28-day moving averages of weight_kg from body_measurements.

Add a derived_metrics table. New migration `alembic revision -m "add derived_metrics"`:
- id uuid pk
- date date not null
- metric_name text not null
- value real not null
- computed_at timestamptz default now()
- Unique (date, metric_name). Index (metric_name, date desc)
- Metric names: ctl, atl, tsb, weight_7d_avg, weight_28d_avg, weekly_load_cycling, weekly_load_running, weekly_load_lifting, weekly_load_total

Build src/training_pipeline/derived/compute.py with entrypoint `recompute_all(session, since: date | None = None)` running all calculations and upserting derived_metrics.

Tests/derived/ — unit tests per function with fixture data (lists of dicts, no DB).

Add CLI command `python -m training_pipeline.cli compute-derived`.

Add .github/workflows/compute_derived.yml:
- Schedule cron `0 6 * * *` (daily 06:00 UTC, after Strava + Withings)
- Env: DATABASE_URL, ATHLETE_FTP
- Run compute command

Add ATHLETE_FTP env var (default 200 for early rebuild). Update .env.example.

Run lint, typecheck, tests. Commit with message "feat: derived metrics (TSS, CTL/ATL/TSB, weekly load, weight trend)".
```

---

## Step 9 — MCP server for Claude

`/clear` before this step. Complete the **Render** section of `SETUP_MANUAL.md` first.

```
Step 9 of 12. Upcoming: step 10 builds the Notion mirror, step 11 generates the iPhone calendar.

Read CLAUDE.md. Use a sub-agent to summarize all data available: activities, body_measurements, daily_summary, manual_logs, weekly_plans, derived_metrics.

We're building a custom MCP server giving Claude clean, coaching-shaped access. Two paths for Claude:
- READ tools: pull data for analysis
- WRITE tools: log subjective info, save a weekly plan

Install: `pip install mcp[cli]>=1.0 fastmcp>=2.0 fastapi>=0.115 uvicorn[standard]>=0.32`. Verify latest versions during the step.

Build src/training_pipeline/mcp_server/server.py exposing tools.

READ tools:
- get_recent_activities(days=14, sport_type=None) → list of activities (start_time, sport_type, name, duration_min, distance_km, avg_hr, training_load, rpe from manual_logs LEFT JOIN, pain from manual_logs LEFT JOIN). Cap 100 results.
- get_activity_by_id(activity_id: str) → full activity with linked manual_log.
- get_daily_summary(start_date, end_date) → daily rows joining body_measurements (weight, body fat) + daily_summary (sleep, RHR, HRV, body battery, steps).
- get_training_load_trend(weeks=8) → daily {date, ctl, atl, tsb} for the period.
- get_weekly_load(weeks=8) → per-week {week_of, cycling_hours, running_hours, lifting_hours, total_load}.
- get_weight_trend(weeks=12) → daily weight + 7-day + 28-day moving averages.
- get_current_plan() → latest weekly_plans row where is_current = true.
- search_sessions(filters: dict) → flexible search. Supported filters: date_from, date_to, sport_type, min_rpe, max_rpe, min_duration_min, has_pain. Returns matching activities.
- readiness_today() → composite: latest weight delta vs 7-day avg, last night's sleep_score, latest HRV, latest body battery low, recent RPE trend, current TSB. Plain dict; Claude composes narrative.

WRITE tools:
- log_session(activity_id=None, rpe, pain_score, notes, tags=[]) → writes to manual_logs. If activity_id provided, links it; else logs_at=now() and unlinked.
- save_weekly_plan(week_of, plan: list[dict], notes="") → marks previous plans for that week is_current=false, inserts new version with is_current=true.
- update_athlete_context(updates: dict) → writes to a new athlete_context table (single row upsert). Fields: ftp_watts, max_hr, body_weight_kg, current_phase, notes, updated_at. Used by Claude to maintain an evolving athlete profile.

Add migration for athlete_context table.

Each tool: input validation via pydantic, structured logging of every call with arguments + result counts, JSON-serializable returns only.

Hosting wrapper at src/training_pipeline/mcp_server/app.py: FastAPI app exposing MCP server over SSE at /mcp endpoint + /health endpoint returning {"status":"ok"}.

Add render.yaml at repo root configuring a free web service: python 3.12, build `pip install -r requirements.txt`, start `uvicorn training_pipeline.mcp_server.app:app --host 0.0.0.0 --port $PORT`, env vars referenced from Render dashboard.

Tests/mcp_server/test_tools.py mocking DB session, verifying inputs/outputs for all read + write tools.

Add CLI `python -m training_pipeline.cli serve-mcp` to run FastAPI locally on port 8000.

Run lint, typecheck, tests. Commit with message "feat: MCP server with read and write coaching tools".

Manual verification:
1. Deploy to Render (push to main triggers it if connected)
2. Note the public URL: https://<your-service>.onrender.com/mcp
3. In Claude.ai → Settings → Connectors → Add custom MCP server, paste the URL
4. Test in your Project: "What activities do I have this week?"
```

---

## Step 10 — Notion mirror

`/clear` before this step. Complete the **Notion** section of `SETUP_MANUAL.md` first.

```
Step 10 of 12. Upcoming: step 11 builds the iPhone calendar.

Read CLAUDE.md. Use a sub-agent to summarize data we want mirrored to Notion (activities for the journal, weekly_plans for the readable plan, derived_metrics for the dashboard).

Add Notion env vars to shared/config.py: NOTION_TOKEN, NOTION_DB_ACTIVITIES_ID, NOTION_DB_PLAN_ID, NOTION_DB_METRICS_ID. Update .env.example.

Install: `pip install notion-client>=2.2`.

Build src/training_pipeline/notion_sync/:
- __init__.py
- client.py: wrapper around notion_client.Client with retry on 429 (~3 req/sec limit) + structured logging
- activities_mirror.py: fetch activities + linked manual_logs from last 30 days, upsert into Notion Activities DB. Store the Notion page_id back into a new column activities.notion_page_id (add migration). Properties written: Date, Sport, Name, Duration, Distance, Avg HR, Training Load, RPE, Pain, Notes.
- plan_mirror.py: fetch current weekly_plans row, render each session as a Notion page in the Plan DB. Properties: Date, Session Type, Description, Duration, Intensity, Status (Planned/Done/Skipped). On rerun, delete previous "Planned" rows for that week and replace.
- metrics_mirror.py: fetch latest derived_metrics + update a single Notion "Dashboard" page with weekly load, current TSB, weight trend. Block-update approach, not row inserts.

Add migration: ALTER TABLE activities ADD COLUMN notion_page_id text.

Add CLI `python -m training_pipeline.cli notion-mirror`.

Add .github/workflows/notion_mirror.yml:
- Schedule cron `30 6 * * *` (daily 06:30 UTC, after derived metrics)
- Env: DATABASE_URL, NOTION_TOKEN, NOTION_DB_*_ID
- Run mirror command

Tests with mocked notion_client: verify upserts, dedupe via notion_page_id, error handling.

Run lint, typecheck, tests. Commit with message "feat: Notion mirror for activities, weekly plan, and dashboard".

Manual verification: add NOTION_* secrets in GitHub, trigger workflow, confirm pages in Notion DBs.
```

---

## Step 11 — iPhone calendar via .ics

`/clear` before this step. Complete the **GitHub Pages** section of `SETUP_MANUAL.md` first.

```
Step 11 of 12. Upcoming: step 12 (final) adds Garmin — the fragile dependency, last on purpose.

Read CLAUDE.md.

Goal: when Claude saves a weekly plan, an .ics file regenerates and lands at a public URL. iPhone subscribes once and pulls updates automatically.

Install: `pip install ics>=0.7`.

Build src/training_pipeline/calendar_publish/:
- __init__.py
- ics_builder.py: take current weekly_plans + next 3 weeks if available, produce an .ics calendar with one VEVENT per session.
  - UID = stable hash of (week_of, date, session_type)
  - DTSTART = morning of session date (8 AM default; override per-session if "time" key present)
  - DURATION from session.duration_min
  - SUMMARY = session_type
  - DESCRIPTION = full session description + intensity + notes
- publisher.py: write the .ics to `docs/training.ics`. GitHub Pages serves /docs as site root, so the public URL is https://<username>.github.io/personal-training-mcp/training.ics. Commit + push only if content changed (hash check).

Add .github/workflows/publish_calendar.yml:
- Triggers: workflow_run on notion_mirror.yml completion (success), workflow_dispatch, and push to main affecting weekly_plans logic
- Permissions: contents: write
- Use github-actions[bot] for commits
- Steps: checkout, python setup, regenerate ics, commit + push if changed

Add .claudeignore entry: `docs/training.ics`.

Tests: unit-test ics_builder with fixture plans, verify VEVENTs correct + UIDs stable across regenerations.

Run lint, typecheck, tests. Commit with message "feat: iPhone calendar via .ics generation and GitHub Pages".

Manual verification:
1. Confirm GitHub Pages is enabled for /docs folder in repo settings (done in SETUP_MANUAL.md)
2. Ask Claude in your Project: "Save this test plan: Monday 18:00 easy bike 45 min, Wednesday 06:00 upper push 60 min." Claude should call save_weekly_plan via MCP.
3. Trigger publish_calendar.yml manually
4. Confirm https://<username>.github.io/personal-training-mcp/training.ics returns the calendar
5. On iPhone: Settings → Calendar → Accounts → Add Account → Other → Add Subscribed Calendar → paste the URL → Save
6. Sessions appear in iOS Calendar
```

---

## Step 12 — Garmin (run last, after the rest is stable)

`/clear` before this step. **Run the rest of the pipeline for at least a week first** so you can tell whether any future breakage is in Garmin or elsewhere. Then complete the **Garmin** section of `SETUP_MANUAL.md` which walks you through the interactive token bootstrap.

```
Step 12 of 12 (final). No upcoming step.

Read CLAUDE.md. Use a sub-agent to summarize ingestors/base.py and the patterns from strava.py + withings.py.

CRITICAL CONTEXT: Garmin Connect uses an unofficial mobile SSO flow. The python-garminconnect library (latest April 2026) handles this but is fragile — Garmin changed auth in March 2026 and broke prior approaches. Token files persist at ~/.garminconnect/garmin_tokens.json and auto-refresh for ~1 year. We MUST isolate Garmin failures so they don't poison the rest of the pipeline.

Install: `pip install garminconnect>=0.2.40` — verify latest during the step.

Two-part build: local interactive bootstrap, then headless ingestor.

PART A — scripts/garmin_auth.py (NOT in src/):
- Interactive: prompts for email, password, MFA code if challenged
- Calls Garmin(email, password, prompt_mfa=lambda: input("MFA: ")) and client.login()
- On success, tokens are written to ~/.garminconnect/
- Then: tar + base64-encode the directory, print the base64 string to terminal with copy-paste instructions for GitHub Secret GARMINTOKENS_B64
- Print a recovery note: "If MFA fails repeatedly, wait 30 minutes (rate limit) and retry. If still failing, the library may need an update — check https://github.com/cyberjunky/python-garminconnect/issues"

PART B — src/training_pipeline/ingestors/garmin.py:
- GarminIngestor extends IngestorBase, source_key='garmin'.
- On run start: read GARMINTOKENS_B64 env var, base64-decode + untar to a fresh tmp dir, set GARMINTOKENS env var to that path before importing Garmin. The library reads pre-baked tokens and auto-refreshes in-process (refreshes don't persist back to the GitHub Secret — fine, refresh tokens last ~1 year).
- Initialize token-only: `client = Garmin(); client.login(tokenstore=<tmp_dir>)` — no email/password needed.
- Endpoints:
  - client.get_activities(start=0, limit=20) — paginate until hitting an activity we already have (lookup by source='garmin' + source_id=activityId)
  - client.get_user_summary(date) per day since last run — resting HR, steps, body battery
  - client.get_sleep_data(date) per day — sleep score, duration, stages
  - client.get_hrv_data(date) — overnight HRV
  - client.get_training_readiness(date)
- Dedupe vs Strava: a ride that came from both — keep Garmin only if NO Strava record exists for the same start_time ± 60 sec. Otherwise keep Strava, store Garmin's richer metrics under raw.garmin_supplement.
- Daily metrics → daily_summary with source='garmin'.

Add Garmin env vars to shared/config.py: GARMIN_EMAIL (optional, interactive only), GARMIN_PASSWORD (optional), GARMINTOKENS_B64. Update .env.example.

Add CLI `python -m training_pipeline.cli sync --source garmin`.

Add .github/workflows/sync_garmin.yml:
- Schedule cron `45 5 * * *` (daily 05:45 UTC — after Withings, before derived metrics)
- `continue-on-error: true` at the job level — Garmin breaking must NOT cascade
- Env from secrets: GARMINTOKENS_B64, DATABASE_URL
- Run sync command
- Step writing to GITHUB_STEP_SUMMARY with result (success / failure / records ingested)

Tests/ingestors/test_garmin.py:
- Mock garminconnect.Garmin
- Test base64 token decode/encode round-trip
- Test dedupe logic against existing Strava activities
- Verify Garmin failures raise but don't crash the workflow

Update CLAUDE.md: add to Existing systems "Garmin: fragile dependency, isolated via continue-on-error. Recovery = re-run scripts/garmin_auth.py locally, update GARMINTOKENS_B64 secret."

Run lint, typecheck, tests. Commit with message "feat: Garmin ingestor with token-based headless auth".

Manual verification:
1. Locally: `python scripts/garmin_auth.py` — complete MFA if prompted
2. Copy the printed base64 string
3. Add as GitHub Secret GARMINTOKENS_B64
4. Trigger sync_garmin.yml manually
5. Confirm Garmin rows in activities + daily_summary
6. Re-bootstrap needed if you change Garmin password or refresh token expires (~1 year)
```

---

## After Step 12

The system is complete. Operational loop:

1. **Sunday evening:** open your Claude Project, say "Let's plan this week." Claude pulls last 7 days via MCP, asks check-in questions, proposes the plan with reasoning. You discuss; Claude calls `save_weekly_plan(...)`. iCal regenerates, iPhone updates, Notion mirrors the plan.
2. **Daily:** workouts auto-sync. Glance at the plan on your phone via Notion or iOS Calendar.
3. **Post-session:** tell Claude "Just lifted, RPE 8, no pain, felt strong." Claude calls `log_session(...)`. Subjective + objective tied together.
4. **Mid-week adjust:** "Slept 5 hours, what should I do today?" Claude calls `readiness_today()`, adjusts.
5. **Monthly review:** "Pull last month's training load trend and weight trend, tell me what's working."

Periodic maintenance:
- Re-bootstrap Garmin auth annually (refresh token expires)
- Rotate Strava + Withings refresh tokens when warnings appear in workflow logs
- Re-tune FTP + HR zones in athlete_context as you rebuild

When you outgrow this (more sources, more users, real-time webhooks): migrate scheduling from GitHub Actions to Prefect or similar. The data model and MCP layer transfer unchanged.
