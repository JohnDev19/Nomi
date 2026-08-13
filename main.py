import logging

from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

from config import TELEGRAM_BOT_TOKEN, TICK_INTERVAL
import db
import scheduler
import handlers

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def main():
    db.init_db()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
    app.add_handler(CommandHandler("memory", handlers.memory_cmd))
    app.add_handler(CommandHandler("forget", handlers.forget_cmd))
    app.add_handler(CommandHandler("rules", handlers.rules_cmd))
    app.add_handler(CommandHandler("jobs", handlers.jobs_cmd))

    # DMs go straight through the NLU pipeline
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handlers.handle_message))
    # groups just get logged and checked against trigger rules, unless the bot's mentioned/replied to
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, handlers.handle_group_message))

    app.job_queue.run_repeating(scheduler.tick, interval=TICK_INTERVAL, first=10)

    logger.info("bot is up, polling for updates...")
    app.run_polling()


if __name__ == "__main__":
    main()
