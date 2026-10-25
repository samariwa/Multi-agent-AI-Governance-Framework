"""LLM selection — provider and model are swappable via env vars.

LLM_PROVIDER = ollama | anthropic | openai   (default: whichever is configured)
LLM_MODEL    = model id                      (default per provider below)
LLM_SUBAGENT_MODEL = optional cheaper model for team specialists (default: LLM_MODEL)
OPENAI_REASONING_EFFORT = minimal | low | medium | high (default: low, to keep cost down)
OLLAMA_BASE_URL = where a local Ollama server listens (default: the Docker host)
"""

import os

DEFAULT_MODELS = {
    "ollama": "gemma4:e4b",
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-5",
}


def provider() -> str | None:
    explicit = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return None


def model_name(role: str = "main") -> str | None:
    p = provider()
    main = os.environ.get("LLM_MODEL") or DEFAULT_MODELS.get(p or "")
    return (os.environ.get("LLM_SUBAGENT_MODEL") or main) if role == "sub" else main


def get_llm(role: str = "main"):
    """role="sub" is a team specialist: it only fetches data and reports back,
    so it can run on a cheaper model (LLM_SUBAGENT_MODEL)."""
    p, model = provider(), model_name(role)
    if p == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=model,
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434"),
            temperature=0,
            num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", "16384")),
            # Thinking on: slower, but small models (gemma4:e4b) otherwise tend to
            # announce a next step after a deny without actually calling the tool.
            reasoning=os.environ.get("OLLAMA_REASONING", "true").lower() == "true",
        )
    if p == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, max_tokens=4096)
    if p == "openai":
        from langchain_openai import ChatOpenAI
        # The Responses API: Chat Completions rejects tools + reasoning_effort on gpt-5.x models.
        return ChatOpenAI(model=model, use_responses_api=True,
                          reasoning={"effort": os.environ.get("OPENAI_REASONING_EFFORT", "low")})
    raise RuntimeError("No LLM configured: set LLM_PROVIDER=ollama, or ANTHROPIC_API_KEY / OPENAI_API_KEY")
