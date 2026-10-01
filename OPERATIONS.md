# Operations

How to run, maintain, and recover this pipeline once it's built. Read this before something breaks, not after.

## Daily / automated rhythm

Once setup is complete (`docs/SETUP_MANUAL.md`), the system runs without you:

```
05:00 UTC   Strava sync (also runs every 2h during the day)
05:30 UTC   Withings sync
05:45 UTC   Garmin sync (if Garmin tokens are configured)
            └─ then, in the same workflow: compute derived metrics
06:30 UTC   Notion mirror
06:35 UTC   Publish .ics calendar (triggered after notion mirror)
```

Derived metrics (TSS, CTL/ATL/TSB, weekly load, weight trend) are a step inside
`sync_garmin.yml`, not a workflow of their own, and only run when the Garmin
sync step succeeded. There is no scheduled `compute_derived.yml` run any more:
computing on stale data reported a cheerful success through a 6-day sync outage,
and skipping the compute makes the gap visible instead. The two steps alert
separately — a failed compute files "Derived metrics is failing", not "Garmin
sync is failing".

`compute_derived.yml` still exists, manual only (Actions → Compute Derived
Metrics → Run workflow), for ad-hoc reruns and backfills. Its `recompute_load`
input forces a recompute of `training_load` for the last 60 days, overwriting
values that are already there; leave it off unless you're deliberately
rewriting history (e.g. after changing FTP or HR settings).

All on GitHub Actions. View runs at `github.com/<user>/personal-training-mcp/actions`.

## Weekly rhythm (you)

- **Sunday evening:** open the Claude Project, plan the week with Claude. Claude calls `save_weekly_plan` via MCP. Plan appears in Notion and iOS Calendar within an hour.
- **After each session:** tell Claude "lifted, RPE 7, no pain, notes..." — Claude logs via `log_session`.
- **Mid-week adjustments:** "slept 5 hours, what should I do today?" — Claude calls `readiness_today` and adapts.

## Monthly rhythm (you)

- **Review month-end:** "show me this month's training load trend and weight trend" — Claude analyzes.
- **Check token health:** scan workflow logs for any "refresh token rotated" warnings from Strava. If you see one, update the corresponding GitHub Secret with the new value from the log. Withings and Garmin store their rotated tokens themselves (see below).

## Annual rhythm (you)

- **Garmin re-bootstrap:** each run rotates and stores the Garmin refresh token, so no routine rotation is needed. When `sync_garmin.yml` starts failing with auth errors, follow "Garmin workflow failing" below — re-bootstrapping the secret alone is not enough, the stored row has to be cleared too.

---

## Common failures and recovery

### "Strava workflow failing with 401"

**Likely cause:** refresh token was rotated and the value in GitHub Secrets is stale.

**Fix:**
1. Check the most recent successful workflow log for a line like `Strava issued new refresh_token: <new_value>`. The ingestor logs this whenever it sees a rotation.
2. If no rotation log is visible, run the OAuth flow again (see `SETUP_MANUAL.md` Section 4.2) to get a fresh refresh token.
3. Update the `STRAVA_REFRESH_TOKEN` GitHub Secret with the new value.
4. Re-trigger the workflow manually.

### "Withings workflow failing with 401"

**Where the live token lives:** the `withings` row in `service_credentials`, not the `WITHINGS_REFRESH_TOKEN` secret. Withings issues a new refresh token on every refresh and invalidates the previous one, so each run saves the new token in its own transaction straight after refreshing — a sync that fails later in the same run still keeps it. The secret is only the seed used when that row doesn't exist yet.

**Likely cause:** the stored token was revoked or expired (e.g. unused for months, or access withdrawn in the Withings app), or a run refreshed but could not store the new token (`withings.refresh_token.save_failed` in the log).

**Fix:**
1. Re-run `python scripts/withings_auth.py` locally with `DATABASE_URL` set (as in `.env`). It writes the fresh refresh token straight to the `withings` row.
2. Update the `WITHINGS_REFRESH_TOKEN` (and `WITHINGS_ACCESS_TOKEN`) GitHub Secrets with the printed values, so the seed is current too
3. Re-trigger the workflow

### "Garmin workflow failing"

Garmin breaks in two ways: the stored token gets rejected, or Garmin changed their auth (also rare but happens).

**Where the live token lives:** the `garmin` row in `service_credentials`, not the `GARMINTOKENS_B64` secret. Garmin's DI flow issues a new refresh token on every refresh and invalidates the previous one, so each run writes back whatever the client ends up holding. The secret is only the seed used when that row doesn't exist yet.

**Token rejected (`API Error 401`, "Failed to retrieve social profile"):**
1. Locally: `python scripts/garmin_auth.py`
2. Complete MFA prompt
3. Copy the base64 output
4. Update `GARMINTOKENS_B64` secret
5. Clear the rejected row so the fresh seed is picked up:
   `delete from service_credentials where service = 'garmin';`
6. Re-trigger workflow

Skipping step 5 leaves the run on the rejected token and the new secret is ignored.

**Garmin changed their auth:**
1. Check https://github.com/cyberjunky/python-garminconnect/issues for current status
2. Wait for a library update if one's not out yet
3. Bump the `garminconnect` floor in `pyproject.toml` when fix is released
4. Re-bootstrap with `scripts/garmin_auth.py` (the new version may need a fresh login)

Garmin has its own workflow, so a red run never blocks the other syncs. Don't panic-fix.

### "Notion mirror failing with rate limit (429)"

The mirror code retries on 429 with backoff. If it's still failing, you probably hit the 3 req/sec ceiling with a large backfill. Reduce the batch size in `notion_sync/activities_mirror.py` or run the workflow during a quieter time.

### "MCP server returning 502 from Render"

The server is kept awake by an external ping against its `/health` endpoint, so cold-start 502s from Render sleeping should no longer happen. If you're seeing one, check the Render dashboard for the service status.

### "Postgres connection errors"

Supabase free tier pauses projects after 7 days of inactivity. Trigger any workflow or run any query manually to wake it. If it's been longer than that, Supabase may have suspended the project — check the dashboard.

### "iPhone calendar not updating"

iOS polls subscribed calendars at intervals it chooses (15 min – several hours). Force a refresh:
1. Open the Calendar app
2. Pull down on the inbox view to refresh

If still not updating, check that `https://<your-username>.github.io/personal-training-mcp/training.ics` returns the latest content in a browser. If it does but iPhone doesn't show it: delete the subscribed calendar (Settings → Calendar → Accounts → tap the subscribed calendar → Delete Account) and re-add it.

### "GitHub Actions failing on every step with 'context access might be invalid'"

A required secret is missing. Check Settings → Secrets and variables → Actions and verify every secret in `.env.example` is also a GitHub Secret.

---

## Secret rotation policy

Treat all credentials as rotatable. Practical timeline:

- **Strava refresh tokens:** rotate automatically with each token refresh. Update the secret when the workflow logs a rotation warning.
- **Withings refresh tokens:** rotate on every refresh and are stored in `service_credentials`, so they need no manual upkeep. Re-bootstrap only when the workflow starts failing on auth.
- **Garmin tokens:** rotate on every refresh and are stored in `service_credentials`, so they need no manual upkeep. Re-bootstrap only when the workflow starts failing on auth.
- **Supabase database password:** rotate via Supabase dashboard if you ever suspect exposure. Update `DATABASE_URL` secret immediately.
- **Notion integration token:** stable until you revoke it. If suspected exposure, revoke via Notion settings and generate a new one.
- **Strava client secret, Withings client secret:** stable. Only rotate if exposed.

---

## Backfill operations

To re-ingest historical data from a source (e.g., after a long outage, or initial setup):

```bash
# Locally with .env populated
python -m training_pipeline.cli sync --source strava --since 2025-01-01
python -m training_pipeline.cli sync --source withings --since 2025-01-01
python -m training_pipeline.cli sync --source garmin --since 2025-01-01
python -m training_pipeline.cli compute-derived
```

The ingestors are idempotent on (source, source_id) so this is safe to re-run.

It is also safe to run while a scheduled sync is in flight. Garmin and Withings each hold a Postgres advisory lock on their stored token: Garmin for the whole sync, Withings only while it refreshes. A second run waits up to 5 minutes for the lock, then fails with `LockNotAvailable` and leaves the running sync's token alone; re-run it once the other finishes. The lock needs `DATABASE_URL` to be a direct or session-mode connection (port 5432), not Supabase's transaction pooler (6543). Garmin recovery is the exception: delete the stored `garmin` row only while no Garmin sync is running, or that sync writes its token straight back.

---

## Local development

```bash
# One-time setup
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Copy .env.example and fill in values
cp .env.example .env
# Edit .env with your secrets

# Run migrations against your local or Supabase DB
alembic upgrade head

# Run a sync manually
python -m training_pipeline.cli sync --source strava

# Run the MCP server locally for testing
python -m training_pipeline.cli serve-mcp
# Now available at http://localhost:8000/mcp

# Run tests
pytest

# Lint and typecheck
ruff check .
mypy src/
```

---

## Adding a new ingestor (future)

The shared `IngestorBase` makes this straightforward:

1. Add credentials to `shared/config.py` and `.env.example`
2. Create `src/training_pipeline/ingestors/<source>.py` extending `IngestorBase`
3. Override `name`, `source_key`, and the fetch logic
4. Add a migration if the source provides data that needs new columns
5. Add a CLI route in `cli.py`
6. Add a workflow `.github/workflows/sync_<source>.yml`
7. Add tests under `tests/ingestors/`

Patterns to follow: see `strava.py` (simpler OAuth), `withings.py` (refresh on 401), `garmin.py` (token-store pattern).

---

## Database maintenance

Supabase manages backups automatically on the free tier (7 days of point-in-time recovery). For your own peace of mind:

```bash
# Export everything to a local SQL dump occasionally
pg_dump "$DATABASE_URL" > backups/$(date +%Y-%m-%d).sql

# Or just the data, not the schema
pg_dump --data-only "$DATABASE_URL" > backups/data-$(date +%Y-%m-%d).sql
```

Add `backups/` to `.gitignore` (already done) and never commit a dump containing real data.

---

## When to refactor vs. tolerate

This repo will sprawl over years. Keep these rules:

- **Tolerate** ingestor-specific quirks. Each source's weirdness lives in its own module. Don't try to abstract Garmin's session-based auth into the same shape as Strava's OAuth.
- **Refactor** when the same problem is solved 3+ times. Three ingestors all doing rate-limit pause? Extract to `ingestors/rate_limit.py`.
- **Tolerate** the schema's JSONB `raw` column. It saves you from many migrations.
- **Refactor** if you find yourself querying inside the JSONB column in MCP tools. Promote that field to a real column.
- **Don't** rewrite the whole pipeline because something annoying. Identify the smallest unit that needs to change.
