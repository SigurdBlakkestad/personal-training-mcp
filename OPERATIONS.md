# Operations

How to run, maintain, and recover this pipeline once it's built. Read this before something breaks, not after.

## Daily / automated rhythm

Once setup is complete (`docs/SETUP_MANUAL.md`), the system runs without you:

```
05:00 UTC   Strava sync (also runs every 2h during the day)
05:30 UTC   Withings sync
05:45 UTC   Garmin sync (if Garmin tokens are configured)
06:00 UTC   Compute derived metrics
06:30 UTC   Notion mirror
06:35 UTC   Publish .ics calendar (triggered after notion mirror)
```

All on GitHub Actions. View runs at `github.com/<user>/personal-training-mcp/actions`.

## Weekly rhythm (you)

- **Sunday evening:** open the Claude Project, plan the week with Claude. Claude calls `save_weekly_plan` via MCP. Plan appears in Notion and iOS Calendar within an hour.
- **After each session:** tell Claude "lifted, RPE 7, no pain, notes..." — Claude logs via `log_session`.
- **Mid-week adjustments:** "slept 5 hours, what should I do today?" — Claude calls `readiness_today` and adapts.

## Monthly rhythm (you)

- **Review month-end:** "show me this month's training load trend and weight trend" — Claude analyzes.
- **Check token health:** scan workflow logs for any "refresh token rotated" warnings from Strava or Withings. If you see one, update the corresponding GitHub Secret with the new value from the log.

## Annual rhythm (you)

- **Garmin re-bootstrap:** the Garmin OAuth1 refresh token expires after ~1 year. When `sync_garmin.yml` starts failing with auth errors, run `python scripts/garmin_auth.py` locally and update the `GARMINTOKENS_B64` secret.

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

Same pattern as Strava — refresh token rotated. Check logs for the new value, update `WITHINGS_REFRESH_TOKEN` and `WITHINGS_ACCESS_TOKEN` secrets.

If both tokens are completely expired (unused for months), re-run `python scripts/withings_auth.py` to bootstrap fresh tokens.

### "Garmin workflow failing"

Garmin breaks in two ways: token expired (~yearly) or Garmin changed their auth (also rare but happens).

**Token expired:**
1. Locally: `python scripts/garmin_auth.py`
2. Complete MFA prompt
3. Copy the base64 output
4. Update `GARMINTOKENS_B64` secret
5. Re-trigger workflow

**Garmin changed their auth:**
1. Check https://github.com/cyberjunky/python-garminconnect/issues for current status
2. Wait for a library update if one's not out yet
3. Bump version in `requirements.txt` when fix is released
4. Re-bootstrap with `scripts/garmin_auth.py` (the new version may need a fresh login)

Garmin is isolated with `continue-on-error: true` — other workflows keep running. Don't panic-fix.

### "Notion mirror failing with rate limit (429)"

The mirror code retries on 429 with backoff. If it's still failing, you probably hit the 3 req/sec ceiling with a large backfill. Reduce the batch size in `notion_sync/activities_mirror.py` or run the workflow during a quieter time.

### "MCP server returning 502 from Render"

Render's free tier sleeps services after 15 min of inactivity. First request after sleep takes ~30 seconds to wake. If you're seeing 502, the service is waking — wait and retry. If the 502 persists for over a minute, check the Render dashboard for the service status.

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
- **Withings refresh tokens:** same — log-triggered.
- **Garmin tokens:** re-bootstrap annually or when the workflow starts failing.
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

---

## Local development

```bash
# One-time setup
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

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
