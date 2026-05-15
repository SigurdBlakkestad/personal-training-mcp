# Scripts

One-time, interactive bootstrap helpers. They are **not** imported by the
package and are not run in CI — they exist to mint long-lived credentials
that then live in `.env` and GitHub Actions secrets.

Run them locally from the repo root with the project's virtualenv active.

## `withings_auth.py`

Mints the Withings access token, refresh token, and user id via OAuth 2.0
(authorization-code flow). Required by the Withings ingestor.

Prerequisites:

- `WITHINGS_CLIENT_ID` and `WITHINGS_CLIENT_SECRET` set (env or paste when
  prompted). Get them from https://developer.withings.com.
- The Withings developer app's callback URL must be exactly
  `http://localhost:8765/callback`.

Usage:

```bash
python scripts/withings_auth.py
```

The script:

1. Starts a local HTTP server on `localhost:8765`.
2. Prints an authorize URL; open it in your browser and click Authorize.
3. Withings redirects back to the local server with a `code` parameter.
4. The script exchanges the code for tokens and prints three lines:
   `WITHINGS_ACCESS_TOKEN`, `WITHINGS_REFRESH_TOKEN`, `WITHINGS_USERID`.

Paste each line into `.env`, and add each as a GitHub Actions secret in
**Settings → Secrets and variables → Actions**.

Re-run only if the refresh token is revoked or the Withings developer app
is rotated.

## `garmin_auth.py`

Mints the base64-encoded Garmin Connect token bundle stored in the
`GARMINTOKENS_B64` secret. Required by the Garmin ingestor.

Usage:

```bash
python scripts/garmin_auth.py
```

Prompts for Garmin email, password, and MFA code if challenged. On
success it prints a single long base64 string — paste it into the
`GARMINTOKENS_B64` GitHub secret. Tokens last roughly a year; re-run
when the workflow starts failing with auth errors.
