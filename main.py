import logging
import os
import threading
import time

import requests
from flask import Flask
from telegram import Update
from telegram.error import Conflict, NetworkError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import TELEGRAM_BOT_TOKEN, TICK_INTERVAL
import db
import scheduler
import handlers

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# HTTP server
web_app = Flask(__name__)


@web_app.route("/")
def health_check():
    return "Nomi is running.", 200


@web_app.route("/health")
def health():
    return {"status": "ok"}, 200


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False,
    )


def keep_alive(base_url: str):
    """
    pings our own /health endpoint every 10 minutes
    """
    while True:
        time.sleep(600)  # sleep first; no point pinging right after startup
        try:
            requests.get(f"{base_url}/health", timeout=10)
            logger.debug("Keep-alive ping OK")
        except Exception as exc:
            logger.warning("Keep-alive ping failed: %s", exc)


async def bot_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        logger.warning(
            "Telegram conflict — another instance is probably still shutting down. "
            "Will retry automatically. (%s)",
            err,
        )
    elif isinstance(err, NetworkError):
        logger.warning("Transient Telegram network error (will retry): %s", err)
    else:
        logger.error("Unhandled bot exception", exc_info=err)


def main():
    db.init_db()

    # HTTP server thread
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    # self-ping keep-alive
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if render_url:
        ka_thread = threading.Thread(target=keep_alive, args=(render_url,), daemon=True)
        ka_thread.start()
        logger.info("Keep-alive thread started → %s/health (every 10 min)", render_url)
    else:
        logger.info("RENDER_EXTERNAL_URL not set — keep-alive disabled (fine for local dev)")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_error_handler(bot_error_handler)

    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
    app.add_handler(CommandHandler("memory", handlers.memory_cmd))
    app.add_handler(CommandHandler("forget", handlers.forget_cmd))
    app.add_handler(CommandHandler("rules", handlers.rules_cmd))
    app.add_handler(CommandHandler("jobs", handlers.jobs_cmd))
    app.add_handler(CommandHandler("history", handlers.history_cmd))

    # Delete / Cancel buttons on dangerous bulk-delete confirmations
    app.add_handler(CallbackQueryHandler(handlers.bulk_delete_callback, pattern=r"^bulkdel:"))

    # private messages
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            handlers.handle_message,
        )
    )

    # group messages
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
            handlers.handle_group_message,
        )
    )

    # background scheduler
    app.job_queue.run_repeating(
        scheduler.tick,
        interval=TICK_INTERVAL,
        first=10,
    )

    logger.info("Nomi is starting...")
    logger.info("Telegram polling is starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()