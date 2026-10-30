"""Composio MCP tool layer — config only, no key in code.
Set COMPOSIO_API_KEY + COMPOSIO_MCP_URL in env on the host.
No tool is called here until Wallace approves the exact job.
The MCP URL is guarded by the Vektor egress allowlist (egress.py)."""
import os


def mcp_url() -> str:
    """Return the configured MCP URL, guarded by the egress allowlist."""
    from vektor_cloud.egress import guard_url
    url = os.environ.get("COMPOSIO_MCP_URL", "")
    if not url:
        return ""
    guard_url(url)  # raises EgressBlocked if not allowlisted
    return url


def configured() -> bool:
    return bool(os.environ.get("COMPOSIO_API_KEY") and mcp_url())
