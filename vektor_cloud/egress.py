"""Vektor egress allowlist — network-level protection for Vektor's outbound connections.

Every outbound HTTPS connection Vektor makes MUST pass through guard_url()
before it opens. Hosts not on this list are refused. This is the layer that
stops a tricked or compromised Vektor from exfiltrating data to an attacker's
server: the connection never opens.

The list is hardcoded and unbypassable at runtime. Adding a host requires a
code change + Wallace's approval + a new deploy. There is no env override,
no config file, no runtime toggle — by design.

Covers: AI providers (free cascade), Composio MCP, Wallace's own sites,
the Meta relay worker, and local loopback.
"""
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# The allowlist. Hostnames only, lowercase, no ports, no schemes.
# ---------------------------------------------------------------------------
VEKTOR_EGRESS_ALLOWLIST = frozenset({
    # --- Free-tier AI providers (Vektor's model cascade) ---
    "api.groq.com",                      # Groq
    "openrouter.ai",                     # OpenRouter
    "generativelanguage.googleapis.com",  # Google Gemini
    "text.pollinations.ai",              # Pollinations (text)
    "image.pollinations.ai",             # Pollinations (image)
    "api.meta.ai",                       # Meta (Muse Spark / Image)

    # --- Composio MCP (1000+ app integrations) ---
    "connect.composio.dev",

    # --- Wallace's own sites ---
    "gojojoes.com",
    "vektormind-2-0.onrender.com",
    "vektorflow-15xr-1.onrender.com",
    "meta-relay.wallacegee321.workers.dev",  # Cloudflare relay worker

    # --- Local loopback (Ollama, local services) ---
    "localhost",
    "127.0.0.1",
})
# NOTE: MISTRAL, NVIDIA, CLOUDFLARE, COHERE, ZAI, SAMBANOVA, DEEPSEEK are
# listed as future cascade providers but their API hosts are NOT on this
# list yet — their endpoints are unverified. When Wallace approves one,
# verify the exact API hostname first, then add it here with a code change.


class EgressBlocked(Exception):
    """Raised when Vektor attempts a connection to a non-allowlisted host."""
    pass


def guard_host(host: str) -> str:
    """Validate a bare hostname against the egress allowlist.

    Returns the normalized hostname. Raises EgressBlocked if not allowed.
    """
    h = (host or "").strip().lower()
    if not h:
        raise EgressBlocked("Empty host — refused")
    if h not in VEKTOR_EGRESS_ALLOWLIST:
        raise EgressBlocked(
            f"Host '{h}' is NOT on Vektor's egress allowlist — connection refused. "
            f"Adding a host needs Wallace's approval and a code change."
        )
    return h


def guard_url(url: str):
    """Validate a full URL against the egress allowlist.

    Enforces: https only, no credentials in URL, no fragments, port 443 or
    unset, and host on the allowlist. Returns the parsed URL parts.
    Raises EgressBlocked or ValueError on any violation.
    """
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if p.scheme != "https":
        raise EgressBlocked(f"Only HTTPS allowed — refused: {p.scheme or '(no scheme)'}")
    if p.username is not None or p.password is not None:
        raise EgressBlocked("Credentials in URL — refused")
    if p.fragment:
        raise EgressBlocked("URL fragments not allowed — refused")
    if p.port not in (None, 443):
        raise EgressBlocked(f"Only port 443 allowed — refused: {p.port}")
    guard_host(host)
    return p


def is_allowed(host: str) -> bool:
    """Non-raising check: is this host on the egress allowlist?"""
    return (host or "").strip().lower() in VEKTOR_EGRESS_ALLOWLIST


def vektor_http(**kwargs):
    """Build a SafeHTTP client locked to Vektor's egress allowlist.

    Any Vektor code that fetches from the network should use this instead
    of constructing SafeHTTP directly — it guarantees the Vektor-wide
    allowlist applies, not just a per-call list.
    """
    from vektor_cloud.vektorbot_http import SafeHTTP
    return SafeHTTP(VEKTOR_EGRESS_ALLOWLIST, **kwargs)
