import logging
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

import db
from config import BOT_NAME
from intent_parser import parse_intent
from time_utils import resolve_reminder_time, compute_next_weekly_run, compute_next_daily_run
from integrations.jobs import fetch_junior_dev_jobs, format_jobs_message
from llm_client import ask

logger = logging.getLogger(__name__)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# --- basic commands ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_chat.id, update.effective_user.username)
    await update.message.reply_text(
        f"Hey, I'm {BOT_NAME}, your personal assistant. Just talk to me normally, e.g.\n\n"
        '"Remind me tomorrow at 8am to submit my resume"\n'
        '"Every Monday send me 5 junior dev jobs"\n'
        '"If John sends a message with urgent, notify me"\n\n'
        "Type /help for the full command list."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Here's what I can do:\n\n"
        "• Reminders - just tell me what and when\n"
        "• Recurring tasks - \"every Monday, send me...\"\n"
        "• Group summaries - mention me in a group and ask for a summary\n"
        "• Keyword alerts - \"if [name] says [word], notify me\" (set this up from inside the group)\n"
        "• Personal memory - \"remember that I'm allergic to peanuts\"\n\n"
        "Commands:\n"
        "/memory - see what I remember about you\n"
        "/forget <key> - make me forget something\n"
        "/rules - list your active keyword alerts\n"
        "/jobs - pull junior dev listings right now"
    )


async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.list_memory(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("Wala pa akong natatandaan tungkol sa'yo.")
        return

    text = "\n".join(f"• {r['key']}: {r['value']}" for r in rows)
    await update.message.reply_text(f"Here's what I remember:\n\n{text}")


async def forget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /forget <key>")
        return

    key = " ".join(context.args)
    db.delete_memory(update.effective_chat.id, key)
    await update.message.reply_text(f"Okay, nakalimutan ko na yung '{key}'.")


async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.list_trigger_rules(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Wala ka pang active na keyword alerts.")
        return

    lines = [
        f"• watching for \"{r['keyword']}\" from {r['watch_username'] or 'anyone'} in chat {r['group_chat_id']}"
        for r in rows
    ]
    await update.message.reply_text("Your active alerts:\n\n" + "\n".join(lines))


async def jobs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jobs = fetch_junior_dev_jobs()
    await update.message.reply_text(format_jobs_message(jobs))


# --- private chat NLU pipeline ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: Optional[str] = None):
    chat_id = update.effective_chat.id
    text = text or update.message.text

    if update.effective_chat.type == "private":
        db.upsert_user(update.effective_user.id, chat_id, update.effective_user.username)

    intent = parse_intent(chat_id, text)
    kind = intent.get("intent")

    if kind == "reminder":
        await _handle_reminder(update, intent, text)
    elif kind == "scheduled_task":
        await _handle_scheduled_task(update, intent)
    elif kind == "trigger_rule":
        await _handle_trigger_rule(update, intent)
    elif kind == "summary":
        await _handle_summary(update)
    elif kind == "memory_save":
        await _handle_memory_save(update, intent)
    elif kind == "memory_recall":
        await _handle_memory_recall(update, intent)
    else:
        await update.message.reply_text(intent.get("reply") or "Sige, noted.")


async def _handle_reminder(update, intent, raw_text):
    run_at = resolve_reminder_time(intent.get("reminder_datetime"), raw_text)

    if not run_at:
        await update.message.reply_text("Hindi ko ma-figure out yung time, pwede mo bang ulitin nang mas specific?")
        return

    reminder_text = intent.get("reminder_text") or raw_text
    db.add_reminder(update.effective_chat.id, reminder_text, run_at.isoformat())

    local_str = run_at.astimezone().strftime("%b %d, %I:%M %p")
    await update.message.reply_text(f'Got it, I\'ll remind you "{reminder_text}" around {local_str}.')


async def _handle_scheduled_task(update, intent):
    day = intent.get("task_day_of_week")
    time_str = intent.get("task_time") or "09:00"

    try:
        hour, minute = (int(x) for x in time_str.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 9, 0

    task_type = intent.get("task_type") or "custom"
    payload = {"text": intent.get("task_query") or "Scheduled task triggered.", "limit": 5}

    if day and day.lower() in WEEKDAYS:
        day_idx = WEEKDAYS.index(day.lower())
        next_run = compute_next_weekly_run(day.lower(), hour, minute)
    else:
        day_idx = None
        next_run = compute_next_daily_run(hour, minute)

    db.add_scheduled_task(
        update.effective_chat.id, task_type, payload, day_idx, hour, minute, next_run.isoformat()
    )

    when = f"every {day.title()}" if day else "daily"
    await update.message.reply_text(f"Noted, I'll do that {when} at {hour:02d}:{minute:02d}.")


async def _handle_trigger_rule(update, intent):
    keyword = intent.get("keyword")
    if not keyword:
        await update.message.reply_text("Ano yung keyword na babantayan ko?")
        return

    db.add_trigger_rule(
        owner_user_id=update.effective_user.id,
        group_chat_id=update.effective_chat.id,
        watch_username=intent.get("watch_user"),
        keyword=keyword,
    )

    who = intent.get("watch_user") or "anyone"
    await update.message.reply_text(f"Okay, papansinin ko kapag may sinabi si {who} na may '{keyword}'.")


async def _handle_summary(update):
    rows = db.get_today_messages(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("Wala pang messages ngayong araw na ma-summarize.")
        return

    transcript = "\n".join(f"{r['username']}: {r['text']}" for r in rows)
    summary = ask(
        "Summarize the following group chat log into a short set of bullet points, "
        "focused on decisions, deadlines, and anything worth remembering. Keep it brief.",
        transcript,
    )
    await update.message.reply_text(f"Here's today's summary:\n\n{summary}")


async def _handle_memory_save(update, intent):
    key = intent.get("memory_key")
    value = intent.get("memory_value")

    if not key or not value:
        await update.message.reply_text("Ano ba talaga yung gusto mong tandaan ko?")
        return

    db.save_memory(update.effective_chat.id, key, value)
    await update.message.reply_text(f"Naka-save na, tatandaan ko na {key}: {value}")


async def _handle_memory_recall(update, intent):
    key = intent.get("memory_key")
    value = db.get_memory(update.effective_chat.id, key) if key else None

    if not value:
        await update.message.reply_text("Wala akong natatandaan diyan, sorry.")
        return

    await update.message.reply_text(f"{key}: {value}")


# --- group chat handling: logging, keyword triggers, and mention-based NLU ---

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    await _log_and_check_triggers(update, context)

    bot_username = context.bot.username
    mentioned = bool(bot_username) and f"@{bot_username}".lower() in message.text.lower()
    replied_to_bot = (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == context.bot.id
    )

    if mentioned or replied_to_bot:
        cleaned = message.text.replace(f"@{bot_username}", "").strip() if bot_username else message.text
        await handle_message(update, context, text=cleaned)


async def _log_and_check_triggers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat_id = update.effective_chat.id
    username = message.from_user.username or message.from_user.first_name

    db.log_group_message(chat_id, username, message.text)

    for rule in db.get_active_rules_for_group(chat_id):
        watch_user = (rule["watch_username"] or "").lower()
        if watch_user and watch_user not in username.lower():
            continue

        if rule["keyword"] not in message.text.lower():
            continue

        # prefer DMing the person who set the rule, fall back to the group if we don't know them yet
        target_chat = db.get_user_chat_id(rule["owner_user_id"]) or rule["group_chat_id"]
        await context.bot.send_message(
            chat_id=target_chat,
            text=f'🔔 {username} just mentioned "{rule["keyword"]}" in the group:\n\n{message.text}',
        )
