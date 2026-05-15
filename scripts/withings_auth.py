"""One-time Withings OAuth bootstrap.

Run locally once to obtain WITHINGS_ACCESS_TOKEN, WITHINGS_REFRESH_TOKEN, and
WITHINGS_USERID:

    python scripts/withings_auth.py

Requires WITHINGS_CLIENT_ID and WITHINGS_CLIENT_SECRET in your environment
(or you'll be prompted). The Withings developer app's callback URL must be
http://localhost:8765/callback to match this script.

After tokens print, paste each line into .env and add each as a GitHub
repository secret. The Withings ingestor refreshes the access token
automatically; you only re-run this if the refresh token is revoked or
the Withings developer app is rotated.
"""

from __future__ import annotations

import getpass
import http.server
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError

REDIRECT_URI = "http://localhost:8765/callback"
CALLBACK_PORT = 8765
AUTHORIZE_URL = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
SCOPES = "user.activity,user.metrics,user.info"


def _read_credential(env_name: str, prompt: str, secret: bool = False) -> str:
    existing = os.environ.get(env_name)
    if existing:
        return existing
    reader = getpass.getpass if secret else input
    value = reader(prompt).strip()
    if not value:
        raise SystemExit(f"{env_name} is required")
    return value


def build_authorize_url(client_id: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "state": state,
        "scope": SCOPES,
        "redirect_uri": REDIRECT_URI,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    captured: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        query = urllib.parse.parse_qs(parsed.query)
        code = query.get("code", [""])[0]
        state = query.get("state", [""])[0]
        type(self).captured = {"code": code, "state": state}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (
            "<!DOCTYPE html><html><body>"
            "<h2>Withings authorization received.</h2>"
            "<p>You can close this tab and return to the terminal.</p>"
            "</body></html>"
        )
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format: str, *args: object) -> None:
        return


def _wait_for_callback(expected_state: str) -> str:
    _CallbackHandler.captured = {}
    server = http.server.HTTPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
    print(f"Listening on {REDIRECT_URI} — waiting for Withings redirect...")
    try:
        while not _CallbackHandler.captured.get("code"):
            server.handle_request()
    finally:
        server.server_close()

    captured = _CallbackHandler.captured
    if captured.get("state") != expected_state:
        raise SystemExit(
            f"State mismatch — possible CSRF. Got {captured.get('state')!r}, "
            f"expected {expected_state!r}."
        )
    return captured["code"]


def _exchange_code(client_id: str, client_secret: str, code: str) -> dict[str, str]:
    body = urllib.parse.urlencode(
        {
            "action": "requesttoken",
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": REDIRECT_URI,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - URL is constant
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"Token endpoint returned {exc.code}: {detail}") from exc
    except URLError as exc:
        raise SystemExit(f"Network error contacting {TOKEN_URL}: {exc.reason}") from exc

    status = payload.get("status")
    if status != 0:
        raise SystemExit(f"Withings returned status={status}: {payload.get('error') or payload}")
    return payload["body"]


def main() -> int:
    print("Withings OAuth bootstrap")
    print("-" * 40)

    client_id = _read_credential("WITHINGS_CLIENT_ID", "Withings client_id: ")
    client_secret = _read_credential(
        "WITHINGS_CLIENT_SECRET", "Withings client_secret: ", secret=True
    )

    state = secrets.token_urlsafe(16)
    authorize_url = build_authorize_url(client_id, state)

    print()
    print("Open this URL in your browser, then click Authorize:")
    print()
    print(f"  {authorize_url}")
    print()

    code = _wait_for_callback(state)
    print("Got authorization code — exchanging for tokens...")
    body = _exchange_code(client_id, client_secret, code)

    access_token = body["access_token"]
    refresh_token = body["refresh_token"]
    userid = str(body["userid"])

    print()
    print("=" * 60)
    print("Add these to .env AND as GitHub repository secrets:")
    print("  https://github.com/<your-username>/personal-training-mcp/settings/secrets/actions")
    print("=" * 60)
    print(f"WITHINGS_ACCESS_TOKEN={access_token}")
    print(f"WITHINGS_REFRESH_TOKEN={refresh_token}")
    print(f"WITHINGS_USERID={userid}")
    print("=" * 60)
    print()
    print("Access tokens last ~3 hours; the ingestor refreshes via refresh_token")
    print("automatically. Withings rotates refresh tokens on use — the ingestor")
    print("logs a WARN when that happens so you can update the GitHub Secret.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
