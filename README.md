# personal-training-mcp

Personal training data platform. Pulls workouts and health data from Strava, Withings, and Garmin into a single Postgres database, computes derived training metrics, and exposes everything to Claude as a coaching assistant. Plans get mirrored to Notion and subscribed to as an iPhone calendar.

Built solo for personal use. The repo is public so others can fork the architecture, but it's not a product.

## What it does

- **Ingests** Strava activities, Withings body composition and sleep, and Garmin training metrics on a daily schedule via GitHub Actions
- **Derives** training load (TSS / TRIMP), CTL/ATL/TSB balance, weekly volume per sport, weight trends
- **Exposes** all of this to Claude via a custom MCP server (read tools for analysis, write tools for logging sessions and saving weekly plans)
- **Mirrors** activities, weekly plans, and a dashboard to Notion (read-only journal view)
- **Publishes** the current weekly plan as an `.ics` calendar feed that iPhone subscribes to

## Stack

Python 3.12 · Postgres on [Supabase](https://supabase.com) (free tier) · GitHub Actions (free for public repos) · Model Context Protocol via [FastMCP](https://github.com/jlowin/fastmcp) · Notion API · Render (free tier, hosts the MCP server) · GitHub Pages (free, hosts the `.ics` calendar)

Total cost: $0 at single-user scale.

## Architecture

```
        ┌──────────┐   ┌──────────┐   ┌──────────┐
        │  Strava  │   │ Withings │   │  Garmin  │
        └────┬─────┘   └────┬─────┘   └────┬─────┘
             │              │              │
             │ GitHub Actions (scheduled crons)
             │              │              │
             ▼              ▼              ▼
        ┌─────────────────────────────────────────┐
        │       Postgres (Supabase free tier)      │
        │   activities · body_measurements ·       │
        │   daily_summary · manual_logs ·          │
        │   weekly_plans · derived_metrics         │
        └────────┬──────────────────────┬─────────┘
                 │                      │
                 │            ┌─────────┴─────────┐
                 │            │                   │
                 ▼            ▼                   ▼
          ┌──────────┐  ┌──────────┐       ┌──────────┐
          │   MCP    │  │  Notion  │       │  iCal    │
          │  server  │  │  mirror  │       │ publish  │
          │ (Render) │  │          │       │ (Pages)  │
          └────┬─────┘  └────┬─────┘       └────┬─────┘
               │             │                  │
               ▼             ▼                  ▼
            Claude         Notion           iPhone
          (coaching       (journal +       Calendar
            chats)        dashboard)
```

## Getting started

The code is already written. To run your own copy you only need to set up your own accounts, paste your secrets in, and trigger the workflows. No coding required.

**Setup path:**

1. **Fork this repo** on GitHub, then `git clone` your fork locally.
2. **Work through `docs/SETUP_MANUAL.md` top to bottom.** It walks every account, OAuth flow, and secret you need: Supabase (database), Strava, Withings, Notion (3 databases), Render (MCP host), GitHub Pages (calendar host), and optionally Garmin. Output: a populated `.env` file locally and a complete set of GitHub Secrets.
3. **Run the local install + first migration**, per `OPERATIONS.md` → "Local development":
   ```
   python3.12 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   alembic upgrade head        # creates tables in your Supabase project
   ```
4. **Trigger the workflows** in your fork's Actions tab to do the first sync (`sync_strava`, `sync_withings`, etc.). After that they run on schedule.
5. **Connect the MCP server to Claude.ai** once Render is deployed (SETUP_MANUAL Section 7.4) and create your Project (Section 10).

> **Heads-up on Garmin.** The Garmin sync relies on [`python-garminconnect`](https://github.com/cyberjunky/python-garminconnect), an *unofficial* reverse-engineered API wrapper. Garmin can change endpoints or rate-limit logins at any time, so this collector may break without warning — it's isolated with `continue-on-error` in `sync_garmin.yml` so a Garmin failure never blocks the other syncs.
>
> Because of that, **Strava is the reliable source for your activities**: it uses an official, stable API, so your workouts keep syncing even when the Garmin route is down. Garmin only layers device-specific metrics (training load, HRV, sleep detail) on top of the Strava activity, and takes priority for those fields when present. Withings (body composition, sleep) is likewise an official, stable API. If you only run Strava + Withings, you get everything except the Garmin device metrics.

`OPERATIONS.md` covers ongoing operation: secret rotation, common failures, backfill, when to refactor.

Total setup: roughly one weekend of focused account-creation and OAuth dances.

### Want a lighter version?

The full stack is overkill if you don't need SQL aggregations or derived training-load metrics. A "Notion-only" variant — Notion as the source of truth, no Supabase, no Alembic, simpler MCP — is feasible and would cut setup to a few hours. Not built yet; open an issue if you want to collaborate on it.
## Privacy and data ownership

Your training data lives in your Supabase project. Your credentials live in your `.env` (gitignored) and GitHub Secrets (encrypted at rest). Nothing in this repo is your personal data. Fork freely.

## Acknowledgements

- [`python-garminconnect`](https://github.com/cyberjunky/python-garminconnect) — the unofficial Garmin API wrapper
- [`chloevoyer/garmin-to-notion`](https://github.com/chloevoyer/garmin-to-notion) — inspired the Garmin sync pattern
- [Strava API](https://developers.strava.com/) and [Withings API](https://developer.withings.com/) — official, well-documented

## License

MIT
