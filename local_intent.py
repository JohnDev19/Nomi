"""
Local, LLM-free intent classification for the bot's core automated features.
"""

import re

from dateparser.search import search_dates

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

TIME_TOKEN = re.compile(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", re.IGNORECASE)
MULTI_REQUEST_MARKERS = re.compile(r"\balso\b|\band then\b|\bafter that\b", re.IGNORECASE)
WEEKDAY_MENTION = re.compile(r"\b(" + "|".join(WEEKDAYS) + r")\b", re.IGNORECASE)


def _looks_like_multiple_requests(text):
    """True if the message probably bundles more than one request together."""
    time_tokens = TIME_TOKEN.findall(text)
    if len(time_tokens) >= 2:
        return True
    if MULTI_REQUEST_MARKERS.search(text) and (time_tokens or WEEKDAY_MENTION.search(text)):
        return True
    return False


REMINDER_TRIGGER = re.compile(r"^(remind me|paalalahanan mo ako|paalala mo)\b", re.IGNORECASE)

SCHEDULED_TRIGGER = re.compile(
    r"\bevery\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|day|weekday)s?\b",
    re.IGNORECASE,
)
TIME_WITH_MERIDIEM = re.compile(r"\b(\d{1,2})(:(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
SEND_ME_QUERY = re.compile(r"\bsend me\s+(.+)$", re.IGNORECASE)

TRIGGER_RULE_PATTERN = re.compile(
    r"^if\s+(?P<who>\S+)\s+(?:says?|sends?|texts?|mentions?)\s+"
    r"(?:a message with\s+)?['\"]?(?P<keyword>[\w\s]+?)['\"]?[,]?\s*(?:then\s+)?notify me\b",
    re.IGNORECASE,
)

MEMORY_SAVE_PATTERN = re.compile(r"^(?:please\s+)?remember (?:that\s+)?(?P<fact>.+)$", re.IGNORECASE)
MEMORY_RECALL_ALL_PATTERN = re.compile(
    r"\b(what do you remember|what do you know about me|list (my )?memory|show (my )?memory)\b",
    re.IGNORECASE,
)
MEMORY_RECALL_KEY_PATTERN = re.compile(
    r"\bdo you remember\s+(?:my\s+|about\s+)?(?P<key>.+?)\??$", re.IGNORECASE
)

UNDO_PATTERN = re.compile(r"^undo\b(?:\s*#?\s*(?P<ref>\d+))?\s*(that|it|the last (change|thing|action))?$",
                           re.IGNORECASE)

BULK_DELETE_PATTERN = re.compile(
    r"\b(delete|remove|clear)\s+all\s+(of\s+)?(my\s+)?"
    r"(?P<target>reminders|recurring tasks|scheduled tasks|keyword alerts|alerts|memory|memories)\b",
    re.IGNORECASE,
)
FORGET_EVERYTHING_PATTERN = re.compile(r"\bforget everything\b", re.IGNORECASE)

_BULK_TARGET_MAP = {
    "reminders": "reminders",
    "recurring tasks": "scheduled_tasks",
    "scheduled tasks": "scheduled_tasks",
    "keyword alerts": "trigger_rules",
    "alerts": "trigger_rules",
    "memory": "memory",
    "memories": "memory",
}


def _strip_date_phrases(text):
    """Removes date/time phrases dateparser can find, leaving (hopefully) just the task."""
    matches = search_dates(text, settings={"PREFER_DATES_FROM": "future"}) or []
    cleaned = text
    for phrase, _ in matches:
        cleaned = cleaned.replace(phrase, "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    cleaned = re.sub(r"^(to|na|para)\s+", "", cleaned, flags=re.IGNORECASE)
    return cleaned or None


def _slugify(fact, max_words=4):
    words = re.findall(r"[\w']+", fact.lower())
    return "_".join(words[:max_words]) or "note"


def try_parse(text):
    """Returns an intent dict for a recognized command pattern, or None if nothing matched."""
    stripped = (text or "").strip()
    if not stripped:
        return None

    if _looks_like_multiple_requests(stripped):
        return None

    # --- undo ---
    m = UNDO_PATTERN.match(stripped)
    if m:
        ref = m.group("ref")
        return {"intent": "undo", "undo_ref": int(ref) if ref else None, "reply": ""}

    # --- bulk delete (always goes through the confirm/cancel flow, so a false-positive
    #     match here is safe — nothing gets deleted until the user taps Delete) ---
    m = BULK_DELETE_PATTERN.search(stripped)
    if m:
        target = _BULK_TARGET_MAP.get(m.group("target").lower())
        if target:
            return {"intent": "bulk_delete", "bulk_delete_target": target, "reply": ""}

    if FORGET_EVERYTHING_PATTERN.search(stripped):
        return {"intent": "bulk_delete", "bulk_delete_target": "memory", "reply": ""}

    # --- trigger rule ---
    m = TRIGGER_RULE_PATTERN.match(stripped)
    if m:
        return {
            "intent": "trigger_rule",
            "watch_user": m.group("who").lstrip("@"),
            "keyword": m.group("keyword").strip().lower(),
            "reply": "",
        }

    # --- scheduled task ("every Monday...") ---
    m = SCHEDULED_TRIGGER.search(stripped)
    if m:
        day = m.group(1).lower()
        hour, minute = 9, 0
        time_match = TIME_WITH_MERIDIEM.search(stripped)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(3) or 0)
            meridiem = time_match.group(4).lower()
            if meridiem == "pm" and hour != 12:
                hour += 12
            if meridiem == "am" and hour == 12:
                hour = 0

        query_match = SEND_ME_QUERY.search(stripped)
        task_query = query_match.group(1).strip() if query_match else stripped
        return {
            "intent": "scheduled_task",
            "task_type": "job_search" if "job" in stripped.lower() else "custom",
            "task_query": task_query,
            "task_day_of_week": day if day in WEEKDAYS else None,
            "task_time": f"{hour:02d}:{minute:02d}",
            "reply": "",
        }

    # --- one-time reminder ---
    if REMINDER_TRIGGER.match(stripped):
        remainder = REMINDER_TRIGGER.sub("", stripped).strip()
        return {
            "intent": "reminder",
            "reminder_text": _strip_date_phrases(remainder),
            "reminder_datetime": None,  # time_utils' dateparser fallback resolves this from raw text
            "reply": "",
        }

    # --- memory recall ---
    if MEMORY_RECALL_ALL_PATTERN.search(stripped):
        return {"intent": "memory_recall", "memory_key": None, "reply": ""}

    m = MEMORY_RECALL_KEY_PATTERN.search(stripped)
    if m:
        return {"intent": "memory_recall", "memory_key": m.group("key").strip(), "reply": ""}

    # --- memory save ---
    m = MEMORY_SAVE_PATTERN.match(stripped)
    if m:
        fact = m.group("fact").strip()
        if fact:
            return {
                "intent": "memory_save",
                "memory_key": _slugify(fact),
                "memory_value": fact,
                "reply": "",
            }

    return None