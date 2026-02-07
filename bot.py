import asyncio
import aiohttp
from datetime import datetime, timedelta
from collections import deque
import logging
import os

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self):
        self.requests = deque()
    
    def can_request(self):
        now = datetime.now()
        while self.requests and (now - self.requests[0]).total_seconds() >= 3600:
            self.requests.popleft()
        return len(self.requests) < 50
    
    def add(self):
        self.requests.append(datetime.now())
    
    def remaining(self):
        now = datetime.now()
        while self.requests and (now - self.requests[0]).total_seconds() >= 3600:
            self.requests.popleft()
        return 50 - len(self.requests)


# Global variables
limiter = RateLimiter()
session = None
unsplash_key = ""


async def init_session():
    global session, unsplash_key
    if not session:
        session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Client-ID {unsplash_key}",
                "Accept-Version": "v1"
            },
            timeout=aiohttp.ClientTimeout(total=30)
        )


async def request(endpoint, params=None):
    if not limiter.can_request():
        raise Exception(f"⏳ محدودیت! باقی: {limiter.remaining()}/50")
    
    await init_session()
    async with session.get(f"https://api.unsplash.com{endpoint}", params=params or {}) as r:
        limiter.add()
        if r.status == 429:
            raise Exception("⚠️ Rate limit!")
        if r.status >= 400:
            raise Exception(f"❌ API Error: {r.status}")
        return await r.json()


async def track_download(download_location):
    try:
        await init_session()
        async with session.get(download_location):
            pass
    except:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌄 **Unsplash Bot**\n\n"
        "📝 `/random` - عکس تصادفی\n"
        "📝 `/search ocean` - جستجو\n"
        f"⚡️ باقی: {limiter.remaining()}/50",
        parse_mode='Markdown'
    )


async def random_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    params = {}
    if context.args:
        params["query"] = " ".join(context.args)
    
    try:
        photo = await request("/photos/random", params)
        await send_photo(update, photo)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ مثال: `/search ocean`", parse_mode='Markdown')
        return
    
    query = " ".join(context.args)
    try:
        result = await request("/search/photos", {"query": query, "per_page": 1})
        if result.get("results"):
            await send_photo(update, result["results"][0])
        else:
            await update.message.reply_text("❌ نتیجه‌ای نیست")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    if not query:
        return
    
    try:
        result = await request("/search/photos", {"query": query, "per_page": 1})
        if result.get("results"):
            await send_photo(update, result["results"][0])
        else:
            await update.message.reply_text("❌ نتیجه‌ای نیست")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def send_photo(update: Update, photo):
    caption = (
        f"📸 {photo.get('alt_description', 'Photo')}\n"
        f"👤 {photo['user']['name']}\n"
        f"⚡️ {limiter.remaining()}/50"
    )
    
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄", callback_data="refresh"),
        InlineKeyboardButton("📸 Unsplash", url=photo['links']['html'])
    ]])
    
    if photo.get("links", {}).get("download_location"):
        await track_download(photo["links"]["download_location"])
    
    await update.effective_chat.send_photo(
        photo['urls']['regular'],
        caption=caption,
        reply_markup=keyboard
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "refresh":
        try:
            photo = await request("/photos/random")
            await query.message.delete()
            await send_photo(update, photo)
        except Exception as e:
            await query.message.reply_text(f"❌ {e}")


def main():
    global unsplash_key
    
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    unsplash_key = os.getenv("UNSPLASH_KEY")
    
    if not TELEGRAM_TOKEN or not unsplash_key:
        logger.error("❌ توکن‌ها رو تنظیم کنید!")
        return
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("random", random_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, text_handler))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    logger.info("✅ ربات شروع شد")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
