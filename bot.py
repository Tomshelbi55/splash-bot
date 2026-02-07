import asyncio
import os
import logging
from datetime import datetime, timedelta
from collections import deque
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self):
        self.requests = deque()
    
    def can_request(self) -> bool:
        now = datetime.now()
        while self.requests and (now - self.requests[0]).total_seconds() >= 3600:
            self.requests.popleft()
        return len(self.requests) < 50
    
    def add(self):
        self.requests.append(datetime.now())
    
    def remaining(self) -> int:
        now = datetime.now()
        while self.requests and (now - self.requests[0]).total_seconds() >= 3600:
            self.requests.popleft()
        return 50 - len(self.requests)


class UnsplashAPI:
    def __init__(self, access_key: str):
        self.access_key = access_key
        self.session: Optional[aiohttp.ClientSession] = None
        self.limiter = RateLimiter()
    
    async def init(self):
        if not self.session:
            self.session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Client-ID {self.access_key}",
                    "Accept-Version": "v1"
                },
                timeout=aiohttp.ClientTimeout(total=30)
            )
    
    async def close(self):
        if self.session:
            await self.session.close()
    
    async def request(self, endpoint: str, params: dict = None):
        if not self.limiter.can_request():
            raise Exception(f"⏳ محدودیت! باقی: {self.limiter.remaining()}/50")
        
        await self.init()
        
        url = f"https://api.unsplash.com{endpoint}"
        async with self.session.get(url, params=params or {}) as response:
            self.limiter.add()
            
            if response.status == 429:
                raise Exception("⚠️ Rate limit reached!")
            if response.status >= 400:
                raise Exception(f"❌ API Error: {response.status}")
            
            return await response.json()
    
    async def get_random(self, query: str = None):
        params = {}
        if query:
            params["query"] = query
        return await self.request("/photos/random", params)
    
    async def search(self, query: str, per_page: int = 1):
        params = {"query": query, "per_page": per_page}
        return await self.request("/search/photos", params)
    
    async def track_download(self, download_location: str):
        try:
            await self.init()
            async with self.session.get(download_location):
                pass
        except:
            pass


# Global instances
bot: Optional[Bot] = None
unsplash: Optional[UnsplashAPI] = None


def get_keyboard() -> InlineKeyboardMarkup:
    """ساخت کیبورد inline"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 جدید", callback_data="refresh")
        ]
    ])


async def send_photo_message(message_or_callback, photo: dict):
    """ارسال عکس با caption"""
    caption = (
        f"📸 {photo.get('alt_description', 'Photo')}\n"
        f"👤 {photo['user']['name']}\n"
        f"🔗 [Unsplash]({photo['links']['html']}?utm_source=telegram_bot&utm_medium=referral)\n"
        f"⚡️ باقی: {unsplash.limiter.remaining()}/50"
    )
    
    # Track download
    if photo.get("links", {}).get("download_location"):
        await unsplash.track_download(photo["links"]["download_location"])
    
    # ارسال عکس
    if isinstance(message_or_callback, types.CallbackQuery):
        await message_or_callback.message.delete()
        await message_or_callback.message.answer_photo(
            photo=photo['urls']['regular'],
            caption=caption,
            reply_markup=get_keyboard(),
            parse_mode="Markdown"
        )
    else:
        await message_or_callback.answer_photo(
            photo=photo['urls']['regular'],
            caption=caption,
            reply_markup=get_keyboard(),
            parse_mode="Markdown"
        )


async def cmd_start(message: types.Message):
    """دستور /start"""
    text = (
        "🌄 **Unsplash Bot**\n\n"
        "📝 **دستورات:**\n"
        "• `/random` - عکس تصادفی\n"
        "• `/random nature` - با موضوع\n"
        "• `/search ocean` - جستجو\n\n"
        "💡 **در چت خصوصی:**\n"
        "فقط بنویس: `mountain`\n\n"
        f"⚡️ باقی: {unsplash.limiter.remaining()}/50"
    )
    await message.answer(text, parse_mode="Markdown")


async def cmd_random(message: types.Message):
    """دستور /random"""
    try:
        # استخراج query از دستور
        query = None
        if message.text and len(message.text.split()) > 1:
            query = " ".join(message.text.split()[1:])
        
        photo = await unsplash.get_random(query)
        await send_photo_message(message, photo)
    
    except Exception as e:
        await message.answer(f"❌ {str(e)}")


async def cmd_search(message: types.Message):
    """دستور /search"""
    try:
        # استخراج query
        if not message.text or len(message.text.split()) < 2:
            await message.answer("❌ مثال: `/search ocean`", parse_mode="Markdown")
            return
        
        query = " ".join(message.text.split()[1:])
        result = await unsplash.search(query)
        
        if result.get("results"):
            await send_photo_message(message, result["results"][0])
        else:
            await message.answer("❌ نتیجه‌ای نیست")
    
    except Exception as e:
        await message.answer(f"❌ {str(e)}")


async def handle_text(message: types.Message):
    """هندلر پیام‌های متنی (فقط چت خصوصی)"""
    # فقط در چت خصوصی
    if message.chat.type != "private":
        return
    
    query = message.text.strip()
    if not query:
        return
    
    try:
        result = await unsplash.search(query)
        
        if result.get("results"):
            await send_photo_message(message, result["results"][0])
        else:
            await message.answer("❌ نتیجه‌ای نیست")
    
    except Exception as e:
        await message.answer(f"❌ {str(e)}")


async def handle_refresh(callback: types.CallbackQuery):
    """هندلر دکمه refresh"""
    try:
        await callback.answer()
        photo = await unsplash.get_random()
        await send_photo_message(callback, photo)
    
    except Exception as e:
        await callback.message.answer(f"❌ {str(e)}")


async def main():
    global bot, unsplash
    
    # دریافت توکن‌ها
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    UNSPLASH_KEY = os.getenv("UNSPLASH_KEY")
    
    if not TELEGRAM_TOKEN or not UNSPLASH_KEY:
        logger.error("❌ لطفاً TELEGRAM_TOKEN و UNSPLASH_KEY را تنظیم کنید!")
        return
    
    # ساخت bot و unsplash
    bot = Bot(token=TELEGRAM_TOKEN)
    unsplash = UnsplashAPI(UNSPLASH_KEY)
    
    # ساخت dispatcher
    dp = Dispatcher()
    
    # ثبت handlers
    dp.message.register(cmd_start, Command(commands=["start", "help"]))
    dp.message.register(cmd_random, Command(commands=["random"]))
    dp.message.register(cmd_search, Command(commands=["search"]))
    dp.message.register(handle_text, F.text)
    dp.callback_query.register(handle_refresh, F.data == "refresh")
    
    try:
        logger.info("✅ ربات شروع شد")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    
    finally:
        await unsplash.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 ربات متوقف شد")
