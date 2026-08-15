import json
import time

import requests

from config import OPENROUTER_API_KEY, OPENROUTER_MODEL

API_URL = "https://openrouter.ai/api/v1/chat/completions"

# The free model tier gets rate-limited under normal use, so we retry a couple
# times with a short backoff before giving up.
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 2


class LLMUnavailableError(Exception):
    """Raised when OpenRouter can't be reached or keeps rate-limiting us after retries."""


def chat(messages, reasoning=False, temperature=0.4):
    """Sends a chat request to OpenRouter and returns the raw assistant message dict.

    Raises LLMUnavailableError (instead of letting requests' exceptions propagate)
    if OpenRouter is still unreachable or rate-limited after retries.
    """
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if reasoning:
        payload["reasoning"] = {"enabled": True}

    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                data=json.dumps(payload),
                timeout=30,
            )

            if resp.status_code == 429:
                last_exc = LLMUnavailableError("OpenRouter rate-limited us (429)")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                raise last_exc

            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]

        except requests.exceptions.RequestException as exc:
            last_exc = LLMUnavailableError(f"OpenRouter request failed: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            raise last_exc from exc

    raise last_exc or LLMUnavailableError("OpenRouter request failed for an unknown reason")


def continue_with_reasoning(prior_user_msg, assistant_msg, follow_up_text):
    """
    Follows OpenRouter's pattern for continuing a reasoning trace across turns.
    reasoning_details gets passed back unmodified so the model can pick up where it left off.
    """
    messages = [
        {"role": "user", "content": prior_user_msg},
        {
            "role": "assistant",
            "content": assistant_msg.get("content"),
            "reasoning_details": assistant_msg.get("reasoning_details"),
        },
        {"role": "user", "content": follow_up_text},
    ]
    return chat(messages, reasoning=True)


def ask(system_prompt, user_prompt):
    """One-shot helper for when we just want plain text back, no JSON involved."""
    message = chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )
    return (message.get("content") or "").strip()
