"""
server.py  ─  YouTube → Telegram Cache API
===========================================
Flow:
  POST /download  { "url": "<yt_url_or_id>", "type": "audio" }
  1. Extract video_id from URL
  2. Check MongoDB → if found, return stored Telegram file_id (no re-download)
  3. Download with yt-dlp + cookies.txt  →  MP3
  4. Upload to Telegram channel @apisolotreee  →  get file_id
  5. Save to MongoDB (with unique index on video_id → auto-dedup)
  6. Return Telegram file_id so client can forward/stream directly
"""

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
import yt_dlp
import os
import re
import uuid
import random
import asyncio
import logging
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Bot
from telegram.constants import ParseMode
import config  # see config.py

# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("SoloAPI")

# ─────────────────────────────────────────
# APP & CONFIG
# ─────────────────────────────────────────
app = FastAPI(title="Solo YouTube Cache API")

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ─────────────────────────────────────────
# MONGODB  (motor async driver)
# ─────────────────────────────────────────
mongo_client = AsyncIOMotorClient(config.MONGO_URI)
db = mongo_client[config.MONGO_DB_NAME]
songs_col = db["songs"]   # unique index on video_id (created at startup)

# ─────────────────────────────────────────
# TELEGRAM BOT
# ─────────────────────────────────────────
bot = Bot(token=config.BOT_TOKEN)

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def extract_video_id(raw: str) -> str:
    """Return bare 11-char YouTube video ID from any URL / ID string."""
    patterns = [
        r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})",
    ]
    for pat in patterns:
        m = re.search(pat, raw)
        if m:
            return m.group(1)
    # assume raw is already an ID
    return raw.strip()


def pick_cookie_file() -> str | None:
    """Return a random .txt file from ./cookies/ folder."""
    cookie_dir = os.path.join(os.getcwd(), "cookies")
    if not os.path.isdir(cookie_dir):
        return None
    files = [f for f in os.listdir(cookie_dir) if f.endswith(".txt")]
    return os.path.join(cookie_dir, random.choice(files)) if files else None


def ydl_opts_audio(output_template: str, cookie_file: str | None) -> dict:
    opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    return opts


async def download_audio_local(video_id: str) -> str | None:
    """Download audio with yt-dlp; return local mp3 path or None."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    file_id_local = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_DIR, f"{file_id_local}.%(ext)s")
    final_path = os.path.join(DOWNLOAD_DIR, f"{file_id_local}.mp3")

    cookie_file = pick_cookie_file()
    opts = ydl_opts_audio(output_template, cookie_file)

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: yt_dlp.YoutubeDL(opts).__enter__().download([url])
        )
    except Exception as e:
        logger.error(f"yt-dlp failed for {video_id}: {e}")
        return None

    return final_path if os.path.exists(final_path) else None


async def upload_to_telegram(local_path: str, title: str, video_id: str) -> str | None:
    """
    Upload mp3 to Telegram channel; return file_id of uploaded audio.
    The channel @apisolotreee must have the bot as admin with 'Post Messages' right.
    """
    try:
        with open(local_path, "rb") as audio_file:
            msg = await bot.send_audio(
                chat_id=config.TELEGRAM_CHANNEL,   # "@apisolotreee" or numeric id
                audio=audio_file,
                title=title,
                caption=f"🎵 {title}\n🔗 https://youtu.be/{video_id}",
                parse_mode=ParseMode.HTML,
            )
        return msg.audio.file_id
    except Exception as e:
        logger.error(f"Telegram upload failed for {video_id}: {e}")
        return None


async def get_yt_title(video_id: str) -> str:
    """Fetch video title without downloading."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    cookie_file = pick_cookie_file()
    opts = {"quiet": True, "skip_download": True}
    if cookie_file:
        opts["cookiefile"] = cookie_file
    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(
            None,
            lambda: yt_dlp.YoutubeDL(opts).extract_info(url, download=False)
        )
        return info.get("title", video_id)
    except Exception:
        return video_id

# ─────────────────────────────────────────
# STARTUP  –  ensure unique index
# ─────────────────────────────────────────
@app.on_event("startup")
async def startup():
    # unique index prevents duplicate documents for the same video_id
    await songs_col.create_index("video_id", unique=True)
    logger.info("✅ MongoDB unique index on video_id ensured.")

# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────
@app.get("/")
async def home():
    return {"status": "online", "message": "Solo YouTube Cache API Running"}


@app.post("/download")
async def download_endpoint(
    data: dict,
    x_api_key: str = Header(None)
):
    # ── Auth ──────────────────────────────
    if x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")

    raw_url = data.get("url", "").strip()
    if not raw_url:
        return JSONResponse({"status": "error", "message": "No URL provided"}, status_code=400)

    video_id = extract_video_id(raw_url)
    if len(video_id) < 5:
        return JSONResponse({"status": "error", "message": "Could not extract video ID"}, status_code=400)

    # ── 1. Check MongoDB cache ─────────────
    cached = await songs_col.find_one({"video_id": video_id})
    if cached:
        logger.info(f"✅ [CACHE HIT] {video_id}")
        return {
            "status": "success",
            "source": "cache",
            "video_id": video_id,
            "title": cached.get("title", ""),
            "telegram_file_id": cached["telegram_file_id"],
        }

    logger.info(f"⬇️  [DOWNLOAD] {video_id}")

    # ── 2. Get title ──────────────────────
    title = await get_yt_title(video_id)

    # ── 3. Download locally ───────────────
    local_path = await download_audio_local(video_id)
    if not local_path:
        return JSONResponse({"status": "error", "message": "Download failed"}, status_code=500)

    # ── 4. Upload to Telegram ─────────────
    tg_file_id = await upload_to_telegram(local_path, title, video_id)

    # cleanup local file
    try:
        os.remove(local_path)
    except Exception:
        pass

    if not tg_file_id:
        return JSONResponse({"status": "error", "message": "Telegram upload failed"}, status_code=500)

    # ── 5. Save to MongoDB (unique → auto dedup) ──
    doc = {
        "video_id": video_id,
        "title": title,
        "telegram_file_id": tg_file_id,
        "url": f"https://youtu.be/{video_id}",
    }
    try:
        await songs_col.insert_one(doc)
        logger.info(f"💾 [SAVED] {video_id} → {tg_file_id}")
    except Exception as e:
        # duplicate key = race condition, still return success
        logger.warning(f"MongoDB insert skipped (likely duplicate): {e}")

    return {
        "status": "success",
        "source": "downloaded",
        "video_id": video_id,
        "title": title,
        "telegram_file_id": tg_file_id,
    }


@app.get("/song/{video_id}")
async def get_song(video_id: str, x_api_key: str = Header(None)):
    """Direct lookup by video_id (no download)."""
    if x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    doc = await songs_col.find_one({"video_id": video_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Song not found in cache")
    return {"status": "success", "data": doc}


@app.delete("/song/{video_id}")
async def delete_song(video_id: str, x_api_key: str = Header(None)):
    """Remove a song from cache."""
    if x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    result = await songs_col.delete_one({"video_id": video_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Song not found")
    return {"status": "success", "message": f"{video_id} removed from cache"}
