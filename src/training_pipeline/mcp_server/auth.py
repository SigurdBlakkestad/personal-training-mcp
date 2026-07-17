"""OAuth authentication for the MCP server.

Claude.ai custom connectors authenticate over OAuth 2.1 with Dynamic Client
Registration (DCR) and PKCE — a static bearer header is not an option in that
UI. FastMCP's ``GitHubProvider`` is an OAuth proxy that presents the DCR-
compliant interface Claude.ai expects while delegating the actual login to a
GitHub OAuth app.

OAuth only proves *who* logged in; by itself any GitHub account would pass. This
module wraps the provider so only an explicit allowlist of GitHub logins is
accepted — everyone else is rejected at token verification (401). It fails
closed: if the login claim is missing, or the server is misconfigured, access
is denied rather than granted.
"""

from __future__ import annotations

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubProvider

from training_pipeline.shared.config import Settings
from training_pipeline.shared.logging import get_logger

logger = get_logger(__name__)


class RestrictedGitHubProvider(GitHubProvider):
    """GitHubProvider that only admits an allowlist of GitHub logins."""

    def __init__(self, *, allowed_logins: set[str], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._allowed_logins = {login.lower() for login in allowed_logins}

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await super().verify_token(token)
        if access is None:
            return None
        login = (access.claims or {}).get("login")
        if not login or login.lower() not in self._allowed_logins:
            logger.warning("mcp_server.auth.github_login_denied", login=login)
            return None
        return access


def build_auth(settings: Settings) -> RestrictedGitHubProvider | None:
    """Return the configured OAuth provider, or None if OAuth is not set up.

    Returning None leaves the server unauthenticated, so callers that require a
    protected deployment must treat a None here as a hard error (see server.py).
    """
    allowed = {
        login.strip() for login in settings.MCP_ALLOWED_GITHUB_LOGINS.split(",") if login.strip()
    }
    if not (
        settings.MCP_GITHUB_CLIENT_ID
        and settings.MCP_GITHUB_CLIENT_SECRET
        and settings.MCP_PUBLIC_URL
        and allowed
    ):
        return None

    base_url = settings.MCP_PUBLIC_URL.rstrip("/")
    return RestrictedGitHubProvider(
        allowed_logins=allowed,
        client_id=settings.MCP_GITHUB_CLIENT_ID,
        client_secret=settings.MCP_GITHUB_CLIENT_SECRET,
        # base_url is the public origin: FastMCP serves the OAuth + discovery
        # routes (/.well-known/*, /authorize, /token, /register, /auth/callback)
        # at the root, and the MCP endpoint itself at /mcp. The app must be
        # served at the root (see app.py) for these paths to resolve.
        base_url=base_url,
    )


__all__ = ["RestrictedGitHubProvider", "build_auth"]
