"""Tests for the Vektor egress allowlist — the network-level safety lock."""
import pytest

from vektor_cloud.egress import (
    EgressBlocked,
    VEKTOR_EGRESS_ALLOWLIST,
    guard_host,
    guard_url,
    is_allowed,
    vektor_http,
)


class TestAllowlistContents:
    def test_ai_providers_present(self):
        for host in ("api.groq.com", "openrouter.ai",
                     "generativelanguage.googleapis.com",
                     "text.pollinations.ai", "image.pollinations.ai",
                     "api.meta.ai"):
            assert host in VEKTOR_EGRESS_ALLOWLIST, host

    def test_composio_present(self):
        assert "connect.composio.dev" in VEKTOR_EGRESS_ALLOWLIST

    def test_wallace_sites_present(self):
        for host in ("gojojoes.com", "vektormind-2-0.onrender.com",
                     "vektorflow-15xr-1.onrender.com",
                     "meta-relay.wallacegee321.workers.dev"):
            assert host in VEKTOR_EGRESS_ALLOWLIST, host

    def test_local_present(self):
        assert "localhost" in VEKTOR_EGRESS_ALLOWLIST
        assert "127.0.0.1" in VEKTOR_EGRESS_ALLOWLIST

    def test_unverified_providers_not_present(self):
        # MISTRAL/NVIDIA/COHERE/etc endpoints are unverified — must NOT be here
        # until Wallace approves each exact hostname.
        for host in ("api.mistral.ai", "api.cohere.ai", "api.deepseek.com"):
            assert host not in VEKTOR_EGRESS_ALLOWLIST, host


class TestGuardHost:
    def test_allowed_host_passes(self):
        assert guard_host("api.groq.com") == "api.groq.com"

    def test_case_insensitive(self):
        assert guard_host("API.GROQ.COM") == "api.groq.com"

    def test_blocked_host_raises(self):
        with pytest.raises(EgressBlocked):
            guard_host("evil-attacker.com")

    def test_empty_host_raises(self):
        with pytest.raises(EgressBlocked):
            guard_host("")

    def test_similar_looking_host_blocked(self):
        # api.groq.com.evil.com is NOT api.groq.com
        with pytest.raises(EgressBlocked):
            guard_host("api.groq.com.evil.com")


class TestGuardUrl:
    def test_allowed_url_passes(self):
        p = guard_url("https://api.groq.com/openai/v1/chat/completions")
        assert p.hostname == "api.groq.com"

    def test_http_blocked(self):
        with pytest.raises(EgressBlocked):
            guard_url("http://api.groq.com/openai/v1/chat/completions")

    def test_credentials_blocked(self):
        with pytest.raises(EgressBlocked):
            guard_url("https://user:pass@api.groq.com/")

    def test_fragment_blocked(self):
        with pytest.raises(EgressBlocked):
            guard_url("https://api.groq.com/path#frag")

    def test_non_443_port_blocked(self):
        with pytest.raises(EgressBlocked):
            guard_url("https://api.groq.com:8443/path")

    def test_non_allowlisted_host_blocked(self):
        with pytest.raises(EgressBlocked):
            guard_url("https://data-exfil.evil.com/steal")


class TestIsAllowed:
    def test_allowed(self):
        assert is_allowed("connect.composio.dev") is True

    def test_blocked(self):
        assert is_allowed("random-site.net") is False


class TestVektorHttp:
    def test_uses_vektor_allowlist(self):
        client = vektor_http()
        assert client.allowed_hosts == set(VEKTOR_EGRESS_ALLOWLIST)

    def test_rejects_non_allowlisted_url(self):
        client = vektor_http()
        with pytest.raises(ValueError, match="allowlist"):
            client.request("https://evil.com/x")


class TestProviderWiring:
    def test_text_urls_guarded(self):
        from vektor_cloud.free_providers import text_url
        # Allowlisted providers return URLs
        assert text_url("GROQ").startswith("https://api.groq.com")
        assert text_url("GEMINI").startswith("https://generativelanguage.googleapis.com")
        # Unknown provider returns empty (no crash)
        assert text_url("NOPE") == ""

    def test_image_url_guarded(self):
        from vektor_cloud.free_providers import image_url
        url = image_url("a cat")
        assert "image.pollinations.ai" in url

    def test_composio_url_guarded(self):
        import os
        from vektor_cloud import composio_mcp
        from vektor_cloud.egress import EgressBlocked
        os.environ["COMPOSIO_MCP_URL"] = "https://evil.com/mcp"
        try:
            with pytest.raises(EgressBlocked):
                composio_mcp.mcp_url()
        finally:
            del os.environ["COMPOSIO_MCP_URL"]
