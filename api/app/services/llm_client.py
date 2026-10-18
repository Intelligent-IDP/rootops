"""
RootOps V2 — Ollama LLM Client

Communicates with a local Ollama instance for RAG synthesis.
Used for local development and air-gapped deployments.
"""

from __future__ import annotations

import json
import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def generate_ollama(
    prompt: str,
    system_prompt: str,
    *,
    model: str | None = None,
    temperature: float = 0.3,
    conversation_history: list[dict] | None = None,
) -> str:
    """Generate a response using a local Ollama instance.

    Args:
        prompt: The full RAG prompt (context + query).
        system_prompt: System-level instructions.
        model: Override the configured model name.
        temperature: LLM temperature.

    Returns:
        The LLM's synthesized response string.
    """
    model_name = model or settings.OLLAMA_MODEL

    # Use /api/chat for multi-turn support (history-aware).
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model_name,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": 512,
            "num_ctx": 4096,
        },
    }

    try:
        async with httpx.AsyncClient(
            base_url=settings.OLLAMA_BASE_URL,
            timeout=300.0,
        ) as client:
            response = await client.post("/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
            return data.get("message", {}).get("content", "No response generated.")

    except httpx.ConnectError:
        logger.error(
            "Cannot connect to Ollama at %s — is it running?",
            settings.OLLAMA_BASE_URL,
        )
        return (
            "⚠️ Ollama is not available. Please ensure Ollama is running "
            f"at `{settings.OLLAMA_BASE_URL}` with the `{model_name}` model loaded.\n\n"
            "To start: `ollama run " + model_name + "`"
        )

    except httpx.HTTPStatusError as e:
        logger.error("Ollama API error: %s", e)
        return f"⚠️ LLM error: {e}"

    except Exception as e:
        logger.error("Unexpected LLM error (%s): %s", type(e).__name__, e)
        return f"⚠️ Unexpected error communicating with Ollama: {type(e).__name__}: {e}"


async def generate_ollama_stream(
    prompt: str,
    system_prompt: str,
    *,
    model: str | None = None,
    temperature: float = 0.3,
    conversation_history: list[dict] | None = None,
):
    model_name = model or settings.OLLAMA_MODEL
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model_name,
        "messages": messages,
        "stream": True,
        "options": {"temperature": temperature, "num_predict": 512, "num_ctx": 4096},
    }

    try:
        async with (
            httpx.AsyncClient(base_url=settings.OLLAMA_BASE_URL, timeout=300.0) as client,
            client.stream("POST", "/api/chat", json=payload) as response,
        ):
            response.raise_for_status()
            async for chunk in response.aiter_lines():
                if chunk:
                    data = json.loads(chunk)
                    msg = data.get("message", {}).get("content", "")
                    if msg:
                        yield msg
    except Exception as e:
        logger.error("Ollama streaming error: %s", e)
        yield f"\n\n[Error streaming from Ollama: {e}]"
