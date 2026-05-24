import re

DEFAULT_HIGHLIGHTS_PROMPT = (
    "You summarise scientific abstracts into journal-style Highlights. "
    "When given an abstract, respond with 3 to 5 bullet points that capture "
    "the novel results of the research and any new methods used during the study. "
    "Each bullet point must be at most 100 characters long (including spaces). "
    "Return only the bullet points, one per line, each starting with a dash (-). "
    "No introduction, no numbering, no trailing commentary. "
    "Use British English spelling throughout (e.g. -ise not -ize, colour not color).\n\n"
    "Rules:\n"
    "- Use the same strength of language as the abstract. If the abstract states "
    "\"significant progress has been achieved\", do not downgrade this to \"shows promise\".\n"
    "- Every bullet must reflect a specific finding, method, or recommendation from the abstract. "
    "Do not write generic statements.\n"
    "- Cover distinct aspects across the bullets. Do not combine separate findings into one bullet.\n"
    "- Do not omit findings that the abstract explicitly flags as key conclusions, "
    "such as cost, turnaround time, or validation requirements."
)


DEFAULT_RECAP_PROMPT = (
    "You are an expert medical and scientific researcher providing a concise briefing "
    "for a busy clinician or scientist who has just returned from a period away. "
    "You will be given a list of recent papers, each with a numeric ID, title, journal "
    "quartile ranking, abstract, and a link to its full record.\n\n"
    "Your task: write a structured briefing of 3–8 paragraphs that highlights the "
    "findings the reader genuinely should not miss. Prioritise findings from Q1 and Q2 "
    "journals, novel methodologies, results that challenge established views, clinically "
    "actionable conclusions, and unusually large or well-designed studies. Do not simply "
    "list every paper — synthesise, group related findings, and prioritise.\n\n"
    "Format rules:\n"
    "- Output valid Markdown.\n"
    "- Refer to authors as a Markdown link using the provided URL: "
    "\"[Smith et al.](url) found that…\", \"[Jones et al.](url) demonstrated…\". "
    "For single-author papers use \"[Brown](url) showed…\".\n"
    "- **Bold** the single most important fact or figure in each paragraph.\n"
    "- Group related findings thematically rather than listing papers one by one.\n"
    "- Begin with the most important or surprising finding.\n"
    "- If a paper has a particularly striking or counter-intuitive result, say so explicitly.\n"
    "- End with a single sentence summarising the overall pattern or the single most "
    "actionable take-away from this set of papers.\n"
    "- Use British English spelling throughout (e.g. -ise not -ize, colour not color).\n"
    "- Do not reproduce paper titles verbatim — name the first author and link, then paraphrase.\n"
    "- Aim for 150–400 words total. Do not exceed 500 words.\n"
    "- If there are no papers to summarise, reply: \"No papers to summarise.\"\n"
    "- Do not include disclaimers, meta-commentary, or notes about your own process."
)


def call_llm(config: dict, system_prompt: str, user_message: str,
             timeout: int | None = None, max_tokens: int = 512) -> str:
    """Call the configured LLM provider. Raises on any error."""
    provider = config.get("llm_provider", "")
    model = (config.get("llm_model") or "").strip()
    if timeout is None:
        timeout = int(config.get("llm_timeout") or 120)

    if not provider:
        raise ValueError("No LLM provider configured")
    if not model:
        raise ValueError("No model name configured")

    if provider == "claude":
        import requests
        api_key = (config.get("llm_api_key") or "").strip()
        if not api_key:
            raise ValueError("Claude API key not configured")
        payload: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user_message}],
        }
        if system_prompt:
            payload["system"] = system_prompt
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]

    elif provider == "chatgpt":
        import requests
        api_key = (config.get("llm_api_key") or "").strip()
        if not api_key:
            raise ValueError("OpenAI API key not configured")
        payload: dict = {
            "model": model,
            "input": user_message,
        }
        if system_prompt:
            payload["instructions"] = system_prompt
        resp = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        msg = next(item for item in data["output"] if item["type"] == "message")
        return msg["content"][0]["text"]

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


def get_provider_config(config: dict, action: str) -> dict | None:
    """
    Return a normalized provider config dict (llm_* keys) for the given LLM action.
    Supports the new multi-provider llm_providers list and the old single-provider
    llm_provider/llm_model/… top-level keys for backward compatibility.
    Returns None if no usable provider is configured.
    """
    providers = config.get("llm_providers") or []

    if not providers:
        # Old single-provider format: top-level llm_* keys
        if config.get("llm_provider"):
            return config
        return None

    action_key = f"llm_action_{action}"
    selected_name = (config.get(action_key) or "").strip()

    provider_data = None
    if selected_name:
        for p in providers:
            if (p.get("name") or "") == selected_name:
                provider_data = p
                break

    if provider_data is None:
        provider_data = providers[0]

    if not provider_data.get("provider"):
        return None

    return {
        "llm_provider": provider_data.get("provider", ""),
        "llm_model": provider_data.get("model", ""),
        "llm_api_key": provider_data.get("api_key", ""),
        "llm_ollama_url": provider_data.get("ollama_url", "http://localhost:11434"),
        "llm_timeout": provider_data.get("timeout", 120),
    }


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
