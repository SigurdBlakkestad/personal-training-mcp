# Training Pipeline — Setup Manual

Everything you do by hand. Follow top to bottom; each section is referenced from the matching step in `BUILD_PLAN.md`.

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

## Section 1 — Local prerequisites (before any build step)

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

## Section 2 — Create the GitHub repo (before Step 1)

Run these commands locally. They create a public repo named `training-pipeline`, push an initial commit, and leave you ready for Step 1 of the build plan.

```bash
# Pick a parent directory (e.g. ~/code)
cd ~/code   # or wherever you keep projects

# Create the local project
mkdir training-pipeline && cd training-pipeline

# Initialise git on main
git init -b main

# Minimal README so the first commit isn't empty
cat > README.md <<'EOF'
# training-pipeline

Personal training data platform. See BUILD_PLAN.md and SETUP_MANUAL.md.
EOF

# Save the build plan and setup manual files alongside README
# (Drop the files from Claude into this directory before this step)

# Create the public GitHub repo and push
gh repo create training-pipeline \
  --public \
  --source=. \
  --remote=origin \
  --description "Personal training data pipeline: Strava/Withings/Garmin -> Postgres -> Claude + Notion + iPhone calendar"

# First commit
git add README.md BUILD_PLAN.md SETUP_MANUAL.md
git commit -m "chore: initial commit with build plan and setup manual"
git push -u origin main
```

If you'd rather create the repo manually on github.com first, do that, then:

```bash
git remote add origin git@github.com:<YOUR-USERNAME>/training-pipeline.git
git add README.md BUILD_PLAN.md SETUP_MANUAL.md
git commit -m "chore: initial commit"
git push -u origin main
```

---

## Section 3 — Supabase (before Step 1)

### 3.1 Create the project

1. Go to https://supabase.com → Sign in → New project
2. Organization: your personal (default)
3. Project name: `training-pipeline`
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

In your `training-pipeline` repo:

```bash
cp .env.example .env   # this file gets created in Step 1; for now just touch
touch .env
```

Edit `.env`:

```
DATABASE_URL=postgresql+psycopg://postgres:YOUR-PASSWORD@db.xxxxx.supabase.co:5432/postgres
LOG_LEVEL=INFO
```

**Verify `.env` is in `.gitignore`** before any commit (Step 1 adds it automatically — if you're touching `.env` before Step 1, double-check).

### 3.4 Add the connection string as a GitHub Secret

1. https://github.com/<your-username>/training-pipeline → Settings → Secrets and variables → Actions
2. **New repository secret** → Name: `DATABASE_URL`, Value: the full connection string with password substituted
3. Save

### 3.5 Enable required Postgres extensions

In Supabase: **Database** → **Extensions** → search for `pgcrypto` → enable. This gives us `gen_random_uuid()` used in the schema.

---

## Section 4 — Strava (before Step 5)

### 4.1 Create a Strava API application

1. Go to https://www.strava.com/settings/api (you must be logged into Strava)
2. **Create & Manage Your App** → fill in:
   - Application Name: `training-pipeline`
   - Category: `Training`
   - Club: leave blank
   - Website: `https://github.com/<your-username>/training-pipeline`
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

## Section 5 — Withings (before Step 6)

### 5.1 Create a Withings developer account + app

1. Go to https://developer.withings.com → **Sign in / Sign up**
2. Create your account using your existing Withings email (the one tied to your scale)
3. **Dashboard** → **Public API** → **Create an app**
4. Fill in:
   - Application name: `training-pipeline`
   - Description: `Personal training data sync`
   - Logo: any small image
   - **Callback URL: `http://localhost:8765/callback`** (must match exactly what the helper script uses in Step 6)
5. After creating: **Client ID** and **Consumer Secret** appear.

### 5.2 Add Withings IDs to .env and GitHub Secrets

`.env`:
```
WITHINGS_CLIENT_ID=...
WITHINGS_CLIENT_SECRET=...
# Access/refresh tokens come from running the helper in Step 6
WITHINGS_ACCESS_TOKEN=
WITHINGS_REFRESH_TOKEN=
WITHINGS_USERID=
```

GitHub Secrets — add `WITHINGS_CLIENT_ID` and `WITHINGS_CLIENT_SECRET` now. The other three you'll add after running the helper script in Step 6.

### 5.3 After Step 6 — run the OAuth helper

Once Step 6 is built:

```bash
python scripts/withings_auth.py
```

The script will:
1. Print an authorization URL — open it in your browser
2. Authorize → Withings redirects to `localhost:8765/callback`
3. Helper captures the `code`, exchanges it, prints access_token, refresh_token, and userid
4. Copy each into `.env` and add as GitHub Secrets

---

## Section 6 — Notion (before Step 10)

### 6.1 Create the integration

1. https://www.notion.so/profile/integrations → **+ New integration**
2. Name: `training-pipeline`
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
1. Open the DB/page → top right `...` → **Connections** → **Connect to** → select `training-pipeline`

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

## Section 7 — Render (before Step 9, for hosting the MCP server)

### 7.1 Create account

1. https://render.com → **Get Started** → sign in with GitHub
2. Authorize Render to access your `training-pipeline` repo

### 7.2 Connect the repo (after Step 9 is built)

Step 9 adds `render.yaml` to the repo, which Render auto-detects.

1. Render dashboard → **New +** → **Blueprint**
2. Select `training-pipeline` repo → Render reads `render.yaml`
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
4. Authentication: none (your MCP server is single-user; if you want belt-and-braces, add a shared secret check in Step 9)
5. Save → return to your training Project → start a conversation → Claude should now see your tools

---

## Section 8 — GitHub Pages (before Step 11, for the iPhone calendar)

### 8.1 Enable Pages

1. GitHub repo → **Settings** → **Pages**
2. Source: **Deploy from a branch**
3. Branch: `main`, folder: `/docs`
4. Save

The site URL will be `https://<your-username>.github.io/training-pipeline/`.

After Step 11 generates `docs/training.ics`, the calendar will be accessible at `https://<your-username>.github.io/training-pipeline/training.ics`.

### 8.2 Subscribe on iPhone (after Step 11 publishes the calendar)

1. iPhone: **Settings** → **Calendar** → **Accounts** → **Add Account** → **Other**
2. **Add Subscribed Calendar**
3. Server: `https://<your-username>.github.io/training-pipeline/training.ics`
4. Tap **Next** → **Save**
5. Open the **Calendar** app — your training sessions appear under a new calendar

The calendar refreshes automatically based on iOS's polling interval (typically 15 min – 1 hour). To force a refresh: open Calendar app, pull down to refresh.

---

## Section 9 — Garmin (before Step 12, do this LAST)

Garmin is the most fragile dependency. Don't tackle this until Strava, Withings, derived metrics, MCP, Notion, and iCal are all running for at least a week. That way, when something inevitably breaks, you'll know where.

### 9.1 Prerequisites

- Your Garmin Connect account (the one tied to your FR945 and Edge 840)
- If you have 2FA enabled (recommended): be ready to type an MFA code

### 9.2 After Step 12 is built — interactive token bootstrap

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

This isn't strictly tied to a build step — do it whenever you're ready to start coaching conversations.

### 10.1 Create the Project

1. https://claude.ai → **Projects** → **+ Create project**
2. Name: `Hybrid Training Coach` (or whatever you used in Incognito)
3. What are you trying to achieve: your one-liner
4. **Project instructions:** paste the polished instructions from your Incognito-chat preparation
5. **Project files:** upload
   - Your `training_context.md` (the compressed version we built)
   - The PT exercise photo

### 10.2 Connect the MCP server

After Step 9 is built and deployed:
1. **Settings** → **Connectors** → **Add custom connector** → paste your Render URL
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
[ ] Section 1: macOS prerequisites installed
[ ] Section 2: GitHub repo created and pushed
[ ] Section 3: Supabase project + DATABASE_URL in .env and GitHub Secrets
[ ] BUILD: Steps 1-3 (scaffold, CLAUDE.md, schema)
[ ] Section 4: Strava app + refresh token + secrets
[ ] BUILD: Steps 4-5 (ingestor base + Strava)
[ ] Section 5: Withings developer app + client_id/secret
[ ] BUILD: Step 6 (Withings OAuth helper)
[ ] Section 5.3: Run helper, get tokens, add to secrets
[ ] BUILD: Step 7 (Withings ingestor)
[ ] BUILD: Step 8 (derived metrics)
[ ] Section 7: Render account
[ ] BUILD: Step 9 (MCP server)
[ ] Section 7.4: Connect MCP to Claude
[ ] Section 6: Notion integration + 3 databases
[ ] BUILD: Step 10 (Notion mirror)
[ ] Section 8: GitHub Pages enabled
[ ] BUILD: Step 11 (iCal generator)
[ ] Section 8.2: Subscribe on iPhone
[ ] WAIT: run the system for at least a week
[ ] Section 9: Garmin auth bootstrap
[ ] BUILD: Step 12 (Garmin ingestor)
[ ] Section 10: Claude Project + MCP connector
```
