"""
RootOps — Google Gemini LLM Client

Uses the google-genai SDK (official lightweight client).
Requires GEMINI_API_KEY in the environment.
"""

from __future__ import annotations

import logging

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def generate_gemini(
    prompt: str,
    system_prompt: str,
    temperature: float = 0.3,
    conversation_history: list[dict] | None = None,
) -> str:
    """Generate a response using the Google Gemini API."""
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return "⚠️ google-genai package is not installed. Run: pip install google-genai"

    try:
        client = genai.Client(api_key=settings.GEMINI_API_KEY)

        # Build contents: system instruction is separate, then history + user prompt
        contents: list[types.Content] = []

        if conversation_history:
            for msg in conversation_history:
                role = "model" if msg["role"] == "assistant" else "user"
                contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg["content"])]))

        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))

        response = await client.aio.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=temperature,
            ),
        )

        return response.text or "⚠️ Gemini returned an empty response."

    except Exception as e:
        logger.exception("Gemini generation failed")
        return f"⚠️ Gemini error: {e}"


async def generate_gemini_stream(
    prompt: str,
    system_prompt: str,
    temperature: float = 0.3,
    conversation_history: list[dict] | None = None,
):
    """Stream a response from Google Gemini."""
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        yield "⚠️ google-genai package is not installed. Run: pip install google-genai"
        return

    try:
        client = genai.Client(api_key=settings.GEMINI_API_KEY)

        contents: list[types.Content] = []

        if conversation_history:
            for msg in conversation_history:
                role = "model" if msg["role"] == "assistant" else "user"
                contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg["content"])]))

        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))

        for chunk in client.models.generate_content_stream(
            model=settings.GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=temperature,
            ),
        ):
            if chunk.text:
                yield chunk.text

    except Exception as e:
        logger.exception("Gemini streaming failed")
        yield f"⚠️ Gemini error: {e}"
