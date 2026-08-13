from datetime import datetime, timezone

from pymongo import MongoClient, ASCENDING
from pymongo.errors import PyMongoError

from config import MONGODB_URI, MONGODB_DB_NAME

_client = MongoClient(MONGODB_URI)
_db = _client[MONGODB_DB_NAME]

reminders_col = _db.reminders
scheduled_tasks_col = _db.scheduled_tasks
trigger_rules_col = _db.trigger_rules
memory_col = _db.memory
group_messages_col = _db.group_messages
users_col = _db.users


def init_db():
    """Sets up the indexes we rely on. Collections themselves get created lazily by mongo."""
    try:
        memory_col.create_index([("chat_id", ASCENDING), ("key", ASCENDING)], unique=True)
        trigger_rules_col.create_index([("group_chat_id", ASCENDING), ("active", ASCENDING)])
        reminders_col.create_index([("sent", ASCENDING), ("run_at", ASCENDING)])
        scheduled_tasks_col.create_index([("active", ASCENDING), ("next_run", ASCENDING)])
        group_messages_col.create_index([("chat_id", ASCENDING), ("ts", ASCENDING)])
    except PyMongoError as exc:
        raise RuntimeError(
            "Couldn't reach MongoDB. Double-check MONGODB_URI/MONGODB_PASSWORD, and make sure "
            "your current IP is allowed in the Atlas network access list."
        ) from exc


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# --- reminders ---

def add_reminder(chat_id, text, run_at_iso):
    reminders_col.insert_one({
        "chat_id": chat_id,
        "text": text,
        "run_at": run_at_iso,
        "sent": False,
        "created_at": now_iso(),
    })


def get_due_reminders():
    return list(reminders_col.find({"sent": False, "run_at": {"$lte": now_iso()}}))


def mark_reminder_sent(reminder_id):
    reminders_col.update_one({"_id": reminder_id}, {"$set": {"sent": True}})


# --- scheduled tasks ---

def add_scheduled_task(chat_id, task_type, payload, day_of_week, hour, minute, next_run_iso):
    scheduled_tasks_col.insert_one({
        "chat_id": chat_id,
        "task_type": task_type,
        "payload": payload or {},
        "day_of_week": day_of_week,
        "hour": hour,
        "minute": minute,
        "next_run": next_run_iso,
        "active": True,
    })


def get_due_scheduled_tasks():
    return list(scheduled_tasks_col.find({"active": True, "next_run": {"$lte": now_iso()}}))


def update_next_run(task_id, next_run_iso):
    scheduled_tasks_col.update_one({"_id": task_id}, {"$set": {"next_run": next_run_iso}})


def list_scheduled_tasks(chat_id):
    return list(scheduled_tasks_col.find({"chat_id": chat_id, "active": True}))


# --- trigger rules ---

def add_trigger_rule(owner_user_id, group_chat_id, watch_username, keyword):
    trigger_rules_col.insert_one({
        "owner_user_id": owner_user_id,
        "group_chat_id": group_chat_id,
        "watch_username": watch_username,
        "keyword": keyword.lower(),
        "active": True,
    })


def get_active_rules_for_group(group_chat_id):
    return list(trigger_rules_col.find({"group_chat_id": group_chat_id, "active": True}))


def list_trigger_rules(owner_user_id):
    return list(trigger_rules_col.find({"owner_user_id": owner_user_id, "active": True}))


# --- memory ---

def save_memory(chat_id, key, value):
    memory_col.update_one(
        {"chat_id": chat_id, "key": key.lower()},
        {"$set": {"value": value, "updated_at": now_iso()}},
        upsert=True,
    )


def get_memory(chat_id, key):
    row = memory_col.find_one({"chat_id": chat_id, "key": key.lower()})
    return row["value"] if row else None


def list_memory(chat_id):
    return list(memory_col.find({"chat_id": chat_id}))


def delete_memory(chat_id, key):
    memory_col.delete_one({"chat_id": chat_id, "key": key.lower()})


# --- group message log, this is what daily summaries get built from ---

def log_group_message(chat_id, username, text):
    group_messages_col.insert_one({
        "chat_id": chat_id,
        "username": username,
        "text": text,
        "ts": now_iso(),
    })


def get_today_messages(chat_id):
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cursor = group_messages_col.find(
        {"chat_id": chat_id, "ts": {"$regex": f"^{today_str}"}}
    ).sort("ts", ASCENDING)
    return list(cursor)


# --- users, so trigger alerts can find the right person to DM ---

def upsert_user(user_id, chat_id, username):
    users_col.update_one(
        {"_id": user_id},
        {"$set": {"chat_id": chat_id, "username": username, "updated_at": now_iso()}},
        upsert=True,
    )


def get_user_chat_id(user_id):
    row = users_col.find_one({"_id": user_id})
    return row["chat_id"] if row else None
