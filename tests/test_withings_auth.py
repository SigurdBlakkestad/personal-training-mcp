"""Structural tests for scripts/withings_auth.py.

The script is a one-shot interactive tool; we only verify that it imports
cleanly and that build_authorize_url emits the parameters Withings expects.
No network, no http server.
"""

from __future__ import annotations

import importlib.util
import urllib.parse
from pathlib import Path
from types import ModuleType


def _load_script() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "scripts" / "withings_auth.py"
    spec = importlib.util.spec_from_file_location("withings_auth", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_authorize_url_contains_required_params() -> None:
    module = _load_script()
    url = module.build_authorize_url("client-abc", "state-xyz")
    parsed = urllib.parse.urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "account.withings.com"
    assert parsed.path == "/oauth2_user/authorize2"

    query = urllib.parse.parse_qs(parsed.query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["client-abc"]
    assert query["state"] == ["state-xyz"]
    assert query["scope"] == ["user.activity,user.metrics,user.info"]
    assert query["redirect_uri"] == ["http://localhost:8765/callback"]


def test_module_constants_match_documented_endpoints() -> None:
    module = _load_script()
    assert module.TOKEN_URL == "https://wbsapi.withings.net/v2/oauth2"
    assert module.REDIRECT_URI == "http://localhost:8765/callback"
    assert module.CALLBACK_PORT == 8765
