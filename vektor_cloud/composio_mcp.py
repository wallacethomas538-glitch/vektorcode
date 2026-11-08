"""Composio MCP client — thin shim over the generic MCP client.

Kept so the existing call sites (app.call_mcp_tool, app.mcp_status)
and the historical tests keep working. The protocol lives in
vektor_cloud.mcp_client now; this module only builds the Composio
config from the environment (URL + key) and delegates.
"""
import os

from vektor_cloud import mcp_client
from vektor_cloud.egress import EgressBlocked

PROTOCOL_VERSION = mcp_client.PROTOCOL_VERSION
STATUS_TTL = mcp_client.STATUS_TTL

MCP_URL_ENV = "COMPOSIO_MCP_URL"
MCP_KEY_ENV = "COMPOSIO_API_KEY"


class MCPError(mcp_client.MCPError):
    """Kept for import compatibility."""


def _url():
    return (os.environ.get(MCP_URL_ENV)
            or os.environ.get("MCP_SERVER_URL") or "").strip()


def _composio_config():
    """Config for the Composio server, from env at call time. No
    registry hosts needed: connect.composio.dev is on the hardcoded
    egress allowlist (the app passes registry hosts to its own configs)."""
    return mcp_client.MCPServerConfig(
        id="composio",
        name="Composio",
        url=_url(),
        transport="streamable-http",
        auth_kind="header",
        auth_header="x-consumer-api-key",
        auth_env=MCP_KEY_ENV,
        enabled=True,
        expose_to_model=True,
        url_env=MCP_URL_ENV,
    )


def mcp_url():
    """Configured Composio URL, guarded by the egress allowlist."""
    return mcp_client.mcp_url(_composio_config())


def configured():
    """True when the Composio URL and key are both set. No network."""
    return mcp_client.configured(_composio_config())


def _headers(session_id=None):
    return mcp_client._headers(_composio_config(), session_id)


def _post(payload, session_id=None, timeout=20):
    return mcp_client._post_http(_composio_config(), payload,
                                 session_id=session_id, timeout=timeout)


def list_tools(timeout=10, force=False):
    """Tool list for the Composio server, cached briefly."""
    return mcp_client.list_tools(_composio_config(),
                                 timeout=timeout, force=force)


def call_tool(tool_name, arguments):
    """Call one Composio tool. Returns {'ok': bool, ...}."""
    return mcp_client.call_tool(_composio_config(), tool_name, arguments)


def status(force=False):
    """Read-only handshake result for the Composio server."""
    return mcp_client.status(_composio_config(), force=force)


def reset():
    """Drop the Composio session and caches (tests, auth changes)."""
    mcp_client.reset_server("composio")


def reset_session():
    """Alias kept for the historical tests."""
    reset()


__all__ = [
    "MCPError", "EgressBlocked", "PROTOCOL_VERSION", "STATUS_TTL",
    "MCP_URL_ENV", "MCP_KEY_ENV", "mcp_url", "configured", "_headers",
    "_post", "list_tools", "call_tool", "status", "reset", "reset_session",
]
