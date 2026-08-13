from datetime import datetime, timedelta

import pytz
import dateparser
from dateparser.search import search_dates

from config import TIMEZONE

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def resolve_reminder_time(llm_datetime_str, raw_user_text):
    """Tries the datetime the LLM gave us first, falls back to dateparser on the raw message."""
    tz = pytz.timezone(TIMEZONE)

    if llm_datetime_str:
        try:
            local_dt = tz.localize(datetime.strptime(llm_datetime_str, "%Y-%m-%d %H:%M"))
            return local_dt.astimezone(pytz.utc)
        except ValueError:
            pass

    parse_settings = {
        "PREFER_DATES_FROM": "future",
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
    }

    parsed = dateparser.parse(raw_user_text, settings=parse_settings)
    if parsed:
        return parsed.astimezone(pytz.utc)

    # last resort, scan the sentence for any date-looking phrase. single-word
    # matches like a lone "8am" tend to misfire, so require at least two words
    matches = search_dates(raw_user_text, settings=parse_settings) or []
    good_matches = [(phrase, dt) for phrase, dt in matches if len(phrase.split()) >= 2]
    if good_matches:
        _, best = max(good_matches, key=lambda m: len(m[0].split()))
        return best.astimezone(pytz.utc)

    return None


def compute_next_weekly_run(day_of_week_name, hour, minute):
    """Next UTC timestamp for a weekly recurring task, based on the bot's configured timezone."""
    tz = pytz.timezone(TIMEZONE)
    now_local = datetime.now(tz)

    target_day = WEEKDAYS.index(day_of_week_name.lower())
    days_ahead = (target_day - now_local.weekday()) % 7

    candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
    if candidate <= now_local:
        candidate += timedelta(days=7)

    return candidate.astimezone(pytz.utc)


def compute_next_daily_run(hour, minute):
    tz = pytz.timezone(TIMEZONE)
    now_local = datetime.now(tz)

    candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(days=1)

    return candidate.astimezone(pytz.utc)
