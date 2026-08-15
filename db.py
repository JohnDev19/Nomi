from datetime import datetime, timezone

from pymongo import MongoClient, ASCENDING, ReturnDocument
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
action_history_col = _db.action_history
counters_col = _db.counters
chat_settings_col = _db.chat_settings


def init_db():
    """Sets up the indexes we rely on. Collections themselves get created lazily by mongo."""
    try:
        memory_col.create_index([("chat_id", ASCENDING), ("key", ASCENDING)], unique=True)
        trigger_rules_col.create_index([("group_chat_id", ASCENDING), ("active", ASCENDING)])
        reminders_col.create_index([("sent", ASCENDING), ("run_at", ASCENDING)])
        scheduled_tasks_col.create_index([("active", ASCENDING), ("next_run", ASCENDING)])
        group_messages_col.create_index([("chat_id", ASCENDING), ("ts", ASCENDING)])
        action_history_col.create_index([("chat_id", ASCENDING), ("seq", ASCENDING)], unique=True)
        chat_settings_col.create_index([("chat_id", ASCENDING), ("key", ASCENDING)], unique=True)
    except PyMongoError as exc:
        raise RuntimeError(
            "Couldn't reach MongoDB. Double-check MONGODB_URI/MONGODB_PASSWORD, and make sure "
            "your current IP is allowed in the Atlas network access list."
        ) from exc


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# --- reminders ---

def add_reminder(chat_id, text, run_at_iso):
    result = reminders_col.insert_one({
        "chat_id": chat_id,
        "text": text,
        "run_at": run_at_iso,
        "sent": False,
        "created_at": now_iso(),
    })
    return result.inserted_id


def get_due_reminders():
    return list(reminders_col.find({"sent": False, "run_at": {"$lte": now_iso()}}))


def mark_reminder_sent(reminder_id):
    reminders_col.update_one({"_id": reminder_id}, {"$set": {"sent": True}})


def delete_reminder_if_unsent(reminder_id):
    """Used to undo a reminder creation. Returns False if it already fired (nothing to undo)."""
    result = reminders_col.delete_one({"_id": reminder_id, "sent": False})
    return result.deleted_count > 0


def count_reminders(chat_id):
    return reminders_col.count_documents({"chat_id": chat_id, "sent": False})


def delete_all_reminders(chat_id):
    """Deletes every unsent reminder for a chat and returns the deleted docs (so it can be undone)."""
    docs = list(reminders_col.find({"chat_id": chat_id, "sent": False}))
    if docs:
        reminders_col.delete_many({"chat_id": chat_id, "sent": False})
    return docs


def restore_reminders(docs):
    if docs:
        reminders_col.insert_many(docs)


# --- scheduled tasks ---

def add_scheduled_task(chat_id, task_type, payload, day_of_week, hour, minute, next_run_iso):
    result = scheduled_tasks_col.insert_one({
        "chat_id": chat_id,
        "task_type": task_type,
        "payload": payload or {},
        "day_of_week": day_of_week,
        "hour": hour,
        "minute": minute,
        "next_run": next_run_iso,
        "active": True,
    })
    return result.inserted_id


def get_due_scheduled_tasks():
    return list(scheduled_tasks_col.find({"active": True, "next_run": {"$lte": now_iso()}}))


def update_next_run(task_id, next_run_iso):
    scheduled_tasks_col.update_one({"_id": task_id}, {"$set": {"next_run": next_run_iso}})


def list_scheduled_tasks(chat_id):
    return list(scheduled_tasks_col.find({"chat_id": chat_id, "active": True}))


def deactivate_scheduled_task(task_id):
    """Used to undo a scheduled task creation."""
    scheduled_tasks_col.update_one({"_id": task_id}, {"$set": {"active": False}})


def count_scheduled_tasks(chat_id):
    return scheduled_tasks_col.count_documents({"chat_id": chat_id, "active": True})


def delete_all_scheduled_tasks(chat_id):
    docs = list(scheduled_tasks_col.find({"chat_id": chat_id, "active": True}))
    if docs:
        scheduled_tasks_col.delete_many({"chat_id": chat_id, "active": True})
    return docs


def restore_scheduled_tasks(docs):
    if docs:
        scheduled_tasks_col.insert_many(docs)


# --- trigger rules ---

def add_trigger_rule(owner_user_id, group_chat_id, watch_username, keyword):
    result = trigger_rules_col.insert_one({
        "owner_user_id": owner_user_id,
        "group_chat_id": group_chat_id,
        "watch_username": watch_username,
        "keyword": keyword.lower(),
        "active": True,
    })
    return result.inserted_id


def get_active_rules_for_group(group_chat_id):
    return list(trigger_rules_col.find({"group_chat_id": group_chat_id, "active": True}))


def list_trigger_rules(owner_user_id):
    return list(trigger_rules_col.find({"owner_user_id": owner_user_id, "active": True}))


def deactivate_trigger_rule(rule_id):
    """Used to undo a trigger rule creation."""
    trigger_rules_col.update_one({"_id": rule_id}, {"$set": {"active": False}})


def count_trigger_rules(owner_user_id):
    return trigger_rules_col.count_documents({"owner_user_id": owner_user_id, "active": True})


def delete_all_trigger_rules(owner_user_id):
    docs = list(trigger_rules_col.find({"owner_user_id": owner_user_id, "active": True}))
    if docs:
        trigger_rules_col.delete_many({"owner_user_id": owner_user_id, "active": True})
    return docs


def restore_trigger_rules(docs):
    if docs:
        trigger_rules_col.insert_many(docs)


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


def restore_memory(chat_id, key, value):
    """Used to undo a memory save/forget. If value is None the key didn't exist before, so drop it."""
    if value is None:
        memory_col.delete_one({"chat_id": chat_id, "key": key.lower()})
    else:
        memory_col.update_one(
            {"chat_id": chat_id, "key": key.lower()},
            {"$set": {"value": value, "updated_at": now_iso()}},
            upsert=True,
        )


def count_memory(chat_id):
    return memory_col.count_documents({"chat_id": chat_id})


def delete_all_memory(chat_id):
    docs = list(memory_col.find({"chat_id": chat_id}))
    if docs:
        memory_col.delete_many({"chat_id": chat_id})
    return docs


def restore_memory_bulk(docs):
    if docs:
        memory_col.insert_many(docs)


# --- group message log ---

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


# --- users ---

def upsert_user(user_id, chat_id, username):
    update_fields = {"username": username, "updated_at": now_iso()}
    if chat_id is not None:
        update_fields["chat_id"] = chat_id
    users_col.update_one(
        {"_id": user_id},
        {"$set": update_fields},
        upsert=True,
    )


def get_user_chat_id(user_id):
    row = users_col.find_one({"_id": user_id})
    return row["chat_id"] if row else None


# --- chat settings (e.g. conversation mode toggle) ---

def get_chat_setting(chat_id, key, default=None):
    row = chat_settings_col.find_one({"chat_id": chat_id, "key": key})
    return row["value"] if row else default


def set_chat_setting(chat_id, key, value):
    chat_settings_col.update_one(
        {"chat_id": chat_id, "key": key},
        {"$set": {"value": value, "updated_at": now_iso()}},
        upsert=True,
    )


# --- action history ---

def _next_action_seq(chat_id):
    doc = counters_col.find_one_and_update(
        {"_id": f"actions:{chat_id}"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc["seq"]


def log_action(chat_id, action_type, description, undo_data=None):
    seq = _next_action_seq(chat_id)
    doc = {
        "chat_id": chat_id,
        "seq": seq,
        "action_type": action_type,
        "description": description,
        "undo_data": undo_data or {},
        "undone": False,
        "created_at": now_iso(),
    }
    result = action_history_col.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


def get_action_history(chat_id, limit=10):
    rows = list(action_history_col.find({"chat_id": chat_id}).sort("seq", -1).limit(limit))
    rows.reverse()
    return rows


def get_last_undoable_action(chat_id):
    return action_history_col.find_one({"chat_id": chat_id, "undone": False}, sort=[("seq", -1)])


def get_action_by_seq(chat_id, seq):
    return action_history_col.find_one({"chat_id": chat_id, "seq": seq})


def mark_action_undone(action_id):
    action_history_col.update_one({"_id": action_id}, {"$set": {"undone": True}})