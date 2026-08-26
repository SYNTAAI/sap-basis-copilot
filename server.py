"""SAP Basis Copilot — read-only Basis diagnostics over MCP.

Default transport is stdio: the normal deployment is Claude Desktop talking to a
local process on an administrator's own machine, with the JCo bridge on
127.0.0.1. Nothing is exposed to a network in that mode.

    python server.py                        # stdio (default)
    MCP_TRANSPORT=http python server.py     # streamable HTTP on MCP_HOST:MCP_PORT
    MCP_TRANSPORT=http MCP_NO_AUTH=1 ...    # hosted, without OAuth

Hosted mode adds OAuth 2.1 + PKCE unless MCP_NO_AUTH=1.
"""

import logging
import sys

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

from config import Settings
from connectors import ConnectorFactory
from prompts import register_prompts
from tools import register_all_tools

settings = Settings()

# stdio speaks JSON-RPC on stdout: logging must never write there.
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("basis_copilot.server")

_INSTRUCTIONS = (
    "Read-only Basis diagnostics for SAP NetWeaver and S/4HANA. Every tool reads; "
    "none changes anything in SAP. Answers come from the live system at the moment "
    "the question is asked.\n\n"
    "When reporting: name the transaction an administrator would open to act "
    "(SM21, ST22, SM37, SM50, SM12, SM58, SMQ1, SP01, ST06, SM13, STC01, RZ10). "
    "Say plainly when a check returned no data rather than implying it passed."
)

# The public hostname (from SYNTAAI_ISSUER_URL) must be allowed, or the MCP SDK's
# DNS-rebinding protection rejects proxied requests with 421 Misdirected Request.
from urllib.parse import urlparse as _urlparse

_issuer_host = (_urlparse(settings.issuer_url).hostname or "").strip()
_public_hosts, _public_origins = [], []
if _issuer_host and _issuer_host not in ("127.0.0.1", "localhost"):
    _public_hosts = [_issuer_host, f"{_issuer_host}:*"]
    _public_origins = [f"https://{_issuer_host}", f"https://{_issuer_host}:*"]

_TRANSPORT_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *_public_hosts],
    allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                     "https://claude.ai", "https://www.claude.ai",
                     "https://claude.com", "https://www.claude.com",
                     "https://api.anthropic.com", *_public_origins],
)

if settings.auth_disabled:
    if settings.transport != "stdio":
        logger.warning("Running WITHOUT authentication (MCP_NO_AUTH=1)")
    mcp = FastMCP(
        "SAP Basis Copilot",
        instructions=_INSTRUCTIONS,
        host=settings.mcp_host,
        port=settings.mcp_port,
        transport_security=_TRANSPORT_SECURITY,
    )
else:
    from mcp.server.auth.settings import (
        AuthSettings,
        ClientRegistrationOptions,
        RevocationOptions,
    )
    from oauth_provider import SyntaAIOAuthProvider

    oauth_provider = SyntaAIOAuthProvider()
    mcp = FastMCP(
        "SAP Basis Copilot",
        instructions=_INSTRUCTIONS,
        auth_server_provider=oauth_provider,
        auth=AuthSettings(
            issuer_url=settings.issuer_url,
            resource_server_url=settings.issuer_url,
            revocation_options=RevocationOptions(enabled=True),
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=["basis:read"], default_scopes=["basis:read"],
            ),
            required_scopes=["basis:read"],
        ),
        host=settings.mcp_host,
        port=settings.mcp_port,
        transport_security=_TRANSPORT_SECURITY,
    )

connector = ConnectorFactory.create(settings)
register_all_tools(mcp, connector)
register_prompts(mcp)


def main() -> None:
    count = len(mcp._tool_manager.list_tools())
    if settings.transport == "stdio":
        logger.info("SAP Basis Copilot on stdio — %d read-only tools", count)
        mcp.run(transport="stdio")
        return

    import anyio
    import uvicorn
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    app = mcp.streamable_http_app()

    async def health_handler(_request):
        return JSONResponse({"status": "ok", "tools": count})

    app.routes.insert(0, Route("/health", health_handler, methods=["GET"]))

    # OAuth login page (hosted mode only). The provider redirects the browser to
    # /syntaai-login?session=...; without this route that URL 404s.
    if not settings.auth_disabled:
        from oauth_provider import login_page_handler
        app.routes.insert(0, Route("/syntaai-login", login_page_handler, methods=["GET", "POST"]))
        app.state.oauth_provider = oauth_provider

    logger.info("SAP Basis Copilot on http://%s:%s/mcp — %d read-only tools, OAuth %s",
                settings.mcp_host, settings.mcp_port, count,
                "disabled" if settings.auth_disabled else "enabled")
    config = uvicorn.Config(app, host=settings.mcp_host, port=settings.mcp_port,
                            log_level=settings.log_level.lower())
    anyio.run(uvicorn.Server(config).serve)


if __name__ == "__main__":
    main()
