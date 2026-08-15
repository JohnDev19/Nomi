import json
import logging
import re
from datetime import datetime

import pytz

from config import TIMEZONE, BOT_NAME, BOT_CREATOR
from llm_client import chat, LLMUnavailableError
import db
import local_intent

logger = logging.getLogger(__name__)

BUSY_REPLY = "Medyo busy yung utak ko ngayon (rate limit), pwede mo bang subukan ulit in a bit?"

SYSTEM_PROMPT = """Your name is {bot_name}, a personal assistant bot developed by {bot_creator}.
If the user asks your name, who made you, or anything about your identity, answer in character
as {bot_name} instead of saying you're a generic AI or language model.

You're also the natural-language router for this Telegram bot. Read the user's message and
reply with ONLY a JSON object, nothing else, no markdown fences, no explanation.

A single message can bundle MULTIPLE separate requests together, sometimes with messy grammar
or mixed English/Tagalog ("Taglish"), e.g.:
"Remind me tomorrow at 10am to submit my resume and also meeting now in 12:00 PM, also in Monday,
I have also meeting at 8AM."
That example is THREE separate reminders, not one. Read the whole message carefully, figure out
how many distinct things the user actually wants, and give each one its own action with its own
date/time resolved independently. Don't merge separate requests into one, and don't invent extras
that aren't there. Most messages only have one request — that's fine, just return one action.

Schema:
{{
  "actions": [
    {{
      "intent": "reminder" | "scheduled_task" | "trigger_rule" | "summary" | "memory_save" | "memory_recall" | "undo" | "bulk_delete" | "chat",
      "reminder_text": string or null,
      "reminder_datetime": string or null (format "YYYY-MM-DD HH:MM", resolved from THIS action relative to the current datetime below),
      "task_type": "job_search" | "custom" or null,
      "task_query": string or null,
      "task_day_of_week": "monday".."sunday" or null,
      "task_time": "HH:MM" or null,
      "watch_user": string or null,
      "keyword": string or null,
      "memory_key": string or null,
      "memory_value": string or null,
      "undo_ref": integer or null (a specific action number like "undo #3" -> 3; null means "the most recent action"),
      "bulk_delete_target": "reminders" | "scheduled_tasks" | "trigger_rules" | "memory" or null
    }}
  ],
  "reply": string (one short natural reply covering everything you understood, same language/style the user used, Taglish is fine)
}}

"actions" must always be a list, even when there's only one request in the message.

Current datetime is {now} ({tz}). Use it to resolve relative dates/times per-action, e.g. "tomorrow",
"next Monday", or "now" (meaning right around the current time).

Known facts about this user (personal memory, use if relevant, ignore if empty):
{memory}

Rules:
- "reminder" is for one-time reminders. "scheduled_task" is for recurring things ("every Monday...").
- "trigger_rule" is for watch-and-notify requests, e.g. "if John says urgent, tell me".
- "undo" is for requests to reverse a previous action, e.g. "undo", "undo that", "undo the last change",
  or "undo #3". Set undo_ref to the number if the user gave one, otherwise leave it null (meaning "the
  most recent thing I did").
- "bulk_delete" is for requests to remove MULTIPLE things at once, e.g. "delete all my reminders",
  "clear my recurring tasks", "remove all my keyword alerts", "forget everything about me". Map the
  target to reminders/scheduled_tasks/trigger_rules/memory in bulk_delete_target. This intent never
  deletes anything by itself — it just flags what the user wants cleared, a confirmation step handles
  the rest. Do NOT use "bulk_delete" for removing a single named item (e.g. "delete the reminder about
  the dentist") since there's no support for targeting a single item yet — give that its own action
  with intent "chat" and explain the limitation in "reply".
- If part of the message is just chat with no clear action, give it its own action with intent "chat".
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
    "undo": "undo",
    "undo_action": "undo",
    "bulk_delete": "bulk_delete",
    "delete_all": "bulk_delete",
    "clear_all": "bulk_delete",
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


def _normalize_actions(parsed):
    """
    Always returns a non-empty list of action dicts, each with an "intent" and "reply".

    Accepts either the current {"actions": [...], "reply": ...} shape, or a bare flat
    intent dict (from the tool-call fallback path, or an older/smaller model that ignores
    the "actions" wrapper) — either way the rest of the pipeline only ever deals with a list.
    """
    if isinstance(parsed, dict):
        actions_field = parsed.get("actions")
        if isinstance(actions_field, list) and actions_field:
            shared_reply = parsed.get("reply", "")
            actions = []
            for action in actions_field:
                if not isinstance(action, dict):
                    continue
                action.setdefault("intent", "chat")
                action.setdefault("reply", shared_reply)
                actions.append(action)
            if actions:
                return actions

        if "intent" in parsed:
            parsed.setdefault("reply", "")
            return [parsed]

    return [{"intent": "chat", "reply": "Sige, noted."}]


def parse_intent(chat_id, user_text):
    """Returns a list of one or more action dicts — most messages produce exactly one."""
    # Try the free, local classifier first — covers the well-known phrasings for
    # every structured feature (reminders, scheduled tasks, trigger rules, memory,
    # undo, bulk delete) with zero API calls. It also backs off for anything that
    # looks like several bundled requests, so those always go to the LLM below.
    local_match = local_intent.try_parse(user_text)
    if local_match:
        logger.info("Matched intent '%s' locally, no LLM call needed", local_match["intent"])
        return [local_match]

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

    try:
        message = chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            reasoning=True,
        )
    except LLMUnavailableError as exc:
        logger.warning("OpenRouter unavailable, falling back to chat: %s", exc)
        return [{"intent": "chat", "reply": BUSY_REPLY}]

    content = message.get("content") or ""

    # 1st pass: clean JSON (the happy path)
    parsed = _extract_json(content)

    # 2nd pass: the small LLM emitted tool-call syntax instead of JSON — recover from it
    if not parsed:
        parsed = _extract_from_tool_call(content)

    if not parsed:
        fallback_reply = content.strip() or "Sorry, medyo naguluhan ako dyan, pwede mo bang i-rephrase?"
        return [{"intent": "chat", "reply": fallback_reply}]

    return _normalize_actions(parsed)