import re

DEFAULT_HIGHLIGHTS_PROMPT = (
    "You summarise scientific abstracts into journal-style Highlights. "
    "When given an abstract, respond with 3 to 5 bullet points that capture "
    "the novel results of the research and any new methods used during the study. "
    "Each bullet point must be at most 100 characters long (including spaces). "
    "Return only the bullet points, one per line, each starting with a dash (-). "
    "No introduction, no numbering, no trailing commentary. "
    "Use British English spelling throughout (e.g. -ise not -ize, colour not color)."
)


def call_llm(config: dict, system_prompt: str, user_message: str,
             timeout: int = 60) -> str:
    """Call the configured LLM provider. Raises on any error."""
    provider = config.get("llm_provider", "")
    model = (config.get("llm_model") or "").strip()

    if not provider:
        raise ValueError("No LLM provider configured")
    if not model:
        raise ValueError("No model name configured")

    if provider == "claude":
        import anthropic
        api_key = (config.get("llm_api_key") or "").strip()
        if not api_key:
            raise ValueError("Claude API key not configured")
        client = anthropic.Anthropic(api_key=api_key)
        kwargs: dict = {"model": model, "max_tokens": 512,
                        "messages": [{"role": "user", "content": user_message}]}
        if system_prompt:
            kwargs["system"] = system_prompt
        message = client.messages.create(**kwargs)
        return message.content[0].text

    elif provider == "chatgpt":
        import requests
        api_key = (config.get("llm_api_key") or "").strip()
        if not api_key:
            raise ValueError("OpenAI API key not configured")
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_message})
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"model": model, "messages": messages, "max_tokens": 512},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    elif provider == "ollama":
        import requests
        base_url = (config.get("llm_ollama_url") or "http://localhost:11434").rstrip("/")
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_message})
        resp = requests.post(
            f"{base_url}/api/chat",
            json={"model": model, "messages": messages, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    else:
        raise ValueError(f"Unknown LLM provider: {provider!r}")


def parse_highlights(text: str, max_items: int = 5) -> list:
    """Parse LLM bullet-point response into a clean list of strings."""
    highlights = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^[-•*]\s+", "", line)
        line = re.sub(r"^\d+\.\s+", "", line)
        line = line.strip()
        if line:
            highlights.append(line)
        if len(highlights) >= max_items:
            break
    return highlights
