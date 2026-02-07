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


class UnsplashBot:
    def __init__(self, tg_token, unsplash_key):
        self.app = Application.builder().token(tg_token).build()
        self.key = unsplash_key
        self.limiter = RateLimiter()
        self.session = None
        
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("random", self.random))
        self.app.add_handler(CommandHandler("search", self.search))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.text))
        self.app.add_handler(CallbackQueryHandler(self.button))
    
    async def init(self):
        if not self.session:
            self.session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Client-ID {self.key}",
                    "Accept-Version": "v1"
                },
                timeout=aiohttp.ClientTimeout(total=30)
            )
    
    async def request(self, endpoint, params=None):
        if not self.limiter.can_request():
            raise Exception(f"⏳ محدودیت! باقی: {self.limiter.remaining()}/50")
        
        await self.init()
        async with self.session.get(f"https://api.unsplash.com{endpoint}", params=params or {}) as r:
            self.limiter.add()
            if r.status == 429:
                raise Exception("⚠️ Rate limit reached!")
            if r.status >= 400:
                raise Exception(f"❌ API Error: {r.status}")
            return await r.json()
    
    async def track_download(self, download_location):
        """الزامی طبق API Guidelines"""
        try:
            await self.init()
            async with self.session.get(download_location):
                pass
        except:
            pass
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🌄 **Unsplash Bot**\n\n"
            "📝 **دستورات:**\n"
            "• `/random` - عکس تصادفی\n"
            "• `/random nature` - عکس تصادفی با موضوع\n"
            "• `/search ocean` - جستجوی عکس\n\n"
            "💡 **در چت خصوصی:**\n"
            "فقط بنویس: `mountain`\n\n"
            f"⚡️ باقی: {self.limiter.remaining()}/50",
            parse_mode='Markdown'
        )
    
    async def random(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        params = {}
        if context.args:
            params["query"] = " ".join(context.args)
        
        try:
            photo = await self.request("/photos/random", params)
            await self.send_photo(update, photo)
        except Exception as e:
            await update.message.reply_text(f"❌ {e}")
    
    async def search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("❌ مثال: `/search ocean`", parse_mode='Markdown')
            return
        
        query = " ".join(context.args)
        try:
            result = await self.request("/search/photos", {"query": query, "per_page": 1})
            if result.get("results"):
                await self.send_photo(update, result["results"][0])
            else:
                await update.message.reply_text("❌ نتیجه‌ای نیست")
        except Exception as e:
            await update.message.reply_text(f"❌ {e}")
    
    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.message.text.strip()
        
        if not query:
            return
        
        try:
            result = await self.request("/search/photos", {"query": query, "per_page": 1})
            if result.get("results"):
                await self.send_photo(update, result["results"][0])
            else:
                await update.message.reply_text("❌ نتیجه‌ای نیست")
        except Exception as e:
            await update.message.reply_text(f"❌ {e}")
    
    async def send_photo(self, update: Update, photo):
        caption = (
            f"📸 {photo.get('alt_description', 'Photo')}\n"
            f"👤 [{photo['user']['name']}]({photo['user']['links']['html']}?utm_source=telegram_bot&utm_medium=referral)\n"
            f"⚡️ {self.limiter.remaining()}/50"
        )
        
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄", callback_data="refresh"),
            InlineKeyboardButton(
                "📸 Unsplash", 
                url=f"{photo['links']['html']}?utm_source=telegram_bot&utm_medium=referral"
            )
        ]])
        
        if photo.get("links", {}).get("download_location"):
            await self.track_download(photo["links"]["download_location"])
        
        await update.effective_chat.send_photo(
            photo['urls']['regular'],
            caption=caption,
            parse_mode='Markdown',
            reply_markup=keyboard
        )
    
    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if query.data == "refresh":
            try:
                photo = await self.request("/photos/random")
                await query.message.delete()
                await self.send_photo(update, photo)
            except Exception as e:
                await query.message.reply_text(f"❌ {e}")
    
    async def run(self):
        await self.init()
        try:
            await self.app.initialize()
            await self.app.start()
            await self.app.updater.start_polling()
            logger.info("✅ ربات فعال شد")
            await asyncio.Event().wait()
        finally:
            if self.session:
                await self.session.close()
            await self.app.stop()


async def main():
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    UNSPLASH_KEY = os.getenv("UNSPLASH_KEY")
    
    if not TELEGRAM_TOKEN or not UNSPLASH_KEY:
        logger.error("❌ لطفاً TELEGRAM_TOKEN و UNSPLASH_KEY را در Environment Variables تنظیم کنید!")
        return
    
    bot = UnsplashBot(TELEGRAM_TOKEN, UNSPLASH_KEY)
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 ربات متوقف شد")
    except Exception as e:
        logger.error(f"❌ خطای کلی: {e}")
