"""Vektor free providers — routing only, no keys in code, no paid fallback.
Text providers (direct, separate): GROQ, GEMINI, OPENROUTER, MISTRAL, NVIDIA, CLOUDFLARE, COHERE, ZAI, SAMBANOVA
Image: POLLINATIONS — free image endpoint, no key required for basic use; optional key raises limits.
Image generation in Vektor: YES, via Pollinations image endpoint (text->image URL, no model training, no OpenAI).
Other free image routes (later, on Wallace approval): Cloudflare Workers AI FLUX (10k Neurons/day).
"""
TEXT_PROVIDERS = ["GROQ", "GEMINI", "OPENROUTER", "MISTRAL", "NVIDIA", "CLOUDFLARE", "COHERE", "ZAI", "SAMBANOVA", "POLLINATIONS", "QWEN"]
IMAGE_PROVIDERS = ["POLLINATIONS"]
# Qwen models: served free via Groq (qwen3-32b) and locally via Ollama.
# Fork: QwenLM/Qwen3 (Apache 2.0) — Wallace's fork pending (GitHub connector
# lacks fork permission; he forks manually).
QWEN_GROQ_MODEL = "qwen/qwen3-32b"
QWEN_OLLAMA_MODEL = "qwen2.5:7b"
def image_url(prompt: str, width: int = 1024, height: int = 1024) -> str:
    from urllib.parse import quote
    from vektor_cloud.egress import guard_url
    url = f"https://image.pollinations.ai/prompt/{quote(prompt)}?width={width}&height={height}&nologo=true"
    guard_url(url)  # raises EgressBlocked if not allowlisted
    return url
def text_url(provider: str) -> str:
    from vektor_cloud.egress import guard_url
    url = {
        "GROQ": "https://api.groq.com/openai/v1/chat/completions",
        "OPENROUTER": "https://openrouter.ai/api/v1/chat/completions",
        "GEMINI": "https://generativelanguage.googleapis.com/v1beta/models",
        "POLLINATIONS": "https://text.pollinations.ai/",
        # Qwen via Groq's OpenAI-compatible endpoint (same host, already allowlisted)
        "QWEN": "https://api.groq.com/openai/v1/chat/completions",
    }.get(provider, "")
    if url:
        guard_url(url)  # raises EgressBlocked if not allowlisted
    return url


def qwen_model(via: str = "groq") -> str:
    """Default Qwen model name for the given route."""
    if via == "ollama":
        return QWEN_OLLAMA_MODEL
    return QWEN_GROQ_MODEL
