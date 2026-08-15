import asyncio
import logging
from datetime import datetime
from typing import Optional

import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import db
from config import BOT_NAME, TIMEZONE
from intent_parser import parse_intent
from time_utils import resolve_reminder_time, compute_next_weekly_run, compute_next_daily_run
from integrations.jobs import fetch_junior_dev_jobs, format_jobs_message
from llm_client import ask, LLMUnavailableError

logger = logging.getLogger(__name__)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Bulk-delete confirmations are short-lived and single-instance (see README), so an
# in-memory dict is fine here — no need to round-trip these through Mongo.
# chat_id -> {"target": str, "user_id": int, "count": int}
_pending_bulk_deletes = {}

# Reminders waiting for the user to supply a missing time/date.
# (chat_id, user_id) -> {"text": str}
_pending_reminders = {}

BULK_DELETE_LABELS = {
    "reminders": "reminders",
    "scheduled_tasks": "recurring tasks",
    "trigger_rules": "keyword alerts",
    "memory": "memory entries",
}


# --- helpers ---

def _resolve_notify_chat_id(update) -> int:
    """
    For reminders and scheduled tasks: prefer the user's private DM chat so
    notifications don't fire into the group.  Falls back to the current chat
    if we don't know their DM chat_id yet (they haven't /start-ed the bot in DM).
    """
    private_chat_id = db.get_user_chat_id(update.effective_user.id)
    return private_chat_id if private_chat_id else update.effective_chat.id


def _bot_is_mentioned(message, bot_id: int, bot_username: str) -> bool:
    """
    Returns True when the bot was @mentioned in this group message.

    Checks Telegram's message entities first (authoritative — avoids false
    positives from the bot username appearing inside a word), then falls back
    to a plain-text search in case entities are missing.
    """
    username_lower = (bot_username or "").lower()

    for entity in message.entities or []:
        # @username mention
        if entity.type == "mention" and username_lower:
            slice_ = message.text[entity.offset: entity.offset + entity.length]
            if slice_.lstrip("@").lower() == username_lower:
                return True
        # mention of a user who has no username (rare for bots, but handle it)
        if entity.type == "text_mention" and entity.user and entity.user.id == bot_id:
            return True

    # Plain-text fallback
    if username_lower and f"@{username_lower}" in (message.text or "").lower():
        return True

    return False


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
        "• Reminders — just tell me what and when\n"
        "• Recurring tasks — \"every Monday, send me...\"\n"
        "• Group summaries — @mention me in a group and ask for a summary\n"
        "• Keyword alerts — \"if [name] says [word], notify me\"\n"
        "• Personal memory — \"remember that I'm allergic to peanuts\"\n"
        "• Undo — say \"undo\" to reverse the last thing I did, or \"undo #3\"\n"
        "• Dangerous actions always ask you to confirm first\n\n"
        "Commands:\n"
        "/memory — see what I remember about you\n"
        "/forget <key> — make me forget something\n"
        "/rules — list your active keyword alerts\n"
        "/jobs — pull junior dev listings right now\n"
        "/history — see your recent actions\n"
        "/convmode — toggle conversation (chat) mode on or off\n\n"
        "⚠️ In groups, always @mention me or reply to my message."
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
    chat_id = update.effective_chat.id

    previous = db.get_memory(chat_id, key)
    db.delete_memory(chat_id, key)

    if previous is not None:
        db.log_action(
            chat_id,
            "memory_delete",
            f'Forgot "{key}"',
            undo_data={"type": "memory_delete", "key": key, "previous_value": previous},
        )

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


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.get_action_history(update.effective_chat.id, limit=10)
    if not rows:
        await update.message.reply_text("Wala pang action history dito.")
        return

    tz = pytz.timezone(TIMEZONE)
    lines = ["Action History", ""]
    for r in rows:
        ts_local = datetime.fromisoformat(r["created_at"]).astimezone(tz).strftime("%H:%M")
        marker = " (undone)" if r.get("undone") else ""
        lines.append(f"{r['seq']}. {ts_local} {r['description']}{marker}")

    lines.append("")
    lines.append('Say "undo" for the last one, or "undo #<number>" for a specific one.')
    await update.message.reply_text("\n".join(lines))


async def convmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle conversation (chat) mode on or off for this chat."""
    chat_id = update.effective_chat.id
    current = db.get_chat_setting(chat_id, "conv_mode", default=True)
    new_value = not current
    db.set_chat_setting(chat_id, "conv_mode", new_value)

    if new_value:
        await update.message.reply_text(
            "✅ Conversation mode is ON — I'll reply to casual messages and chat normally."
        )
    else:
        await update.message.reply_text(
            "🔕 Conversation mode is OFF — I'll only handle reminders, tasks, alerts, "
            "and memory. Use /convmode again to turn it back on."
        )


# --- private chat NLU pipeline ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: Optional[str] = None):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    text = text or update.message.text

    if update.effective_chat.type == "private":
        db.upsert_user(user_id, chat_id, update.effective_user.username)

    # --- pending reminder follow-up ---
    # If a previous reminder had no time/date and we asked the user to supply one,
    # treat this message as the answer before running the full NLU pipeline.
    pending_key = (chat_id, user_id)
    if pending_key in _pending_reminders:
        pending = _pending_reminders.pop(pending_key)
        run_at = resolve_reminder_time(None, text)
        if run_at:
            await _complete_pending_reminder(update, pending, run_at)
        else:
            await update.message.reply_text(
                "Hindi ko pa rin ma-figure out ang oras. "
                'Subukan mo ulit mula sa simula, e.g. "remind me to call mom tomorrow at 3pm".'
            )
        return

    # parse_intent does a blocking network call (OpenRouter) plus a Mongo read; running it
    # on the event loop directly would stall the scheduler's tick job for as long as it takes.
    actions = await asyncio.to_thread(parse_intent, chat_id, text)

    for intent in actions:
        await _dispatch_intent(update, intent, text)


async def _dispatch_intent(update, intent, raw_text):
    kind = intent.get("intent")

    # When conversation mode is off, silently drop AI chat replies so
    # they don't steal messages the user meant as reminders.
    if kind == "chat":
        conv_on = db.get_chat_setting(update.effective_chat.id, "conv_mode", default=True)
        if not conv_on:
            return

    if kind == "reminder":
        await _handle_reminder(update, intent, raw_text)
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
    elif kind == "undo":
        await _handle_undo(update, intent)
    elif kind == "bulk_delete":
        await _handle_bulk_delete(update, intent)
    else:
        await update.message.reply_text(intent.get("reply") or "Sige, noted.")


async def _handle_reminder(update, intent, raw_text):
    run_at = resolve_reminder_time(intent.get("reminder_datetime"), raw_text)

    if not run_at:
        # No time/date found — store the reminder text and ask for it.
        reminder_text = intent.get("reminder_text") or raw_text
        pending_key = (update.effective_chat.id, update.effective_user.id if update.effective_user else None)
        _pending_reminders[pending_key] = {"text": reminder_text}
        await update.message.reply_text(
            f'Got it — remind you to "{reminder_text}".\n\n'
            f'When? Reply with the time, like "tomorrow at 3pm" or "Friday at 9am".'
        )
        return

    # If asked from a group, fire the reminder to the user's DM so it doesn't
    # clutter the group feed. Falls back to the current chat if they haven't DM'd
    # the bot yet (no private chat_id on record).
    notify_chat_id = _resolve_notify_chat_id(update)
    current_chat_id = update.effective_chat.id

    reminder_text = intent.get("reminder_text") or raw_text
    reminder_id = db.add_reminder(notify_chat_id, reminder_text, run_at.isoformat())

    db.log_action(
        current_chat_id,
        "reminder",
        f'Created reminder: "{reminder_text}"',
        undo_data={"type": "reminder", "reminder_id": reminder_id},
    )

    tz = pytz.timezone(TIMEZONE)
    local_str = run_at.astimezone(tz).strftime("%b %d, %I:%M %p")
    dm_note = " I'll DM you the reminder." if notify_chat_id != current_chat_id else ""
    await update.message.reply_text(
        f'Got it, I\'ll remind you "{reminder_text}" around {local_str}.{dm_note}'
    )


async def _complete_pending_reminder(update, pending: dict, run_at):
    """Called when the user supplies a time/date for a previously incomplete reminder."""
    notify_chat_id = _resolve_notify_chat_id(update)
    current_chat_id = update.effective_chat.id

    reminder_text = pending["text"]
    reminder_id = db.add_reminder(notify_chat_id, reminder_text, run_at.isoformat())

    db.log_action(
        current_chat_id,
        "reminder",
        f'Created reminder: "{reminder_text}"',
        undo_data={"type": "reminder", "reminder_id": reminder_id},
    )

    tz = pytz.timezone(TIMEZONE)
    local_str = run_at.astimezone(tz).strftime("%b %d, %I:%M %p")
    dm_note = " I'll DM you the reminder." if notify_chat_id != current_chat_id else ""
    await update.message.reply_text(
        f'Done! I\'ll remind you "{reminder_text}" at {local_str}.{dm_note}'
    )


async def _handle_scheduled_task(update, intent):
    day = intent.get("task_day_of_week")
    time_str = intent.get("task_time") or "09:00"

    try:
        hour, minute = (int(x) for x in time_str.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 9, 0

    task_type = intent.get("task_type") or "custom"
    payload = {"text": intent.get("task_query") or "Scheduled task triggered.", "limit": 5}

    # Same as reminders: send recurring task output to DM, not the group.
    notify_chat_id = _resolve_notify_chat_id(update)
    current_chat_id = update.effective_chat.id

    if day and day.lower() in WEEKDAYS:
        day_idx = WEEKDAYS.index(day.lower())
        next_run = compute_next_weekly_run(day.lower(), hour, minute)
        when = f"every {day.title()}"
    else:
        day_idx = None
        next_run = compute_next_daily_run(hour, minute)
        when = "daily"

    task_id = db.add_scheduled_task(
        notify_chat_id, task_type, payload, day_idx, hour, minute, next_run.isoformat()
    )

    db.log_action(
        current_chat_id,
        "scheduled_task",
        f"Created recurring task ({when} at {hour:02d}:{minute:02d})",
        undo_data={"type": "scheduled_task", "task_id": task_id},
    )

    dm_note = " I'll send it to your DM." if notify_chat_id != current_chat_id else ""
    await update.message.reply_text(
        f"Noted, I'll do that {when} at {hour:02d}:{minute:02d}.{dm_note}"
    )


async def _handle_trigger_rule(update, intent):
    keyword = intent.get("keyword")
    if not keyword:
        await update.message.reply_text("Ano yung keyword na babantayan ko?")
        return

    chat_id = update.effective_chat.id
    rule_id = db.add_trigger_rule(
        owner_user_id=update.effective_user.id,
        group_chat_id=chat_id,
        watch_username=intent.get("watch_user"),
        keyword=keyword,
    )

    db.log_action(
        chat_id,
        "trigger_rule",
        f'Created keyword alert for "{keyword}"',
        undo_data={"type": "trigger_rule", "rule_id": rule_id},
    )

    who = intent.get("watch_user") or "anyone"
    await update.message.reply_text(f"Okay, papansinin ko kapag may sinabi si {who} na may '{keyword}'.")


async def _handle_summary(update):
    rows = db.get_today_messages(update.effective_chat.id)
    if not rows:
        await update.message.reply_text(
            "Wala pang messages ngayong araw na ma-summarize.\n\n"
            "⚠️ Make sure Group Privacy is disabled in @BotFather so I can read group messages."
        )
        return

    transcript = "\n".join(f"{r['username']}: {r['text']}" for r in rows)

    try:
        summary = await asyncio.to_thread(
            ask,
            "Summarize the following group chat log into a short set of bullet points, "
            "focused on decisions, deadlines, and anything worth remembering. Keep it brief.",
            transcript,
        )
    except LLMUnavailableError:
        await update.message.reply_text("Medyo busy yung utak ko ngayon, subukan mo ulit in a bit.")
        return

    await update.message.reply_text(f"Here's today's summary:\n\n{summary}")


async def _handle_memory_save(update, intent):
    key = intent.get("memory_key")
    value = intent.get("memory_value")

    if not key or not value:
        await update.message.reply_text("Ano ba talaga yung gusto mong tandaan ko?")
        return

    chat_id = update.effective_chat.id
    previous = db.get_memory(chat_id, key)
    db.save_memory(chat_id, key, value)

    db.log_action(
        chat_id,
        "memory_save",
        f"Saved memory: {key} = {value}",
        undo_data={"type": "memory_save", "key": key, "previous_value": previous},
    )

    await update.message.reply_text(f"Naka-save na, tatandaan ko na {key}: {value}")


async def _handle_memory_recall(update, intent):
    key = intent.get("memory_key")

    if key:
        value = db.get_memory(update.effective_chat.id, key)
        if not value:
            await update.message.reply_text(f"Wala akong natatandaan tungkol sa '{key}', sorry.")
            return
        await update.message.reply_text(f"{key}: {value}")
    else:
        rows = db.list_memory(update.effective_chat.id)
        if not rows:
            await update.message.reply_text("Wala pa akong natatandaan tungkol sa'yo.")
            return
        text = "\n".join(f"• {r['key']}: {r['value']}" for r in rows)
        await update.message.reply_text(f"Here's everything I remember about you:\n\n{text}")


# --- undo ---

async def _handle_undo(update, intent):
    chat_id = update.effective_chat.id
    ref = intent.get("undo_ref")

    if ref not in (None, ""):
        try:
            ref = int(ref)
        except (TypeError, ValueError):
            ref = None

    action = db.get_action_by_seq(chat_id, ref) if ref else db.get_last_undoable_action(chat_id)

    if not action:
        await update.message.reply_text("Wala akong makitang action na pwedeng i-undo.")
        return

    if action.get("undone"):
        await update.message.reply_text(f"Na-undo ko na yung #{action['seq']} dati.")
        return

    ok, message = _reverse_action(action)
    if ok:
        db.mark_action_undone(action["_id"])

    await update.message.reply_text(message)


def _reverse_action(action):
    """Applies the reverse of a logged action. Returns (success, reply_text)."""
    data = action.get("undo_data") or {}
    kind = data.get("type")
    chat_id = action["chat_id"]

    if kind == "reminder":
        removed = db.delete_reminder_if_unsent(data["reminder_id"])
        if removed:
            return True, f"Undone #{action['seq']}: {action['description']}"
        return False, "Hindi ko na ma-undo yun, baka na-send na yung reminder."

    if kind == "scheduled_task":
        db.deactivate_scheduled_task(data["task_id"])
        return True, f"Undone #{action['seq']}: {action['description']}"

    if kind == "trigger_rule":
        db.deactivate_trigger_rule(data["rule_id"])
        return True, f"Undone #{action['seq']}: {action['description']}"

    if kind in ("memory_save", "memory_delete"):
        db.restore_memory(chat_id, data["key"], data.get("previous_value"))
        return True, f"Undone #{action['seq']}: {action['description']}"

    if kind == "bulk_delete":
        restorers = {
            "reminders": db.restore_reminders,
            "scheduled_tasks": db.restore_scheduled_tasks,
            "trigger_rules": db.restore_trigger_rules,
            "memory": db.restore_memory_bulk,
        }
        restorer = restorers.get(data.get("target"))
        if not restorer:
            return False, "Hindi ko ma-undo yun."
        restorer(data.get("docs") or [])
        return True, f"Undone #{action['seq']}: {action['description']}"

    return False, "Hindi ko alam kung paano i-undo yun."


# --- bulk delete ---

async def _handle_bulk_delete(update, intent):
    target = intent.get("bulk_delete_target")
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    counters = {
        "reminders": lambda: db.count_reminders(chat_id),
        "scheduled_tasks": lambda: db.count_scheduled_tasks(chat_id),
        "trigger_rules": lambda: db.count_trigger_rules(user_id),
        "memory": lambda: db.count_memory(chat_id),
    }

    if target not in counters:
        await update.message.reply_text(
            "Hindi ko sure kung ano lahat yung gusto mong i-delete, pwede mo bang linawin?"
        )
        return

    count = counters[target]()
    if count == 0:
        await update.message.reply_text(f"Wala ka namang {BULK_DELETE_LABELS[target]} ngayon.")
        return

    _pending_bulk_deletes[chat_id] = {"target": target, "user_id": user_id, "count": count}

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Delete", callback_data=f"bulkdel:confirm:{chat_id}"),
        InlineKeyboardButton("Cancel", callback_data=f"bulkdel:cancel:{chat_id}"),
    ]])
    await update.message.reply_text(
        f"This will delete {count} {BULK_DELETE_LABELS[target]}.\nAre you sure?",
        reply_markup=keyboard,
    )


async def bulk_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = (query.data or "").split(":")
    if len(parts) != 3:
        return

    _, action, chat_id_str = parts
    try:
        chat_id = int(chat_id_str)
    except ValueError:
        return

    pending = _pending_bulk_deletes.get(chat_id)
    if not pending:
        await query.edit_message_text("Wala nang pending na action, baka na-expire na. Try mo ulit.")
        return

    if query.from_user.id != pending["user_id"]:
        await query.answer("Ikaw lang na nag-request nito ang pwedeng mag-confirm.", show_alert=True)
        return

    if action == "cancel":
        del _pending_bulk_deletes[chat_id]
        await query.edit_message_text("Okay, kinansela ko na. Walang na-delete.")
        return

    target = pending["target"]
    deleters = {
        "reminders": lambda: db.delete_all_reminders(chat_id),
        "scheduled_tasks": lambda: db.delete_all_scheduled_tasks(chat_id),
        "trigger_rules": lambda: db.delete_all_trigger_rules(pending["user_id"]),
        "memory": lambda: db.delete_all_memory(chat_id),
    }

    docs = deleters[target]()
    del _pending_bulk_deletes[chat_id]

    db.log_action(
        chat_id,
        "bulk_delete",
        f"Deleted {len(docs)} {BULK_DELETE_LABELS[target]}",
        undo_data={"type": "bulk_delete", "target": target, "docs": docs},
    )

    await query.edit_message_text(
        f'Done, deleted {len(docs)} {BULK_DELETE_LABELS[target]}. Say "undo" if that was a mistake.'
    )


# --- group chat handling ---

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    await _log_and_check_triggers(update, context)

    bot_id = context.bot.id
    bot_username = context.bot.username  # no @ prefix

    mentioned = _bot_is_mentioned(message, bot_id, bot_username)
    replied_to_bot = (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_id
    )

    if mentioned or replied_to_bot:
        # Persist username without overwriting an existing private chat_id.
        user = update.effective_user
        existing_chat_id = db.get_user_chat_id(user.id)
        db.upsert_user(user.id, existing_chat_id, user.username)

        # Strip the @mention from the text so the NLU only sees the actual request.
        cleaned = message.text
        if bot_username:
            cleaned = cleaned.replace(f"@{bot_username}", "").strip()

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

        target_chat = db.get_user_chat_id(rule["owner_user_id"]) or rule["group_chat_id"]
        await context.bot.send_message(
            chat_id=target_chat,
            text=f'🔔 {username} just mentioned "{rule["keyword"]}" in the group:\n\n{message.text}',
        )