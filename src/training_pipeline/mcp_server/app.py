"""FastAPI host that mounts the MCP server over SSE.

Exposes:
- GET /health  -> {"status": "ok"}
- /mcp/*       -> FastMCP SSE transport
"""

from fastapi import FastAPI

from training_pipeline.mcp_server.server import mcp
from training_pipeline.shared.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

mcp_app = mcp.http_app(transport="sse")

app = FastAPI(title="personal-training-mcp", lifespan=mcp_app.lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


app.mount("/mcp", mcp_app)

logger.info("mcp_server.app.ready")
