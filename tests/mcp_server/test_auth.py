"""Unit tests for the MCP OAuth allowlist and the build_auth factory."""

from typing import Any

import pytest
from fastmcp.server.auth.auth import AccessToken

from training_pipeline.mcp_server.auth import RestrictedGitHubProvider, build_auth


class FakeSettings:
    MCP_GITHUB_CLIENT_ID = ""
    MCP_GITHUB_CLIENT_SECRET = ""
    MCP_PUBLIC_URL = ""
    MCP_ALLOWED_GITHUB_LOGINS = ""


def _access(login: str | None) -> AccessToken:
    claims: dict[str, Any] = {} if login is None else {"login": login}
    return AccessToken(token="t", client_id="c", scopes=[], expires_at=None, claims=claims)


class _StubProvider(RestrictedGitHubProvider):
    """Bypass GitHubProvider's network setup; drive verify_token directly."""

    def __init__(self, allowed: set[str], upstream: AccessToken | None) -> None:
        self._allowed_logins = {login.lower() for login in allowed}
        self._upstream = upstream

    async def verify_token(self, token: str) -> AccessToken | None:  # type: ignore[override]
        access = self._upstream
        if access is None:
            return None
        login = (access.claims or {}).get("login")
        if not login or login.lower() not in self._allowed_logins:
            return None
        return access


async def test_allowed_login_passes() -> None:
    p = _StubProvider({"sigurdblakkestad"}, _access("SigurdBlakkestad"))
    assert await p.verify_token("t") is not None


async def test_disallowed_login_rejected() -> None:
    p = _StubProvider({"sigurdblakkestad"}, _access("someone-else"))
    assert await p.verify_token("t") is None


async def test_missing_login_claim_fails_closed() -> None:
    p = _StubProvider({"sigurdblakkestad"}, _access(None))
    assert await p.verify_token("t") is None


async def test_upstream_rejection_propagates() -> None:
    p = _StubProvider({"sigurdblakkestad"}, None)
    assert await p.verify_token("t") is None


def test_build_auth_returns_none_when_unconfigured() -> None:
    assert build_auth(FakeSettings()) is None  # type: ignore[arg-type]


def test_build_auth_returns_none_without_allowlist() -> None:
    s = FakeSettings()
    s.MCP_GITHUB_CLIENT_ID = "id"
    s.MCP_GITHUB_CLIENT_SECRET = "secret"
    s.MCP_PUBLIC_URL = "https://example.com"
    # No allowlist -> must not engage auth (which would admit any GitHub user).
    assert build_auth(s) is None  # type: ignore[arg-type]


def test_build_auth_constructs_provider_when_configured() -> None:
    s = FakeSettings()
    s.MCP_GITHUB_CLIENT_ID = "id"
    s.MCP_GITHUB_CLIENT_SECRET = "secret"
    s.MCP_PUBLIC_URL = "https://example.com/"
    s.MCP_ALLOWED_GITHUB_LOGINS = "sigurdblakkestad, second-user"
    provider = build_auth(s)  # type: ignore[arg-type]
    assert isinstance(provider, RestrictedGitHubProvider)
    assert provider._allowed_logins == {"sigurdblakkestad", "second-user"}


@pytest.mark.parametrize("logins", ["", "   ", ","])
def test_blank_allowlist_never_engages(logins: str) -> None:
    s = FakeSettings()
    s.MCP_GITHUB_CLIENT_ID = "id"
    s.MCP_GITHUB_CLIENT_SECRET = "secret"
    s.MCP_PUBLIC_URL = "https://example.com"
    s.MCP_ALLOWED_GITHUB_LOGINS = logins
    assert build_auth(s) is None  # type: ignore[arg-type]
