import json
import re
from datetime import datetime

import pytz

from config import TIMEZONE, BOT_NAME, BOT_CREATOR
from llm_client import chat
import db

SYSTEM_PROMPT = """Your name is {bot_name}, a personal assistant bot developed by {bot_creator}.
If the user asks your name, who made you, or anything about your identity, answer in character
as {bot_name} instead of saying you're a generic AI or language model.

You're also the natural-language router for this Telegram bot. Read the user's message and
reply with ONLY a JSON object, nothing else, no markdown fences, no explanation.

Schema:
{{
  "intent": "reminder" | "scheduled_task" | "trigger_rule" | "summary" | "memory_save" | "memory_recall" | "chat",
  "reminder_text": string or null,
  "reminder_datetime": string or null (format "YYYY-MM-DD HH:MM", resolved from the message),
  "task_type": "job_search" | "custom" or null,
  "task_query": string or null,
  "task_day_of_week": "monday".."sunday" or null,
  "task_time": "HH:MM" or null,
  "watch_user": string or null,
  "keyword": string or null,
  "memory_key": string or null,
  "memory_value": string or null,
  "reply": string (a short natural reply in the same language/style the user used, Taglish is fine)
}}

Current datetime is {now} ({tz}). Use it to resolve relative dates like "tomorrow" or "next Monday".

Known facts about this user (personal memory, use if relevant, ignore if empty):
{memory}

Rules:
- "reminder" is for one-time reminders. "scheduled_task" is for recurring things ("every Monday...").
- "trigger_rule" is for watch-and-notify requests, e.g. "if John says urgent, tell me".
- If the user just wants to chat or ask something with no clear action, use "chat" and put the real answer in "reply".
- Leave a field null if you're not confident about it, don't guess wildly.
- Never wrap the JSON in backticks.
"""


def _format_memory(rows):
    if not rows:
        return "(none yet)"
    return "\n".join(f"- {r['key']}: {r['value']}" for r in rows)


def _extract_json(text):
    # small free models sometimes still sneak in a bit of extra text, so just grab the {...} chunk
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def parse_intent(chat_id, user_text):
    tz = pytz.timezone(TIMEZONE)
    now_local = datetime.now(tz).strftime("%Y-%m-%d %H:%M (%A)")
    memory_rows = db.list_memory(chat_id)

    system_prompt = SYSTEM_PROMPT.format(
        bot_name=BOT_NAME,
        bot_creator=BOT_CREATOR,
        now=now_local,
        tz=TIMEZONE,
        memory=_format_memory(memory_rows),
    )

    message = chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        reasoning=True,
    )

    content = message.get("content") or ""
    parsed = _extract_json(content)

    if not parsed:
        # model didn't play along, just fall back to treating this as a plain chat turn
        fallback_reply = content.strip() or "Sorry, medyo naguluhan ako dyan, pwede mo bang i-rephrase?"
        return {"intent": "chat", "reply": fallback_reply}

    parsed.setdefault("intent", "chat")
    parsed.setdefault("reply", "")
    return parsed
