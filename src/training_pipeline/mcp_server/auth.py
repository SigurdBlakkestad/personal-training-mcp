"""OAuth authentication for the MCP server.

Claude.ai custom connectors authenticate over OAuth 2.1 with Dynamic Client
Registration (DCR) and PKCE — a static bearer header is not an option in that
UI. FastMCP's ``GitHubProvider`` is an OAuth proxy that presents the DCR-
compliant interface Claude.ai expects while delegating the actual login to a
GitHub OAuth app.

OAuth only proves *who* logged in; by itself any GitHub account would pass. This
module wraps the provider so only an explicit allowlist of GitHub logins is
accepted — everyone else is rejected at token verification (401). An optional
allowlist of numeric GitHub user ids pins those logins to specific accounts, so
a renamed or deleted login re-registered by someone else is still rejected. It
fails closed: if a required claim is missing, or OAuth is only partly
configured, access is denied or the server refuses to start.
"""

from __future__ import annotations

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubProvider

from training_pipeline.shared.config import Settings
from training_pipeline.shared.logging import get_logger

logger = get_logger(__name__)


class RestrictedGitHubProvider(GitHubProvider):
    """GitHubProvider that only admits an allowlist of GitHub logins.

    When ``allowed_ids`` is non-empty the token's numeric GitHub user id (the
    ``sub`` claim FastMCP's GitHub provider sets from ``/user``'s ``id``) must
    also be listed; an empty set leaves the login check as the only gate.
    """

    def __init__(
        self,
        *,
        allowed_logins: set[str],
        allowed_ids: frozenset[str] = frozenset(),
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._allowed_logins = {login.lower() for login in allowed_logins}
        self._allowed_ids = allowed_ids

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await super().verify_token(token)
        if access is None:
            return None
        claims = access.claims or {}
        login = claims.get("login")
        if not login or login.lower() not in self._allowed_logins:
            logger.warning("mcp_server.auth.github_login_denied", login=login)
            return None
        if self._allowed_ids:
            github_id = claims.get("sub")
            if not github_id or str(github_id) not in self._allowed_ids:
                logger.warning("mcp_server.auth.github_id_denied", login=login, github_id=github_id)
                return None
        return access


def _split_csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def build_auth(settings: Settings) -> RestrictedGitHubProvider | None:
    """Return the configured OAuth provider, or None if OAuth is not set up.

    None (none of the OAuth settings set) leaves the server open; app.py runs it
    unauthenticated with a loud warning. Setting any of them is intent to enable
    auth, so a partial configuration — including an allowlist that parses to
    nothing, or an unusable id allowlist — raises instead of silently running
    open. Whitespace-only values count as unset.
    """
    allowed = _split_csv(settings.MCP_ALLOWED_GITHUB_LOGINS)
    allowed_ids = frozenset(_split_csv(settings.MCP_ALLOWED_GITHUB_IDS))
    required = {
        "MCP_GITHUB_CLIENT_ID": bool(settings.MCP_GITHUB_CLIENT_ID.strip()),
        "MCP_GITHUB_CLIENT_SECRET": bool(settings.MCP_GITHUB_CLIENT_SECRET.strip()),
        "MCP_PUBLIC_URL": bool(settings.MCP_PUBLIC_URL.strip()),
        "MCP_ALLOWED_GITHUB_LOGINS": bool(allowed),
    }
    ids_given = bool(settings.MCP_ALLOWED_GITHUB_IDS.strip())
    # Any non-whitespace value (even an allowlist of only commas) signals intent.
    intent = ids_given or any(
        value.strip()
        for value in (
            settings.MCP_GITHUB_CLIENT_ID,
            settings.MCP_GITHUB_CLIENT_SECRET,
            settings.MCP_PUBLIC_URL,
            settings.MCP_ALLOWED_GITHUB_LOGINS,
        )
    )
    if not intent:
        return None

    missing = [name for name, present in required.items() if not present]
    if missing:
        raise RuntimeError(
            "MCP OAuth is partly configured; refusing to start with the endpoint "
            f"open. Missing or empty: {', '.join(missing)}. Set all of them to "
            "require GitHub login, or unset every MCP OAuth setting to run open."
        )
    if ids_given and not allowed_ids:
        raise RuntimeError("MCP_ALLOWED_GITHUB_IDS is set but lists no ids.")
    non_numeric = sorted(i for i in allowed_ids if not i.isdecimal())
    if non_numeric:
        raise RuntimeError(
            "MCP_ALLOWED_GITHUB_IDS must list numeric GitHub user ids; "
            f"not numeric: {', '.join(non_numeric)}"
        )

    base_url = settings.MCP_PUBLIC_URL.rstrip("/")
    return RestrictedGitHubProvider(
        allowed_logins=allowed,
        allowed_ids=allowed_ids,
        client_id=settings.MCP_GITHUB_CLIENT_ID,
        client_secret=settings.MCP_GITHUB_CLIENT_SECRET,
        # base_url is the public origin: FastMCP serves the OAuth + discovery
        # routes (/.well-known/*, /authorize, /token, /register, /auth/callback)
        # at the root, and the MCP endpoint itself at /mcp. The app must be
        # served at the root (see app.py) for these paths to resolve.
        base_url=base_url,
    )


__all__ = ["RestrictedGitHubProvider", "build_auth"]
