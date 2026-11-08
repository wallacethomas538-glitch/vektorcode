"""Vektor egress allowlist — network-level protection for Vektor's outbound connections.

Every outbound HTTPS connection Vektor makes MUST pass through guard_url()
before it opens. Hosts not on this list are refused. This is the layer that
stops a tricked or compromised Vektor from exfiltrating data to an attacker's
server: the connection never opens.

The list is hardcoded and unbypassable at runtime. Adding a host requires a
code change + Wallace's approval + a new deploy. There is no env override,
no config file, no runtime toggle — by design.

One exception, mechanism B (universal MCP, 2026-11-07): MCP servers may
add hosts through the Wallace-managed vektor_mcp_hosts table. Those hosts
are additive and MCP-only — guard_url(url, extra_hosts=...) — the hardcoded
list above is never mutated, and web_fetch / the model cascade never see
them. The table is Wallace-only (auth-gated registry endpoints).

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
    "api.mistral.ai",                    # Mistral
    "api.together.xyz",                  # Together AI
    "router.huggingface.co",             # HuggingFace Inference router
    "dashscope.aliyuncs.com",            # Alibaba DashScope (Qwen compat mode)
    "api.llama.com",                     # Meta Llama API
    "openrouter.ai",                     # OpenRouter
    "generativelanguage.googleapis.com",  # Google Gemini
    "text.pollinations.ai",              # Pollinations (text)
    "image.pollinations.ai",             # Pollinations (image)
    "api.meta.ai",                       # Meta (Muse Spark / Image)

    # --- Composio MCP (1000+ app integrations) ---
    "connect.composio.dev",

    # --- Option B web access (2026-11-07, Wallace's order): read-only
    # web search + fetch. No other web hosts are allowlisted. ---
    "github.com",                       # GitHub repos/pages (read)
    "raw.githubusercontent.com",        # Raw file contents (read)
    "api.github.com",                   # GitHub API (read)
    "html.duckduckgo.com",              # DuckDuckGo HTML search scrape

    # --- Wallace's own sites ---
    "gojojoes.com",
    "vektormind-2-0.onrender.com",
    "vektorflow-15xr-1.onrender.com",
    "meta-relay.wallacegee321.workers.dev",  # Cloudflare relay worker

    # --- Local loopback (Ollama, local services) ---
    "localhost",
    "127.0.0.1",
})
# NOTE: api.mistral.ai was added 2026-11-05 (Track A) because VektorCode's
# chat path calls that provider's OpenAI-compatible endpoint. api.openai.com
# and api.cohere.com were removed 2026-11-05 — OpenAI and Cohere have no
# free tier, so their providers were removed from VektorCode entirely
# (Wallace: free-tier models only). api.deepseek.com and other cascade
# candidates remain unverified and NOT on this list — verify the exact API
# hostname first, then add it here with a code change.


class EgressBlocked(Exception):
    """Raised when Vektor attempts a connection to a non-allowlisted host."""
    pass


def load_mcp_hosts(store):
    """Wallace-managed MCP hosts (mechanism B) as a frozenset.

    Reads the vektor_mcp_hosts table through the given store. Never
    raises — on any failure the set is empty and MCP calls fall back
    to the hardcoded allowlist only. Callers cache the result briefly.
    """
    try:
        hosts = store.mcp_host_list() or []
    except Exception:
        return frozenset()
    return frozenset(h for h in (str(x or "").strip().lower()
                                 for x in hosts) if h)


def guard_host(host: str, extra_hosts=None) -> str:
    """Validate a bare hostname against the egress allowlist.

    extra_hosts is an additive set for MCP calls only (Wallace-managed
    vektor_mcp_hosts table). The hardcoded allowlist is never mutated.

    Returns the normalized hostname. Raises EgressBlocked if not allowed.
    """
    h = (host or "").strip().lower()
    if not h:
        raise EgressBlocked("Empty host — refused")
    allowed = VEKTOR_EGRESS_ALLOWLIST | set(extra_hosts or ())
    if h not in allowed:
        raise EgressBlocked(
            f"Host '{h}' is NOT on Vektor's egress allowlist — connection refused. "
            f"Adding a host needs Wallace's approval and a code change."
        )
    return h


def guard_url(url: str, extra_hosts=None):
    """Validate a full URL against the egress allowlist.

    Enforces: https only, no credentials in URL, no fragments, port 443 or
    unset, and host on the allowlist. Returns the parsed URL parts.
    Raises EgressBlocked or ValueError on any violation.

    extra_hosts: additive MCP-only hosts (mechanism B, see guard_host).

    Loopback exception: plain HTTP is allowed for loopback hosts
    (localhost / 127.0.0.1 / ::1) because that traffic never leaves the
    device. This is what lets the phone's local Ollama serve the Termux
    dashboard and CLI. Everything else stays HTTPS-only.
    """
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    loopback = host in ("localhost", "127.0.0.1", "::1")
    if p.scheme != "https" and not (loopback and p.scheme == "http"):
        raise EgressBlocked(f"Only HTTPS allowed — refused: {p.scheme or '(no scheme)'}")
    if p.username is not None or p.password is not None:
        raise EgressBlocked("Credentials in URL — refused")
    if p.fragment:
        raise EgressBlocked("URL fragments not allowed — refused")
    if p.port not in (None, 443) and not loopback:
        raise EgressBlocked(f"Only port 443 allowed — refused: {p.port}")
    guard_host(host, extra_hosts=extra_hosts)
    return p


def is_allowed(host: str, extra_hosts=None) -> bool:
    """Non-raising check: is this host on the egress allowlist?
    extra_hosts: additive MCP-only hosts (mechanism B)."""
    allowed = VEKTOR_EGRESS_ALLOWLIST | set(extra_hosts or ())
    return (host or "").strip().lower() in allowed


def vektor_http(**kwargs):
    """Build a SafeHTTP client locked to Vektor's egress allowlist.

    Any Vektor code that fetches from the network should use this instead
    of constructing SafeHTTP directly — it guarantees the Vektor-wide
    allowlist applies, not just a per-call list.
    """
    from vektor_cloud.vektorbot_http import SafeHTTP
    return SafeHTTP(VEKTOR_EGRESS_ALLOWLIST, **kwargs)
