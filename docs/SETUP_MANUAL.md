# Training Pipeline — Setup Manual

Everything you do by hand to run this pipeline against your own accounts. The code is already in the repo — this guide gets you to the point where you can trigger the workflows and they'll work.

Follow top to bottom. Each section is independent (you can pause between Strava and Withings if you need a break), but later sections depend on earlier secrets being in place.

**Required accounts (all free):**
- GitHub
- Supabase
- Strava
- Withings developer
- Notion
- Render (free tier)
- Apple ID (for iPhone calendar — you already have this)
- Garmin Connect (you already have this)

---

## Section 1 — Local prerequisites

### 1.1 Install required tools on macOS

```bash
# Homebrew (if not already)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Python 3.12 (required by latest python-garminconnect)
brew install python@3.12

# git is usually pre-installed; verify
git --version

# GitHub CLI (lets you create the repo from the terminal)
brew install gh
gh auth login   # follow prompts, use HTTPS + login via web browser

# Claude Code
# Install per the official docs at https://docs.claude.com — verify install
claude --version
```

### 1.2 Configure git identity (only if you haven't)

```bash
git config --global user.name "Your Name"
git config --global user.email "your-email@example.com"
```

---

## Section 2 — Fork the repo

1. Go to https://github.com/<original-owner>/personal-training-mcp and click **Fork** (top right). Keep the name `personal-training-mcp` or pick your own.
2. Clone your fork locally:
   ```bash
   cd ~/code   # or wherever you keep projects
   git clone git@github.com:<YOUR-USERNAME>/personal-training-mcp.git
   cd personal-training-mcp
   ```
3. Verify it works locally:
   ```bash
   python3.12 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   pytest   # should be all green
   ```

You now have the code. The rest of this manual gets you the credentials and external services it needs to run.

> **For the original builder:** Section 2 was originally a `git init` + `gh repo create` flow for the very first commit. That's preserved in `docs/archive/BUILD_PLAN.md` if you ever need to recreate the repo from scratch.

---

## Section 3 — Supabase

### 3.1 Create the project

1. Go to https://supabase.com → Sign in → New project
2. Organization: your personal (default)
3. Project name: `personal-training-mcp`
4. Database password: generate a strong one, **save it in your password manager** — you'll need it
5. Region: pick the one closest to you (for you in Norway: `eu-central-1` Frankfurt or `eu-west-1` Ireland)
6. Plan: Free
7. Click **Create new project** and wait ~2 minutes

### 3.2 Copy the connection string

1. In your Supabase project: **Project Settings** (gear icon, bottom left) → **Database**
2. Find **Connection string** → **URI** tab
3. Copy the string. It looks like `postgresql://postgres:[YOUR-PASSWORD]@db.xxxxx.supabase.co:5432/postgres`
4. Replace `[YOUR-PASSWORD]` with the actual password you saved
5. Note: for use with psycopg, prefix with `postgresql+psycopg://` in `.env`

### 3.3 Create local .env

In your `personal-training-mcp` clone:

```bash
cp .env.example .env
```

Edit `.env` and set:

```
DATABASE_URL=postgresql+psycopg://postgres:YOUR-PASSWORD@db.xxxxx.supabase.co:5432/postgres
LOG_LEVEL=INFO
```

`.env` is already in `.gitignore` — never commit it. You'll add the other source credentials (Strava, Withings, Notion, etc.) to this same file as you complete each section.

### 3.4 Add the connection string as a GitHub Secret

1. https://github.com/<your-username>/personal-training-mcp → Settings → Secrets and variables → Actions
2. **New repository secret** → Name: `DATABASE_URL`, Value: the full connection string with password substituted
3. Save

### 3.5 Enable required Postgres extensions

In Supabase: **Database** → **Extensions** → search for `pgcrypto` → enable. This gives us `gen_random_uuid()` used in the schema.

---

## Section 4 — Strava

### 4.1 Create a Strava API application

1. Go to https://www.strava.com/settings/api (you must be logged into Strava)
2. **Create & Manage Your App** → fill in:
   - Application Name: `personal-training-mcp`
   - Category: `Training`
   - Club: leave blank
   - Website: `https://github.com/<your-username>/personal-training-mcp`
   - Application Description: `Personal training data pipeline`
   - Authorization Callback Domain: `localhost` (we'll do OAuth locally one time)
3. Upload an icon if asked (any small image will do)
4. Click **Create**
5. You now have **Client ID** and **Client Secret**. Note both.

### 4.2 Generate a refresh token (one-time OAuth dance)

Strava's OAuth requires opening a URL in a browser. Do this once locally.

**Step 1:** Open this URL in your browser (replace `YOUR_CLIENT_ID`):

```
https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http://localhost/exchange_token&approval_prompt=force&scope=read,activity:read_all,profile:read_all
```

Authorize the app. You'll be redirected to a URL like:

```
http://localhost/exchange_token?state=&code=ABCDEF123456&scope=read,activity:read_all,profile:read_all
```

The page won't load (no server running) but **copy the `code` parameter** from the URL.

**Step 2:** Exchange the code for a refresh token. In your terminal:

```bash
curl -X POST https://www.strava.com/oauth/token \
  -d client_id=YOUR_CLIENT_ID \
  -d client_secret=YOUR_CLIENT_SECRET \
  -d code=THE_CODE_FROM_STEP_1 \
  -d grant_type=authorization_code
```

The response JSON contains `refresh_token`, `access_token`, and `expires_at`. **Save the `refresh_token`** — that's the long-lived credential.

### 4.3 Add Strava to .env and GitHub Secrets

`.env` (append):
```
STRAVA_CLIENT_ID=12345
STRAVA_CLIENT_SECRET=...
STRAVA_REFRESH_TOKEN=...
```

GitHub Secrets (add each one):
- `STRAVA_CLIENT_ID`
- `STRAVA_CLIENT_SECRET`
- `STRAVA_REFRESH_TOKEN`

### 4.4 Note on token rotation

Strava rotates refresh tokens. The ingestor logs a WARNING when it receives a new one. When you see this in workflow logs, update the GitHub Secret with the new value (copy from the log output).

---

## Section 5 — Withings

### 5.1 Create a Withings developer account + app

1. Go to https://developer.withings.com → **Sign in / Sign up**
2. Create your account using your existing Withings email (the one tied to your scale)
3. **Dashboard** → **Public API** → **Create an app**
4. Fill in:
   - Application name: `personal-training-mcp`
   - Description: `Personal training data sync`
   - Logo: any small image
   - **Callback URL: `http://localhost:8765/callback`** (must match exactly what `scripts/withings_auth.py` uses)
5. After creating: **Client ID** and **Consumer Secret** appear.

### 5.2 Add Withings IDs to .env and GitHub Secrets

`.env`:
```
WITHINGS_CLIENT_ID=...
WITHINGS_CLIENT_SECRET=...
# Access/refresh tokens come from running scripts/withings_auth.py (Section 5.3)
WITHINGS_ACCESS_TOKEN=
WITHINGS_REFRESH_TOKEN=
WITHINGS_USERID=
```

GitHub Secrets — add `WITHINGS_CLIENT_ID` and `WITHINGS_CLIENT_SECRET` now. The other three you'll add after running the helper script (Section 5.3).

### 5.3 Run the OAuth helper

```bash
python scripts/withings_auth.py
```

The script will:
1. Print an authorization URL — open it in your browser
2. Authorize → Withings redirects to `localhost:8765/callback`
3. Helper captures the `code`, exchanges it, prints access_token, refresh_token, and userid
4. Copy each into `.env` and add as GitHub Secrets

---

## Section 6 — Notion

### 6.1 Create the integration

1. https://www.notion.so/profile/integrations → **+ New integration**
2. Name: `personal-training-mcp`
3. Type: **Internal**
4. Associated workspace: your personal workspace
5. Capabilities: Read content, Update content, Insert content
6. Submit → copy the **Internal Integration Secret** (starts with `secret_` or `ntn_`)

### 6.2 Create the three databases in Notion

Create these three databases in your Notion workspace. The exact property types matter — the mirror code expects them.

**Database 1: `Training Activities`**

| Property | Type |
|---|---|
| Name | Title |
| Date | Date |
| Sport | Select (options: Cycling, Running, Swimming, Lifting, Walking, Other) |
| Duration (min) | Number |
| Distance (km) | Number |
| Avg HR | Number |
| Training Load | Number |
| RPE | Number |
| Pain | Number |
| Notes | Text |

**Database 2: `Training Plan`**

| Property | Type |
|---|---|
| Session | Title |
| Date | Date |
| Session Type | Select (Cycling, Lifting, Mobility, Rest, Other) |
| Description | Text |
| Duration (min) | Number |
| Intensity | Select (Easy, Moderate, Hard) |
| Status | Select (Planned, Done, Skipped) |

**Database 3: `Training Dashboard`**

This one is a single page (not a multi-row DB). Create a regular page called "Training Dashboard" with a few section headers — the mirror will update blocks under those headers.

### 6.3 Share each database with the integration

For each of the three (and the dashboard page):
1. Open the DB/page → top right `...` → **Connections** → **Connect to** → select `personal-training-mcp`

### 6.4 Get the database IDs

For each database:
1. Open the DB in Notion → click **Share** (top right) or **...** → **Copy link**
2. The URL looks like `https://www.notion.so/<workspace>/<DATABASE_ID>?v=...`
3. The DATABASE_ID is the 32-character segment between the last `/` and the `?` — strip any dashes if present, or keep them; Notion API accepts both.

For the Dashboard page, copy its page URL the same way and extract the ID.

### 6.5 Add Notion to .env and GitHub Secrets

`.env`:
```
NOTION_TOKEN=ntn_...
NOTION_DB_ACTIVITIES_ID=...
NOTION_DB_PLAN_ID=...
NOTION_DB_METRICS_ID=...
```

Add all four as GitHub Secrets.

---

## Section 7 — Render (hosts the MCP server)

### 7.1 Create account

1. https://render.com → **Get Started** → sign in with GitHub
2. Authorize Render to access your `personal-training-mcp` repo

### 7.2 Connect the repo

The repo already includes `render.yaml`, which Render auto-detects.

1. Render dashboard → **New +** → **Blueprint**
2. Select `personal-training-mcp` repo → Render reads `render.yaml`
3. Service plan: Free
4. Add environment variables in the Render dashboard (cannot be in the blueprint for secrets):
   - `DATABASE_URL`
   - Any other secrets your MCP server needs
5. Deploy

### 7.3 Note the public URL

After first deploy, your URL is `https://<service-name>.onrender.com`. The MCP endpoint is `https://<service-name>.onrender.com/mcp`.

**Free tier caveat:** Render free services sleep after 15 minutes of inactivity. First request after sleep takes ~30 seconds to wake. Acceptable for a personal coaching tool — the first question of a conversation has a cold start, follow-ups are instant.

### 7.4 Connect to Claude.ai

1. https://claude.ai → **Settings** → **Connectors** → **Add custom connector**
2. Type: MCP
3. URL: `https://<service-name>.onrender.com/mcp`
4. Authentication: none (the MCP server is single-user; if you want belt-and-braces, add a shared-secret check in `mcp_server/app.py`)
5. Save → return to your training Project → start a conversation → Claude should now see your tools

---

## Section 8 — GitHub Pages (hosts the iPhone calendar feed)

### 8.1 Enable Pages

1. GitHub repo → **Settings** → **Pages**
2. Source: **Deploy from a branch**
3. Branch: `main`, folder: `/docs`
4. Save

The site URL will be `https://<your-username>.github.io/personal-training-mcp/`.

After the `publish_calendar` workflow runs and commits `docs/training.ics`, the calendar will be accessible at `https://<your-username>.github.io/personal-training-mcp/training.ics`.

### 8.2 Subscribe on iPhone (after the first calendar publish)

1. iPhone: **Settings** → **Calendar** → **Accounts** → **Add Account** → **Other**
2. **Add Subscribed Calendar**
3. Server: `https://<your-username>.github.io/personal-training-mcp/training.ics`
4. Tap **Next** → **Save**
5. Open the **Calendar** app — your training sessions appear under a new calendar

The calendar refreshes automatically based on iOS's polling interval (typically 15 min – 1 hour). To force a refresh: open Calendar app, pull down to refresh.

---

## Section 9 — Garmin (optional, do this LAST)

Garmin is the most fragile dependency. Don't tackle this until Strava, Withings, derived metrics, MCP, Notion, and iCal are all running for at least a week. That way, when something inevitably breaks, you'll know where.

### 9.1 Prerequisites

- Your Garmin Connect account (the one tied to your FR945 and Edge 840)
- If you have 2FA enabled (recommended): be ready to type an MFA code

### 9.2 Interactive token bootstrap

Locally:

```bash
python scripts/garmin_auth.py
```

The script prompts for:
1. Garmin email
2. Garmin password
3. MFA code (if challenged — check your email/SMS)

On success it:
1. Writes tokens to `~/.garminconnect/`
2. Tars + base64-encodes that directory
3. Prints the base64 string with copy-paste instructions

### 9.3 Add the base64 token to GitHub Secrets

1. Copy the printed base64 string
2. GitHub Secrets → **New repository secret**
3. Name: `GARMINTOKENS_B64`
4. Value: paste the base64 string
5. Save

### 9.4 Token expiry

Garmin refresh tokens last approximately one year. When the workflow starts failing with auth errors (after roughly a year), re-run `python scripts/garmin_auth.py` locally and update the `GARMINTOKENS_B64` secret.

### 9.5 When Garmin breaks (it will)

Garmin updates their internal auth ~once a year, breaking unofficial libraries. When this happens:
1. The `sync_garmin.yml` workflow will start failing
2. Other workflows keep running (Strava, Withings, derived metrics, Notion, iCal) — `continue-on-error: true` isolates the blast radius
3. Check https://github.com/cyberjunky/python-garminconnect/issues for the current status
4. When a fix is released: bump the version in `requirements.txt`, re-run `scripts/garmin_auth.py` locally, update the secret

Don't panic-fix on the day of breakage. Your other data sources have you covered.

---

## Section 10 — Claude Project setup

Do this whenever you're ready to start coaching conversations — typically after Render is live and the MCP server is reachable.

### 10.1 Create the Project

1. https://claude.ai → **Projects** → **+ Create project**
2. Name: `Hybrid Training Coach` (or whatever you used in Incognito)
3. What are you trying to achieve: your one-liner
4. **Project instructions:** paste the polished instructions from your Incognito-chat preparation
5. **Project files:** upload
   - Your `training_context.md` (the compressed version we built)
   - The PT exercise photo

### 10.2 Connect the MCP server

Once Render shows the service as live (Section 7.3):
1. **Settings** → **Connectors** → **Add custom connector** → paste your Render URL (with the `/mcp` path)
2. Back in the Project, the MCP tools are now available in every conversation

### 10.3 First conversation

Paste the kickoff message we wrote:

```
Read my context file and the PT exercise photo, then:
1. Ask any critical clarifying questions
2. Propose a conservative 2-week starter block
3. Explain the logic of session placement
4. Tell me what to log so we can iterate

Don't write a 12-week plan — we build week by week.
```

---

## Quick-reference checklist

Use this as a single page to track where you are.

```
[ ] Section 1: macOS / Python 3.12 / git / gh / Claude Code installed
[ ] Section 2: forked the repo, cloned locally, `pytest` green
[ ] Section 3: Supabase project + DATABASE_URL in .env and GitHub Secrets
[ ] One-time install: pip install -r requirements.txt && alembic upgrade head
[ ] Section 4: Strava app + refresh token in .env and GitHub Secrets
[ ] Section 5: Withings developer app, client_id/secret in .env and GitHub Secrets
[ ] Section 5.3: ran scripts/withings_auth.py, tokens added
[ ] Section 6: Notion integration + 3 databases shared with it, IDs in secrets
[ ] Section 7: Render service deployed, MCP_URL noted
[ ] Section 8: GitHub Pages enabled for /docs on main
[ ] First sync: trigger sync_strava, sync_withings, compute_derived, notion_mirror, publish_calendar manually from Actions tab
[ ] Section 8.2: subscribed to .ics URL on iPhone
[ ] Section 10: Claude Project created, MCP connector pointed at Render URL
[ ] (optional, last) Section 9: Garmin token bootstrap → GARMINTOKENS_B64 secret → trigger sync_garmin
```

After this is all green, you only have to revisit `OPERATIONS.md` for secret rotation, failure recovery, and ongoing weekly/monthly rhythm.
