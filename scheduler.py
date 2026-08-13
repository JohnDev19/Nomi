import logging

import db
from time_utils import compute_next_weekly_run, compute_next_daily_run
from integrations.jobs import fetch_junior_dev_jobs, format_jobs_message

logger = logging.getLogger(__name__)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


async def tick(context):
    """Runs on a timer via the bot's job queue, checks what's due and fires it off."""
    bot = context.bot
    await _handle_reminders(bot)
    await _handle_scheduled_tasks(bot)


async def _handle_reminders(bot):
    for row in db.get_due_reminders():
        try:
            await bot.send_message(chat_id=row["chat_id"], text=f"⏰ Reminder: {row['text']}")
        except Exception:
            logger.exception("couldn't deliver reminder %s", row["_id"])
        finally:
            # mark it done either way, retrying forever on a dead chat isn't useful
            db.mark_reminder_sent(row["_id"])


async def _handle_scheduled_tasks(bot):
    for row in db.get_due_scheduled_tasks():
        payload = row["payload"] or {}

        try:
            if row["task_type"] == "job_search":
                jobs = fetch_junior_dev_jobs(limit=payload.get("limit", 5))
                text = format_jobs_message(jobs)
            elif row["task_type"] == "custom":
                text = payload.get("text", "Scheduled task triggered.")
            else:
                text = "Scheduled task triggered."

            await bot.send_message(chat_id=row["chat_id"], text=text)
        except Exception:
            logger.exception("scheduled task %s blew up", row["_id"])

        _reschedule(row)


def _reschedule(row):
    if row["day_of_week"] is not None:
        weekday_name = WEEKDAYS[row["day_of_week"]]
        next_run = compute_next_weekly_run(weekday_name, row["hour"], row["minute"])
    else:
        next_run = compute_next_daily_run(row["hour"], row["minute"])

    db.update_next_run(row["_id"], next_run.isoformat())
