# Nomi — AI Personal Assistant Bot

A Telegram bot that understands plain-language requests and actually acts on them —
reminders, recurring tasks, group summaries, keyword alerts, and personal memory,
all routed through an LLM (via OpenRouter) instead of hardcoded commands.

Her name is **Nomi**, built by **John Ré** — that's baked into her system prompt, so
if you ask her who she is or who made her, she'll answer in character instead of
giving a generic "I'm an AI language model" response.

## Features

- **Natural-language commands** — talk to her normally, no slash-command syntax needed
- **Reminders** — "remind me tomorrow at 8am to submit my resume"
- **Scheduled/recurring tasks** — "every Monday send me 5 junior dev jobs"
- **Web/API integration** — pulls live junior dev listings from RemoteOK's public API
- **Keyword notifications** — "if John sends a message with 'urgent', notify me"
- **AI summaries** — summarizes a group chat's messages for the day
- **Personal memory** — "remember that I prefer morning meetings" and she'll recall it later

## How it's put together

```
main.py            - starts the bot, wires up handlers and the scheduler
config.py           - env var loading, Mongo URI assembly, identity settings
db.py               - MongoDB storage (reminders, tasks, rules, memory, logs)
llm_client.py       - OpenRouter API wrapper
intent_parser.py    - turns a message into a structured intent (JSON) via the LLM
time_utils.py       - date/time resolution for reminders and recurring tasks
scheduler.py        - background loop that fires due reminders/tasks
handlers.py         - Telegram command + message handlers
integrations/jobs.py - RemoteOK job search integration
```

The LLM does two jobs here: classifying what the user wants (reminder, scheduled
task, trigger rule, summary, memory, or just chat) and, for chat/summary, generating
the actual reply text. Since the model is small, `intent_parser.py` has a fallback:
if it doesn't return clean JSON, the whole response just gets treated as a normal
chat reply instead of erroring out.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) on Telegram and grab the token.
2. Get an API key from [OpenRouter](https://openrouter.ai/).
3. In MongoDB Atlas, make sure your database user's password is handy, and add
   whichever IP you're running the bot from to the cluster's Network Access list
   (Atlas blocks connections from unlisted IPs by default).
4. Copy `.env.example` to `.env` and fill it in:

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
- Reminders and recurring tasks get checked every 30 seconds by default (see
  `TICK_INTERVAL_SECONDS` in `.env`).
- Want a different name or creator credit? Change `BOT_NAME` / `BOT_CREATOR` in `.env`.

## Known limitations

- The free/small LLM model can occasionally misread a date or misclassify intent —
  worth swapping `OPENROUTER_MODEL` for a stronger model if accuracy matters more
  than cost.
- Mongo calls are synchronous/blocking (via `pymongo`), which is fine at small scale
  but adds real network latency per call since Atlas is remote. Worth switching to
  `motor` (the async driver) if you're expecting heavy concurrent use.
- The job search integration is wired to RemoteOK specifically; swap out
  `integrations/jobs.py` for a different job board or API if you need something else.

## Security note

Never commit your `.env` file — it holds your bot token, OpenRouter key, and Mongo
password. `.gitignore` already excludes it.
