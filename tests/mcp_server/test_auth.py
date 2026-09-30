"""Unit tests for the MCP OAuth allowlist and the build_auth factory."""

from itertools import combinations
from typing import Any

import pytest
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubProvider

from training_pipeline.mcp_server.auth import RestrictedGitHubProvider, build_auth


class FakeSettings:
    MCP_GITHUB_CLIENT_ID = ""
    MCP_GITHUB_CLIENT_SECRET = ""
    MCP_PUBLIC_URL = ""
    MCP_ALLOWED_GITHUB_LOGINS = ""
    MCP_ALLOWED_GITHUB_IDS = ""


_FULL = {
    "MCP_GITHUB_CLIENT_ID": "id",
    "MCP_GITHUB_CLIENT_SECRET": "secret",
    "MCP_PUBLIC_URL": "https://example.com/",
    "MCP_ALLOWED_GITHUB_LOGINS": "sigurdblakkestad, second-user",
}


def _settings(**values: str) -> FakeSettings:
    s = FakeSettings()
    for name, value in values.items():
        setattr(s, name, value)
    return s


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


def test_build_auth_constructs_provider_when_configured() -> None:
    s = FakeSettings()
    s.MCP_GITHUB_CLIENT_ID = "id"
    s.MCP_GITHUB_CLIENT_SECRET = "secret"
    s.MCP_PUBLIC_URL = "https://example.com/"
    s.MCP_ALLOWED_GITHUB_LOGINS = "sigurdblakkestad, second-user"
    provider = build_auth(s)  # type: ignore[arg-type]
    assert isinstance(provider, RestrictedGitHubProvider)
    assert provider._allowed_logins == {"sigurdblakkestad", "second-user"}


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_build_auth_returns_none_when_all_blank(blank: str) -> None:
    s = _settings(**{name: blank for name in _FULL}, MCP_ALLOWED_GITHUB_IDS=blank)
    assert build_auth(s) is None  # type: ignore[arg-type]


_PARTIAL = [subset for size in range(1, len(_FULL)) for subset in combinations(sorted(_FULL), size)]


@pytest.mark.parametrize("present", _PARTIAL, ids=["+".join(p) for p in _PARTIAL])
def test_build_auth_raises_on_partial_config(present: tuple[str, ...]) -> None:
    s = _settings(**{name: _FULL[name] for name in present})
    with pytest.raises(RuntimeError) as excinfo:
        build_auth(s)  # type: ignore[arg-type]
    message = str(excinfo.value)
    for name in _FULL:
        assert (name in message) is (name not in present)


@pytest.mark.parametrize("logins", ["   ", ",", " , ,"])
def test_build_auth_raises_on_blank_allowlist(logins: str) -> None:
    # The other three set is clear intent; an allowlist that parses to nothing
    # must not quietly leave the endpoint open (or admit any GitHub user).
    s = _settings(**{**_FULL, "MCP_ALLOWED_GITHUB_LOGINS": logins})
    with pytest.raises(RuntimeError, match="Missing or empty: MCP_ALLOWED_GITHUB_LOGINS\\."):
        build_auth(s)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", sorted(_FULL))
def test_build_auth_treats_whitespace_only_as_unset(name: str) -> None:
    s = _settings(**{**_FULL, name: " \t "})
    with pytest.raises(RuntimeError, match=f"Missing or empty: {name}\\."):
        build_auth(s)  # type: ignore[arg-type]


def test_build_auth_ids_alone_is_partial_config() -> None:
    s = _settings(MCP_ALLOWED_GITHUB_IDS="123")
    with pytest.raises(RuntimeError, match="MCP_GITHUB_CLIENT_ID"):
        build_auth(s)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("ids", "match"),
    [(",", "lists no ids"), ("123, octocat", "not numeric: octocat")],
)
def test_build_auth_rejects_unusable_id_allowlist(ids: str, match: str) -> None:
    s = _settings(**_FULL, MCP_ALLOWED_GITHUB_IDS=ids)
    with pytest.raises(RuntimeError, match=match):
        build_auth(s)  # type: ignore[arg-type]


def test_build_auth_passes_id_allowlist() -> None:
    provider = build_auth(_settings(**_FULL, MCP_ALLOWED_GITHUB_IDS="123, 456"))  # type: ignore[arg-type]
    assert provider is not None
    assert provider._allowed_ids == frozenset({"123", "456"})


def _real_provider(
    monkeypatch: pytest.MonkeyPatch, ids: str, upstream: AccessToken
) -> RestrictedGitHubProvider:
    provider = build_auth(_settings(**_FULL, MCP_ALLOWED_GITHUB_IDS=ids))  # type: ignore[arg-type]
    assert provider is not None

    async def upstream_verify(self: GitHubProvider, token: str) -> AccessToken | None:
        return upstream

    monkeypatch.setattr(GitHubProvider, "verify_token", upstream_verify)
    return provider


def _github_access(login: str, github_id: str | None) -> AccessToken:
    claims: dict[str, Any] = {"login": login}
    if github_id is not None:
        claims["sub"] = github_id
    return AccessToken(token="t", client_id="c", scopes=[], expires_at=None, claims=claims)


@pytest.mark.parametrize(
    ("ids", "github_id", "admitted"),
    [
        ("", "999", True),  # no id allowlist: login check alone, as before
        ("", None, True),
        ("123", "123", True),
        ("123", "999", False),  # re-registered login, different account
        ("123", None, False),  # id claim missing: fail closed
    ],
)
async def test_id_allowlist_pins_login(
    monkeypatch: pytest.MonkeyPatch, ids: str, github_id: str | None, admitted: bool
) -> None:
    provider = _real_provider(monkeypatch, ids, _github_access("sigurdblakkestad", github_id))
    assert (await provider.verify_token("t") is not None) is admitted


async def test_id_allowlist_does_not_bypass_login_check(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _real_provider(monkeypatch, "123", _github_access("someone-else", "123"))
    assert await provider.verify_token("t") is None
