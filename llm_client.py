import json
import time

import requests

from config import OPENROUTER_API_KEY, OPENROUTER_MODEL

API_URL = "https://openrouter.ai/api/v1/chat/completions"

MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 2


class LLMUnavailableError(Exception):


def chat(messages, reasoning=False, temperature=0.4):
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
    message = chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )
    return (message.get("content") or "").strip()