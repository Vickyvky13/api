from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
import yt_dlp
import os
import re
import uuid
import random
import asyncio
import logging
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Bot
from telegram.constants import ParseMode
import config

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
try:
    mongo_client = AsyncIOMotorClient(config.MONGO_URI)
    db = mongo_client[config.MONGO_DB_NAME]
    songs_col = db["songs"]
    logger.info("✅ MongoDB connected")
except Exception as e:
    logger.error(f"❌ MongoDB connection failed: {e}")
    raise

# ─────────────────────────────────────────
# TELEGRAM BOT
# ─────────────────────────────────────────
try:
    bot = Bot(token=config.BOT_TOKEN)
    logger.info("✅ Telegram bot initialized")
except Exception as e:
    logger.error(f"❌ Telegram bot initialization failed: {e}")
    raise

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def extract_video_id(raw: str) -> Optional[str]:
    """Return bare 11-char YouTube video ID from any URL / ID string."""
    if not raw:
        return None
        
    patterns = [
        r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$"  # Direct ID
    ]
    for pat in patterns:
        m = re.search(pat, raw)
        if m:
            return m.group(1)
    return None


def pick_cookie_file() -> Optional[str]:
    """Return a random .txt file from ./cookies/ folder."""
    cookie_dir = os.path.join(os.getcwd(), "cookies")
    if not os.path.isdir(cookie_dir):
        return None
    files = [f for f in os.listdir(cookie_dir) if f.endswith(".txt")]
    return os.path.join(cookie_dir, random.choice(files)) if files else None


def ydl_opts_audio(output_template: str, cookie_file: Optional[str]) -> dict:
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


async def download_audio_local(video_id: str) -> Optional[str]:
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
        logger.info(f"✅ Downloaded audio for {video_id} to {final_path}")
    except Exception as e:
        logger.error(f"yt-dlp failed for {video_id}: {e}")
        return None

    return final_path if os.path.exists(final_path) else None


async def upload_to_telegram(local_path: str, title: str, video_id: str) -> Optional[str]:
    """
    Upload mp3 to Telegram channel; return file_id of uploaded audio.
    The channel must have the bot as admin with 'Post Messages' right.
    """
    try:
        # Check if file exists and is not empty
        if not os.path.exists(local_path):
            logger.error(f"File not found: {local_path}")
            return None
            
        file_size = os.path.getsize(local_path)
        if file_size == 0:
            logger.error(f"File is empty: {local_path}")
            return None
            
        logger.info(f"Uploading {file_size} bytes to Telegram for {video_id}")
        
        with open(local_path, "rb") as audio_file:
            msg = await bot.send_audio(
                chat_id=config.TELEGRAM_CHANNEL,
                audio=audio_file,
                title=title[:1024],  # Telegram title limit
                caption=f"🎵 {title[:200]}\n🔗 https://youtu.be/{video_id}",
                parse_mode=ParseMode.HTML,
                timeout=60,  # 60 second timeout
            )
        
        logger.info(f"✅ Uploaded to Telegram for {video_id}, file_id: {msg.audio.file_id}")
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
        title = info.get("title", video_id)
        logger.info(f"✅ Got title for {video_id}: {title[:50]}")
        return title
    except Exception as e:
        logger.error(f"Failed to get title for {video_id}: {e}")
        return video_id

# ─────────────────────────────────────────
# STARTUP  –  ensure unique index
# ─────────────────────────────────────────
@app.on_event("startup")
async def startup():
    try:
        # unique index prevents duplicate documents for the same video_id
        await songs_col.create_index("video_id", unique=True)
        logger.info("✅ MongoDB unique index on video_id ensured")
        
        # Test Telegram connection
        me = await bot.get_me()
        logger.info(f"✅ Bot connected: @{me.username}")
        
        # Test channel access
        try:
            chat = await bot.get_chat(config.TELEGRAM_CHANNEL)
            logger.info(f"✅ Channel access confirmed: {chat.title}")
        except Exception as e:
            logger.warning(f"⚠️ Cannot access channel: {e}")
            
    except Exception as e:
        logger.error(f"❌ Startup error: {e}")

# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────
@app.get("/")
async def home():
    return {"status": "online", "message": "Solo YouTube Cache API Running"}


@app.get("/health")
async def health():
    """Health check endpoint"""
    try:
        # Check MongoDB
        await db.command("ping")
        mongo_status = "ok"
    except:
        mongo_status = "failed"
        
    return {
        "status": "healthy",
        "mongodb": mongo_status,
        "telegram_bot": "ok"
    }


@app.post("/download")
async def download_endpoint(
    data: dict,
    x_api_key: str = Header(None)
):
    # ── Auth ──────────────────────────────
    if not x_api_key or x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")

    raw_url = data.get("url", "").strip()
    if not raw_url:
        return JSONResponse(
            {"status": "error", "message": "No URL provided"}, 
            status_code=400
        )

    video_id = extract_video_id(raw_url)
    if not video_id or len(video_id) != 11:
        return JSONResponse(
            {"status": "error", "message": f"Could not extract valid video ID from: {raw_url}"}, 
            status_code=400
        )

    try:
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
            return JSONResponse(
                {"status": "error", "message": "Download failed"}, 
                status_code=500
            )

        # ── 4. Upload to Telegram ─────────────
        tg_file_id = await upload_to_telegram(local_path, title, video_id)

        # cleanup local file
        try:
            os.remove(local_path)
            logger.info(f"🧹 Cleaned up local file for {video_id}")
        except Exception as e:
            logger.warning(f"Failed to cleanup {local_path}: {e}")

        if not tg_file_id:
            return JSONResponse(
                {"status": "error", "message": "Telegram upload failed"}, 
                status_code=500
            )

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
        
    except Exception as e:
        logger.error(f"Unexpected error for {video_id}: {e}", exc_info=True)
        return JSONResponse(
            {"status": "error", "message": f"Internal server error: {str(e)}"}, 
            status_code=500
        )


@app.get("/song/{video_id}")
async def get_song(video_id: str, x_api_key: str = Header(None)):
    """Direct lookup by video_id (no download)."""
    if not x_api_key or x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")
        
    # Validate video_id
    if not video_id or len(video_id) != 11:
        raise HTTPException(status_code=400, detail="Invalid video ID format")
        
    doc = await songs_col.find_one({"video_id": video_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Song not found in cache")
        
    return {"status": "success", "data": doc}


@app.delete("/song/{video_id}")
async def delete_song(video_id: str, x_api_key: str = Header(None)):
    """Remove a song from cache."""
    if not x_api_key or x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")
        
    result = await songs_col.delete_one({"video_id": video_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Song not found")
        
    return {"status": "success", "message": f"{video_id} removed from cache"}


@app.get("/stats")
async def get_stats(x_api_key: str = Header(None)):
    """Get cache statistics"""
    if not x_api_key or x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")
        
    count = await songs_col.count_documents({})
    return {
        "status": "success",
        "total_cached_songs": count,
        "mongodb_database": config.MONGO_DB_NAME,
        "telegram_channel": config.TELEGRAM_CHANNEL
    }