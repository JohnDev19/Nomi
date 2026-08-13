import logging
import os
import threading
import time

import requests
from flask import Flask
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

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
    Pings our own /health endpoint every 10 minutes
    """
    while True:
        time.sleep(600)  # sleep first; no point pinging right after startup
        try:
            requests.get(f"{base_url}/health", timeout=10)
            logger.debug("Keep-alive ping OK")
        except Exception as exc:
            logger.warning("Keep-alive ping failed: %s", exc)


def main():
    db.init_db()

    # HTTP server thread
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    render_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if render_url:
        ka_thread = threading.Thread(target=keep_alive, args=(render_url,), daemon=True)
        ka_thread.start()
        logger.info("Keep-alive thread started → %s/health (every 10 min)", render_url)
    else:
        logger.info("RENDER_EXTERNAL_URL not set — keep-alive disabled (fine for local dev)")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
    app.add_handler(CommandHandler("memory", handlers.memory_cmd))
    app.add_handler(CommandHandler("forget", handlers.forget_cmd))
    app.add_handler(CommandHandler("rules", handlers.rules_cmd))
    app.add_handler(CommandHandler("jobs", handlers.jobs_cmd))

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

    app.run_polling()


if __name__ == "__main__":
    main()