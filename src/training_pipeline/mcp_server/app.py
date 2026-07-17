"""ASGI entrypoint for the MCP server.

The FastMCP app is served at the root so its OAuth discovery routes
(/.well-known/*, /authorize, /token, /register, /auth/callback) resolve where
Claude.ai expects them; the MCP protocol endpoint sits at /mcp and /health is a
custom route on the server.

Fails closed: if OAuth is not configured (see auth.build_auth) the app is
replaced by a stub that serves /health but answers 503 everywhere else, so a
misconfigured deploy never exposes data unauthenticated.
"""

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from training_pipeline.mcp_server.server import mcp
from training_pipeline.shared.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

_auth_configured = mcp.auth is not None


async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def _unconfigured(request: Request) -> JSONResponse:
    return JSONResponse({"error": "mcp auth not configured"}, status_code=503)


app: Starlette
if _auth_configured:
    app = mcp.http_app(path="/mcp")
else:
    logger.error("mcp_server.app.auth_not_configured")
    _methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]
    app = Starlette(
        routes=[
            Route("/health", _health, methods=["GET"]),
            Route("/{path:path}", _unconfigured, methods=_methods),
        ]
    )

logger.info("mcp_server.app.ready", mcp_auth_configured=_auth_configured)
