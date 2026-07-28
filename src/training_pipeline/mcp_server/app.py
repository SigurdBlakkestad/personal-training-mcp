"""ASGI entrypoint for the MCP server.

The FastMCP app is served at the root so its OAuth discovery routes
(/.well-known/*, /authorize, /token, /register, /auth/callback) resolve where
Claude.ai expects them; the MCP protocol endpoint sits at /mcp and /health is a
custom route on the server.

Auth is opt-in by configuration: when the four MCP_GITHUB_*/MCP_PUBLIC_URL/
MCP_ALLOWED_GITHUB_LOGINS settings are present (see auth.build_auth) the server
requires GitHub OAuth login; when they are absent it runs OPEN and logs a loud
warning on every boot. Running open is deliberate so the connector keeps
working until the operator chooses to turn auth on — it is not a silent default.
"""

from starlette.applications import Starlette

from training_pipeline.mcp_server.server import mcp
from training_pipeline.shared.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

_auth_configured = mcp.auth is not None

app: Starlette = mcp.http_app(path="/mcp")

if _auth_configured:
    logger.info("mcp_server.app.ready", mcp_auth_configured=True)
else:
    logger.warning(
        "mcp_server.app.ready_unauthenticated",
        detail=(
            "MCP endpoint is OPEN — no OAuth configured. Anyone with the URL can "
            "read and write your data. Set MCP_GITHUB_CLIENT_ID, "
            "MCP_GITHUB_CLIENT_SECRET, MCP_PUBLIC_URL and MCP_ALLOWED_GITHUB_LOGINS "
            "to require GitHub login."
        ),
    )
