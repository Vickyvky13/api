# config.py  ─  fill in your actual values before running

# ── API Server ──────────────────────────────────────
API_URL = "http://localhost:8000"       # URL where server.py runs
API_KEY = "SOLO"                        # Secret key (same in server + client)

# ── Telegram ────────────────────────────────────────
BOT_TOKEN   = "123456:ABCdef..."        # Your bot token from @BotFather
TELEGRAM_CHANNEL = "@apisolotreee"      # Channel username or numeric id (-100...)
# Bot must be admin in the channel with "Post Messages" permission

# ── MongoDB ─────────────────────────────────────────
MONGO_URI     = "mongodb://localhost:27017"   # or your Atlas URI
MONGO_DB_NAME = "solo_music"
