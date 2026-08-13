import os
from urllib.parse import quote_plus
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "liquid/lfm-2.5-2.6b:free")
TIMEZONE = os.getenv("BOT_TIMEZONE", "Asia/Manila")
TICK_INTERVAL = int(os.getenv("TICK_INTERVAL_SECONDS", "30"))

BOT_NAME = os.getenv("BOT_NAME", "Nomi")
BOT_CREATOR = os.getenv("BOT_CREATOR", "John Ré")

# the URI keeps the <db_password> placeholder in .env, the real secret stays
# in its own var so nobody accidentally commits a live password along with the connection string
MONGODB_URI_TEMPLATE = os.getenv(
    "MONGODB_URI",
    "mongodb+srv://nomi_chatbot:<db_password>@nomi.rvtryqg.mongodb.net/?appName=Nomi",
)
MONGODB_PASSWORD = os.getenv("MONGODB_PASSWORD")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "nomi")

# fail loud and early, no point starting the bot without these
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN, check your .env file")

if not OPENROUTER_API_KEY:
    raise RuntimeError("Missing OPENROUTER_API_KEY, check your .env file")

if not MONGODB_PASSWORD:
    raise RuntimeError("Missing MONGODB_PASSWORD, check your .env file")

# mongo needs special characters in credentials percent-encoded, or the URI just breaks
MONGODB_URI = MONGODB_URI_TEMPLATE.replace("<db_password>", quote_plus(MONGODB_PASSWORD))
