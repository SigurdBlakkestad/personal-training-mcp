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

This is not a one-click install. See:

- **`SETUP_MANUAL.md`** — manual setup (accounts, OAuth, secrets, Notion databases, iPhone subscription)
- **`BUILD_PLAN.md`** — staged build with paste-ready Claude Code prompts (12 steps)
- **`OPERATIONS.md`** — running the system, secret rotation, recovery from common failures
- **`CLAUDE.md`** — guidance for Claude Code when working on this codebase

Total setup time if you follow it end-to-end: roughly one weekend of focused work, spread across multiple sessions.

## Privacy and data ownership

Your training data lives in your Supabase project. Your credentials live in your `.env` (gitignored) and GitHub Secrets (encrypted at rest). Nothing in this repo is your personal data. Fork freely.

## Acknowledgements

- [`python-garminconnect`](https://github.com/cyberjunky/python-garminconnect) — the unofficial Garmin API wrapper
- [`chloevoyer/garmin-to-notion`](https://github.com/chloevoyer/garmin-to-notion) — inspired the Garmin sync pattern
- [Strava API](https://developers.strava.com/) and [Withings API](https://developer.withings.com/) — official, well-documented

## License

MIT
