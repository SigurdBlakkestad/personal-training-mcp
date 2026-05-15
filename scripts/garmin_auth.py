"""Interactive Garmin Connect token bootstrap.

Run locally one time (and again when refresh tokens expire, ~once a year):

    python scripts/garmin_auth.py

The script:
  1. Reads email/password from env (GARMIN_EMAIL, GARMIN_PASSWORD) or prompts for them
  2. Prompts for MFA code if Garmin challenges (when 2FA is enabled)
  3. Lets python-garminconnect write tokens to ~/.garminconnect/
  4. Tars + base64-encodes that directory and prints the string
  5. You paste that string into the GitHub Secret GARMINTOKENS_B64

If MFA fails repeatedly, wait ~30 minutes (rate limit) before retrying. If still
failing, see https://github.com/cyberjunky/python-garminconnect/issues — Garmin
changes their auth internals roughly once a year and the library may need an
update.
"""

from __future__ import annotations

import base64
import getpass
import io
import os
import sys
import tarfile
from pathlib import Path


def _read_credential(env_name: str, prompt: str, secret: bool = False) -> str:
    existing = os.environ.get(env_name)
    if existing:
        return existing
    reader = getpass.getpass if secret else input
    value = reader(prompt).strip()
    if not value:
        raise SystemExit(f"{env_name} is required")
    return value


def _encode_tokens(tokens_dir: Path) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(tokens_dir, arcname=".garminconnect")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def main() -> int:
    print("Garmin Connect token bootstrap")
    print("-" * 40)

    email = _read_credential("GARMIN_EMAIL", "Garmin email: ")
    password = _read_credential("GARMIN_PASSWORD", "Garmin password: ", secret=True)

    try:
        from garminconnect import Garmin  # type: ignore[import-untyped]
    except ImportError:
        print("ERROR: install dependencies first → pip install -r requirements.txt")
        return 1

    def prompt_mfa() -> str:
        return input("MFA code (check email/SMS/authenticator app): ").strip()

    tokens_dir = Path.home() / ".garminconnect"

    print("Logging in (this may take a moment)...")
    try:
        client = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
        # Pass tokenstore so the library persists tokens after a successful
        # login via any strategy in its cascade (mobile+*, widget+*, portal+*).
        # Without it, login can succeed silently via widget/portal yet leave
        # nothing on disk because the library only dumps when a path is given.
        client.login(tokenstore=str(tokens_dir))
    except Exception as exc:  # noqa: BLE001  -- one-off CLI tool, surface anything
        print(f"\nLogin failed: {exc}")
        print()
        print("If MFA was challenged and failed: wait ~30 minutes, then retry.")
        print("If the error mentions auth/sso changes, the library may need a bump:")
        print("  https://github.com/cyberjunky/python-garminconnect/issues")
        return 1

    if not tokens_dir.exists():
        print(f"ERROR: expected tokens at {tokens_dir} but none were written")
        return 1

    print(f"Tokens written to {tokens_dir}")

    encoded = _encode_tokens(tokens_dir)

    print()
    print("=" * 60)
    print("Copy the base64 string below into GitHub Secret GARMINTOKENS_B64:")
    print("  https://github.com/<your-username>/personal-training-mcp/settings/secrets/actions")
    print("=" * 60)
    print(encoded)
    print("=" * 60)
    print()
    print("Tokens are valid for ~1 year. Re-run this script when the workflow")
    print("starts failing with auth errors.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
