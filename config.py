import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
DB_PATH = os.getenv("DB_PATH", "bot.db")
CODE_LENGTH = 8
PAGE_SIZE = 10
BROADCAST_DELAY = 0.05  # seconds between each message to avoid rate limiting

# Telegram Bot API Local Server (optional, for large file support >20MB)
# Set to your local API server URL, e.g. "http://localhost:8081/bot"
# Leave empty to use Telegram's official API (20MB download / 50MB upload limit)
API_BASE_URL = os.getenv("API_BASE_URL", "")
API_BASE_FILE_URL = os.getenv("API_BASE_FILE_URL", "")
