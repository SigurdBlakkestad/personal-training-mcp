"""FastAPI host that mounts the MCP server over streamable HTTP.

Exposes:
- GET /health  -> {"status": "ok"}
- /mcp         -> FastMCP streamable-HTTP transport (single endpoint, what
                  Claude.ai's custom connectors expect)
"""

from fastapi import FastAPI

from training_pipeline.mcp_server.auth import BearerAuthMiddleware
from training_pipeline.mcp_server.server import mcp
from training_pipeline.shared.config import get_settings
from training_pipeline.shared.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

mcp_app = mcp.http_app(path="/", transport="http")

app = FastAPI(title="personal-training-mcp", lifespan=mcp_app.lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


app.mount("/mcp", mcp_app)

# Gate the MCP transport behind a shared bearer token. /health stays open.
app.add_middleware(BearerAuthMiddleware, token=get_settings().MCP_AUTH_TOKEN)

logger.info("mcp_server.app.ready", mcp_auth_configured=bool(get_settings().MCP_AUTH_TOKEN))
