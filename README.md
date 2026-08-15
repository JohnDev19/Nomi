# Nomi — AI Personal Assistant Bot

A Telegram bot that understands plain-language requests and acts on them:
reminders, recurring tasks, group summaries, keyword alerts, personal memory,
and undo.

## Features

- **Natural-language commands** — talk to her normally, no slash-command syntax needed
- **Reminders** — "remind me tomorrow at 8am to submit my resume"
- **Scheduled/recurring tasks** — "every Monday send me 5 junior dev jobs"
- **Web/API integration** — pulls live junior dev listings from RemoteOK's public API
- **Keyword notifications** — "if John sends a message with 'urgent', notify me"
- **AI summaries** — summarizes a group chat's messages for the day
- **Personal memory** — "remember that I prefer morning meetings" and she'll recall it later
- **Undo** — "undo" reverses the last thing you did; "undo #3" reverses a specific
  numbered action from `/history`
- **Confirmation before dangerous actions** — anything that deletes more than one
  thing at once ("delete all my reminders") shows a count and asks you to confirm
  with a [Delete] / [Cancel] button before touching anything

## How it's put together

```
main.py            - starts the bot, wires up handlers and the scheduler
config.py           - env var loading, Mongo URI assembly, identity settings
db.py               - MongoDB storage (reminders, tasks, rules, memory, action history, logs)
llm_client.py       - OpenRouter API wrapper
intent_parser.py    - turns a message into a structured intent (JSON) via the LLM
time_utils.py       - date/time resolution for reminders and recurring tasks
scheduler.py        - background loop that fires due reminders/tasks
handlers.py         - Telegram command + message handlers
integrations/jobs.py - RemoteOK job search integration
```

The LLM does two jobs here: classifying what the user wants (reminder, scheduled
task, trigger rule, summary, memory, undo, bulk delete, or just chat) and, for
chat/summary, generating the actual reply text. Since the model is small,
`intent_parser.py` has a fallback: if it doesn't return clean JSON, the whole
response just gets treated as a normal chat reply instead of erroring out.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) on Telegram and grab the token.
2.  In MongoDB Atlas, make sure your database user's password is handy, and add
   whichever IP you're running the bot from to the cluster's Network Access list
   (Atlas blocks connections from unlisted IPs by default).
3. Copy `.env.example` to `.env` and fill it in:

   ```
   cp .env.example .env
   ```

   - `MONGODB_URI` keeps the `<db_password>` placeholder as-is — don't put the real
     password there.
   - `MONGODB_PASSWORD` holds the actual secret. The code swaps it into the URI at
     startup and percent-encodes it automatically, so special characters in the
     password (`@`, `:`, `/`, etc.) won't break the connection string.

5. Install dependencies:

   ```
   pip install -r requirements.txt
   ```

6. Run it:

   ```
   python main.py
   ```

## Usage notes

- **DMs** go straight through the natural-language pipeline — just type what you want.
- **Groups**: add the bot, then @mention it (or reply to one of its messages) to ask
  it something directly (e.g. "@yourbot summarize today"). Every other message in
  the group just gets logged (for summaries) and checked against your alert rules.
- **Keyword alerts should be set up from inside the group you want watched** — the
  bot uses whatever chat the request came from as the group to monitor. It'll try
  to DM you the alert; if it doesn't know your private chat yet (you haven't messaged
  it directly), it'll post the alert in the group instead.
- **Undo**: every reversible action (reminder, recurring task, keyword alert, memory
  save/forget, bulk delete) gets logged with a number. Say "undo" to reverse the most
  recent one, or "undo #3" for a specific one. Use `/history` to see the numbered list:

  ```
  Action History

  1. 10:03 Created reminder: "submit my resume"
  2. 10:04 Saved memory: work_pref = mornings
  3. 10:05 Created recurring task (every Monday at 09:00)
  ```

  A reminder can't be undone once it's already fired, and undoing something twice
  is a no-op — Nomi will just tell you it's already undone.
- **Dangerous actions** — anything that deletes *multiple* things at once (e.g.
  "delete all my reminders", "forget everything about me") never happens
  immediately. Nomi tells you how many items would be affected and shows
  [Delete] / [Cancel] buttons; only the person who asked can tap them. If you
  confirm, the delete is itself logged as an undoable action, so "undo" right
  after works too. Deleting one specific named item (e.g. "delete the reminder
  about the dentist") isn't supported yet — Nomi will say so if you ask for that.
- Reminders and recurring tasks get checked every 30 seconds by default (see
  `TICK_INTERVAL_SECONDS` in `.env`).
- Want a different name or creator credit? Change `BOT_NAME` / `BOT_CREATOR` in `.env`.

## Known limitations

- Mongo calls are synchronous/blocking (via `pymongo`), which is fine at small scale
  but adds real network latency per call since Atlas is remote. Worth switching to
  `motor` (the async driver) if you're expecting heavy concurrent use.
- The job search integration is wired to RemoteOK specifically; swap out
  `integrations/jobs.py` for a different job board or API if you need something else.
- Pending bulk-delete confirmations live in memory, not Mongo. If the bot restarts
  between showing the [Delete]/[Cancel] buttons and you tapping one, the buttons will
  say the request expired and you'll need to ask again. This is intentional — bulk
  deletes are rare enough that persisting confirmation state isn't worth the complexity,
  and it fails safe (nothing gets deleted) rather than unsafe.
- Undo only covers actions Nomi has logged (reminders, scheduled tasks, keyword
  alerts, memory saves/forgets, and bulk deletes). There's currently no "undo" for
  things outside that list, like a summary that already got sent.

## Security note

Never commit your `.env` file — it holds your bot token, OpenRouter key, and Mongo
password. `.gitignore` already excludes it.