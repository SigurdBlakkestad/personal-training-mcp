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

This is not a one-click install — it's a fork-and-build-your-own setup. The build was designed so Claude Code does most of the typing; you bring the accounts, secrets, and a weekend of focused time.

**To run your own copy:**

1. **Fork this repo** and clone it locally.
2. **Work through `docs/SETUP_MANUAL.md` top to bottom.** It walks every account, OAuth flow, and secret you need (Supabase, Strava, Withings, Notion, Render, GitHub Pages, optional Garmin).
3. **Execute `docs/BUILD_PLAN.md` step by step.** 12 paste-ready Claude Code prompts that scaffold the project, build each ingestor, wire up the MCP server, and ship the Notion + iPhone outputs. Use `docs/RUN_STEP_PROMPT.md` as the wrapper prompt that executes one step at a time.
4. **Once running**, `OPERATIONS.md` covers secret rotation and failure recovery. `CLAUDE.md` is the working-context file Claude reads when you collaborate on this codebase.

Total setup: roughly one weekend of focused work, spread across multiple sessions.

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
