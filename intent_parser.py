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

# Maps tool-call function names the small LLM sometimes emits to our flat intent schema.
_TOOL_CALL_INTENT_MAP = {
    "memory_save": "memory_save",
    "save_memory": "memory_save",
    "memory_recall": "memory_recall",
    "recall_memory": "memory_recall",
    "get_memory": "memory_recall",
    "add_reminder": "reminder",
    "set_reminder": "reminder",
    "create_reminder": "reminder",
    "add_scheduled_task": "scheduled_task",
    "create_task": "scheduled_task",
    "schedule_task": "scheduled_task",
    "add_trigger_rule": "trigger_rule",
    "watch_keyword": "trigger_rule",
    "summarize": "summary",
    "get_summary": "summary",
}


def _format_memory(rows):
    if not rows:
        return "(none yet)"
    return "\n".join(f"- {r['key']}: {r['value']}" for r in rows)


def _extract_json(text):
    """Grab the first {...} block and try to parse it as JSON."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _extract_from_tool_call(text):
    """
    Recover intent from tool-call syntax that small models sometimes emit instead of plain JSON.

    Example input:
        <|tool_call_start|>[memory_save(memory_key='work_pref', memory_value='mornings')]<|tool_call_end|>

    Returns the equivalent flat dict our handlers expect, or None if the text doesn't
    match the expected pattern or the function name isn't one we recognise.
    """
    match = re.search(
        r"<\|tool_call_start\|>\s*\[(\w+)\((.*?)\)\]\s*<\|tool_call_end\|>",
        text,
        re.DOTALL,
    )
    if not match:
        return None

    func_name = match.group(1)
    intent = _TOOL_CALL_INTENT_MAP.get(func_name)
    if not intent:
        return None

    args_str = match.group(2)
    args = {}
    # Handles both single- and double-quoted values: key='val' or key="val"
    for m in re.finditer(r"(\w+)=['\"]([^'\"]*)['\"]", args_str):
        args[m.group(1)] = m.group(2)

    result = {"intent": intent, "reply": ""}
    result.update(args)
    return result


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

    # 1st pass: clean JSON (the happy path)
    parsed = _extract_json(content)

    # 2nd pass: the small LLM emitted tool-call syntax instead of JSON — recover from it
    if not parsed:
        parsed = _extract_from_tool_call(content)

    if not parsed:
        fallback_reply = content.strip() or "Sorry, medyo naguluhan ako dyan, pwede mo bang i-rephrase?"
        return {"intent": "chat", "reply": fallback_reply}

    parsed.setdefault("intent", "chat")
    parsed.setdefault("reply", "")
    return parsed