import asyncio
import aiohttp
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from collections import deque
from pathlib import Path
import logging
import os

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# تنظیم logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class RateLimiter:
    """مدیریت محدودیت 50 درخواست در ساعت"""
    
    def __init__(self, max_requests: int = 50, time_window: int = 3600):
        self.max_requests = max_requests
        self.time_window = time_window  # ثانیه
        self.requests: deque = deque()
    
    def can_request(self) -> bool:
        """آیا می‌تونیم درخواست بدیم؟"""
        now = datetime.now()
        
        # حذف درخواست‌های قدیمی (بیشتر از 1 ساعت)
        while self.requests and (now - self.requests[0]) > timedelta(seconds=self.time_window):
            self.requests.popleft()
        
        return len(self.requests) < self.max_requests
    
    def add_request(self):
        """ثبت درخواست جدید"""
        self.requests.append(datetime.now())
    
    def get_remaining(self) -> int:
        """درخواست‌های باقی‌مانده"""
        now = datetime.now()
        
        # حذف قدیمی‌ها
        while self.requests and (now - self.requests[0]) > timedelta(seconds=self.time_window):
            self.requests.popleft()
        
        return self.max_requests - len(self.requests)
    
    def get_reset_time(self) -> Optional[str]:
        """زمان ریست محدودیت"""
        if not self.requests:
            return None
        
        oldest = self.requests[0]
        reset_time = oldest + timedelta(seconds=self.time_window)
        remaining = (reset_time - datetime.now()).total_seconds()
        
        if remaining > 0:
            minutes = int(remaining // 60)
            seconds = int(remaining % 60)
            return f"{minutes}:{seconds:02d}"
        
        return None


class UnsplashAPI:
    """کلاینت ساده Unsplash با Rate Limit"""
    
    def __init__(self, access_key: str, secret_key: Optional[str] = None):
        self.access_key = access_key
        self.secret_key = secret_key
        self.base_url = "https://api.unsplash.com"
        self.rate_limiter = RateLimiter(max_requests=50, time_window=3600)
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def init_session(self):
        """ایجاد session"""
        if not self.session:
            headers = {
                "Authorization": f"Client-ID {self.access_key}",
                "Accept-Version": "v1"
            }
            
            self.session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            )
    
    async def close_session(self):
        """بستن session"""
        if self.session:
            await self.session.close()
    
    async def _request(self, endpoint: str, params: Dict) -> Dict:
        """درخواست با بررسی rate limit"""
        
        # بررسی محدودیت
        if not self.rate_limiter.can_request():
            reset_time = self.rate_limiter.get_reset_time()
            raise Exception(f"⏳ محدودیت ساعتی! ریست در: {reset_time}")
        
        await self.init_session()
        
        url = f"{self.base_url}{endpoint}"
        
        try:
            async with self.session.get(url, params=params) as response:
                
                # ثبت درخواست موفق
                self.rate_limiter.add_request()
                
                if response.status == 429:
                    raise Exception("⚠️ محدودیت API رسید!")
                
                if response.status >= 400:
                    error_text = await response.text()
                    raise Exception(f"❌ خطای API: {response.status}")
                
                return await response.json()
                
        except asyncio.TimeoutError:
            raise Exception("⏱️ زمان درخواست تمام شد")
        except aiohttp.ClientError as e:
            raise Exception(f"❌ خطای شبکه: {str(e)}")
    
    async def get_random_photo(
        self,
        query: Optional[str] = None,
        orientation: Optional[str] = None
    ) -> Dict:
        """دریافت عکس رندوم"""
        
        params = {}
        if query:
            params["query"] = query
        if orientation:
            params["orientation"] = orientation
        
        return await self._request("/photos/random", params)
    
    async def search_photos(
        self,
        query: str,
        page: int = 1,
        per_page: int = 10,
        orientation: Optional[str] = None,
        color: Optional[str] = None
    ) -> Dict:
        """جستجوی عکس"""
        
        params = {
            "query": query,
            "page": page,
            "per_page": per_page
        }
        
        if orientation:
            params["orientation"] = orientation
        if color:
            params["color"] = color
        
        return await self._request("/search/photos", params)
    
    async def track_download(self, download_location: str):
        """ثبت دانلود (الزامی طبق guidelines)"""
        
        await self.init_session()
        
        try:
            async with self.session.get(download_location):
                pass
        except:
            pass
    
    def get_stats(self) -> str:
        """آمار استفاده"""
        remaining = self.rate_limiter.get_remaining()
        reset_time = self.rate_limiter.get_reset_time()
        
        stats = f"📊 باقی‌مانده: {remaining}/50"
        
        if reset_time and remaining < 10:
            stats += f" | ⏳ ریست: {reset_time}"
        
        return stats


class UnsplashBot:
    """ربات تلگرام"""
    
    def __init__(self, telegram_token: str, unsplash_key: str, unsplash_secret: Optional[str] = None):
        self.app = Application.builder().token(telegram_token).build()
        self.api = UnsplashAPI(unsplash_key, unsplash_secret)
        self.bot_username = None  # برای چک کردن mention
        
        # ثبت handlers
        self._register_handlers()
    
    def _register_handlers(self):
        """ثبت دستورات"""
        # Commands (کار می‌کنه توی همه جا)
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("random", self.random_photo))
        self.app.add_handler(CommandHandler("stats", self.stats))
        self.app.add_handler(CommandHandler("search", self.search_command))
        
        # Text messages (فقط در شرایط خاص)
        self.app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self.handle_text_message
            )
        )
        
        # Callback queries
        self.app.add_handler(CallbackQueryHandler(self.button_handler))
    
    async def _should_respond_in_group(self, update: Update) -> bool:
        """آیا باید توی گروه جواب بدیم؟"""
        
        message = update.message
        
        # اگه توی چت خصوصی هست، همیشه جواب بده
        if message.chat.type == "private":
            return True
        
        # اگه توی گروه هست:
        
        # 1. اگه reply به پیام ربات باشه
        if message.reply_to_message and message.reply_to_message.from_user.id == self.app.bot.id:
            return True
        
        # 2. اگه ربات mention شده باشه (@bot_username)
        if self.bot_username:
            if f"@{self.bot_username}" in message.text:
                return True
        
        # 3. اگه متن با کلمات کلیدی شروع بشه
        text_lower = message.text.lower()
        keywords = ["عکس", "photo", "image", "picture", "پیک", "تصویر"]
        if any(text_lower.startswith(kw) for kw in keywords):
            return True
        
        return False
    
    async def handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """مدیریت پیام‌های متنی"""
        
        # بررسی آیا باید جواب بدیم
        if not await self._should_respond_in_group(update):
            return
        
        # پاک کردن mention از متن
        query = update.message.text
        if self.bot_username:
            query = query.replace(f"@{self.bot_username}", "").strip()
        
        # پاک کردن کلمات کلیدی از اول متن
        query_lower = query.lower()
        for keyword in ["عکس", "photo", "image", "picture", "پیک", "تصویر"]:
            if query_lower.startswith(keyword):
                query = query[len(keyword):].strip()
                break
        
        if not query:
            await update.message.reply_text(
                "❓ چی می‌خوای جستجو کنم؟\n"
                "مثال: `mountain` یا `city night`",
                parse_mode='Markdown'
            )
            return
        
        # جستجو
        await self.search_photos(update, context, query)
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """پیام خوش‌آمدگویی"""
        
        chat_type = update.effective_chat.type
        
        if chat_type == "private":
            welcome = (
                "🌄 **به ربات Unsplash خوش اومدی!**\n\n"
                "🔍 **جستجو:** فقط یه کلمه بفرست\n"
                "   مثال: `mountain` یا `city sunset`\n\n"
                "🎲 **رندوم:**\n"
                "   • `/random` - کاملاً تصادفی\n"
                "   • `/random nature` - با موضوع\n\n"
                "📊 **آمار:** `/stats`\n"
                "❓ **راهنما:** `/help`\n\n"
                "⚡️ محدودیت: 50 عکس در ساعت"
            )
        else:
            welcome = (
                "👋 سلام! من ربات Unsplash هستم\n\n"
                "🔍 **برای استفاده در گروه:**\n"
                "   • منشن کن: `@USERNAME mountain`\n"
                "   • ریپلای کن و بنویس: `sea`\n"
                "   • شروع با: `عکس mountain`\n\n"
                "📝 **دستورات:**\n"
                "   • `/random` - عکس رندوم\n"
                "   • `/search mountain` - جستجو\n"
                "   • `/stats` - آمار\n\n"
                "💡 تو چت خصوصی راحت‌تر کار می‌کنم!"
            )
        
        await update.message.reply_text(welcome, parse_mode='Markdown')
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """راهنما"""
        
        chat_type = update.effective_chat.type
        
        if chat_type == "private":
            help_text = (
                "📚 **راهنمای استفاده:**\n\n"
                "**1️⃣ جستجو:**\n"
                "فقط بنویس:\n"
                "   • `mountain`\n"
                "   • `city night`\n"
                "   • `nature green`\n\n"
                "**2️⃣ عکس رندوم:**\n"
                "   • `/random`\n"
                "   • `/random ocean`\n\n"
                "**3️⃣ جستجوی دستوری:**\n"
                "   • `/search mountain`\n\n"
                "**4️⃣ آمار:**\n"
                "   • `/stats`\n\n"
                "**🎨 فیلترها:**\n"
                "بعد از هر عکس:\n"
                "   • جهت: Landscape, Portrait\n"
                "   • رنگ: مشکی‌سفید، آبی، سبز\n"
                "   • عکس جدید: 🔄\n\n"
                "⚠️ محدودیت: 50 عکس/ساعت"
            )
        else:
            help_text = (
                "📚 **راهنما (گروه):**\n\n"
                "**🔸 روش 1 - منشن:**\n"
                "`@USERNAME mountain`\n\n"
                "**🔸 روش 2 - ریپلای:**\n"
                "روی پیامم ریپلای کن:\n"
                "`sea`\n\n"
                "**🔸 روش 3 - کلیدی:**\n"
                "   • `عکس mountain`\n"
                "   • `photo city`\n\n"
                "**📝 دستورات:**\n"
                "   • `/random` - تصادفی\n"
                "   • `/search mountain` - جستجو\n"
                "   • `/stats` - آمار\n\n"
                "💡 چت خصوصی راحت‌تره!"
            )
        
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """نمایش آمار"""
        
        remaining = self.api.rate_limiter.get_remaining()
        reset_time = self.api.rate_limiter.get_reset_time()
        
        stats = (
            "📊 **آمار استفاده:**\n\n"
            f"✅ باقی‌مانده: **{remaining}/50** درخواست\n"
        )
        
        if reset_time:
            if remaining == 0:
                stats += f"⏳ ریست در: **{reset_time}**\n"
            elif remaining < 10:
                stats += f"⚠️ ریست بعدی: **{reset_time}**\n"
        
        # نمایش نحوه استفاده در گروه
        if update.effective_chat.type != "private":
            stats += "\n💡 برای استفاده منشن کن یا ریپلای بده"
        
        await update.message.reply_text(stats, parse_mode='Markdown')
    
    async def search_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """دستور جستجو"""
        
        if not context.args:
            await update.message.reply_text(
                "❌ استفاده: `/search mountain`\n"
                "یا تو چت خصوصی: `mountain`",
                parse_mode='Markdown'
            )
            return
        
        query = " ".join(context.args)
        await self.search_photos(update, context, query)
    
    async def random_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """عکس رندوم"""
        
        # استخراج query از دستور
        query = None
        if context.args:
            query = " ".join(context.args)
        
        try:
            msg = await update.message.reply_text("🔄 در حال دریافت...")
            
            # دریافت عکس
            photo = await self.api.get_random_photo(query=query)
            
            # ارسال عکس
            await self._send_photo(update, photo, msg)
            
        except Exception as e:
            await update.message.reply_text(f"❌ خطا: {str(e)}")
    
    async def search_photos(self, update: Update, context: ContextTypes.DEFAULT_TYPE, query: str = None):
        """جستجوی عکس"""
        
        if not query:
            query = update.message.text.strip()
        
        if not query:
            await update.message.reply_text("❌ کلمه جستجو؟")
            return
        
        try:
            msg = await update.message.reply_text(f"🔍 جستجو: {query}...")
            
            # جستجو
            results = await self.api.search_photos(query=query, per_page=5)
            
            if not results.get("results"):
                await msg.edit_text("❌ نتیجه‌ای پیدا نشد!")
                return
            
            # ذخیره نتایج
            user_id = update.effective_user.id
            chat_id = update.effective_chat.id
            key = f"{chat_id}_{user_id}"
            
            if "search_data" not in context.bot_data:
                context.bot_data["search_data"] = {}
            
            context.bot_data["search_data"][key] = {
                "results": results["results"],
                "query": query,
                "index": 0
            }
            
            # ارسال اولین عکس
            await self._send_photo(update, results["results"][0], msg, show_navigation=True, key=key)
            
        except Exception as e:
            await update.message.reply_text(f"❌ خطا: {str(e)}")
    
    async def _send_photo(
        self,
        update: Update,
        photo: Dict,
        message_to_edit=None,
        show_navigation: bool = False,
        key: str = None
    ):
        """ارسال عکس با اطلاعات"""
        
        # Caption کوتاه برای گروه
        is_private = update.effective_chat.type == "private"
        
        if is_private:
            caption = (
                f"📸 **{photo.get('description') or photo.get('alt_description', 'بدون عنوان')}**\n\n"
                f"👤 [{photo['user']['name']}]({photo['user']['links']['html']})\n"
                f"💚 {photo.get('likes', 0)} | "
                f"📏 {photo['width']}x{photo['height']}\n\n"
                f"{self.api.get_stats()}"
            )
        else:
            caption = (
                f"📸 {photo.get('alt_description', 'عکس')}\n"
                f"👤 {photo['user']['name']} | "
                f"{self.api.get_stats()}"
            )
        
        # دکمه‌ها
        keyboard = []
        
        if show_navigation and key:
            keyboard.append([
                InlineKeyboardButton("⬅️", callback_data=f"prev_{key}"),
                InlineKeyboardButton("🔄", callback_data=f"refresh_{key}"),
                InlineKeyboardButton("➡️", callback_data=f"next_{key}")
            ])
        else:
            keyboard.append([
                InlineKeyboardButton("🔄 جدید", callback_data="refresh_random")
            ])
        
        if is_private:
            keyboard.append([
                InlineKeyboardButton("🎨 فیلتر", callback_data=f"filters_{key}" if key else "filters")
            ])
        
        keyboard.append([
            InlineKeyboardButton("🔗 Unsplash", url=photo['links']['html'])
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Track download
        if "links" in photo and "download_location" in photo["links"]:
            await self.api.track_download(photo["links"]["download_location"])
        
        # ارسال عکس
        try:
            # حذف پیام loading
            if message_to_edit:
                await message_to_edit.delete()
            
            await update.effective_chat.send_photo(
                photo=photo['urls']['regular'],
                caption=caption,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            
        except Exception as e:
            logger.error(f"خطا در ارسال: {e}")
            await update.effective_chat.send_message(f"❌ خطا: {str(e)}")
    
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """مدیریت دکمه‌ها"""
        
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        try:
            # رفرش رندوم
            if data == "refresh_random":
                photo = await self.api.get_random_photo()
                await query.message.delete()
                await self._send_photo(update, photo)
                return
            
            # استخراج اطلاعات
            parts = data.split("_", 1)
            action = parts[0]
            key = parts[1] if len(parts) > 1 else None
            
            # دریافت داده‌های جستجو
            search_data = None
            if key and "search_data" in context.bot_data:
                search_data = context.bot_data["search_data"].get(key)
            
            # ناوبری
            if action in ["next", "prev"] and search_data:
                results = search_data["results"]
                current_index = search_data["index"]
                
                if action == "next":
                    current_index = (current_index + 1) % len(results)
                else:
                    current_index = (current_index - 1) % len(results)
                
                search_data["index"] = current_index
                
                await query.message.delete()
                await self._send_photo(
                    update,
                    results[current_index],
                    show_navigation=True,
                    key=key
                )
            
            # رفرش با همون جستجو
            elif action == "refresh" and search_data:
                photo = await self.api.get_random_photo(query=search_data["query"])
                await query.message.delete()
                await self._send_photo(update, photo, show_navigation=True, key=key)
            
            # فیلترها
            elif action == "filters":
                keyboard = [
                    [
                        InlineKeyboardButton("🏔️ Landscape", callback_data=f"filter_landscape_{key}" if key else "filter_landscape"),
                        InlineKeyboardButton("📱 Portrait", callback_data=f"filter_portrait_{key}" if key else "filter_portrait")
                    ],
                    [
                        InlineKeyboardButton("⬛ B&W", callback_data=f"filter_black_and_white_{key}" if key else "filter_black_and_white"),
                        InlineKeyboardButton("🔵 آبی", callback_data=f"filter_blue_{key}" if key else "filter_blue")
                    ],
                    [
                        InlineKeyboardButton("🟢 سبز", callback_data=f"filter_green_{key}" if key else "filter_green"),
                        InlineKeyboardButton("🔴 قرمز", callback_data=f"filter_red_{key}" if key else "filter_red")
                    ],
                    [InlineKeyboardButton("🔙 بازگشت", callback_data="back")]
                ]
                
                await query.message.edit_reply_markup(InlineKeyboardMarkup(keyboard))
            
            # اعمال فیلتر
            elif action == "filter":
                parts = data.split("_", 2)
                filter_type = parts[1]
                key = parts[2] if len(parts) > 2 else None
                
                search_query = "random"
                if key and "search_data" in context.bot_data:
                    search_data = context.bot_data["search_data"].get(key)
                    if search_data:
                        search_query = search_data["query"]
                
                msg = await query.message.reply_text("🔄 فیلتر...")
                
                # جستجو با فیلتر
                if filter_type in ["landscape", "portrait", "squarish"]:
                    results = await self.api.search_photos(
                        query=search_query,
                        orientation=filter_type,
                        per_page=1
                    )
                else:
                    results = await self.api.search_photos(
                        query=search_query,
                        color=filter_type,
                        per_page=1
                    )
                
                if results.get("results"):
                    await query.message.delete()
                    await self._send_photo(update, results["results"][0], msg)
                else:
                    await msg.edit_text("❌ نتیجه‌ای نیست")
            
            # بازگشت
            elif action == "back":
                keyboard = [[InlineKeyboardButton("🔄", callback_data="refresh_random")]]
                await query.message.edit_reply_markup(InlineKeyboardMarkup(keyboard))
        
        except Exception as e:
            await query.message.reply_text(f"❌ خطا: {str(e)}")
    
    async def post_init(self, application: Application):
        """بعد از راه‌اندازی"""
        # دریافت username ربات
        bot = await application.bot.get_me()
        self.bot_username = bot.username
        logger.info(f"🤖 ربات: @{self.bot_username}")
    
    async def run(self):
        """اجرای ربات"""
        
        await self.api.init_session()
        
        try:
            await self.app.initialize()
            await self.post_init(self.app)
            await self.app.start()
            await self.app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            
            logger.info("✅ ربات در حال اجرا...")
            
            # Keep running
            await asyncio.Event().wait()
            
        finally:
            await self.api.close_session()
            await self.app.stop()


# ==================== اجرا ====================

async def main():
    # تنظیمات - از environment variable یا اینجا
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
    UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "YOUR_UNSPLASH_ACCESS_KEY")
    UNSPLASH_SECRET_KEY = os.getenv("UNSPLASH_SECRET_KEY")  # اختیاری
    
    # بررسی توکن‌ها
    if TELEGRAM_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        logger.error("❌ لطفاً توکن تلگرام رو وارد کن!")
        return
    
    if UNSPLASH_ACCESS_KEY == "YOUR_UNSPLASH_ACCESS_KEY":
        logger.error("❌ لطفاً Access Key Unsplash رو وارد کن!")
        return
    
    # ایجاد ربات
    bot = UnsplashBot(TELEGRAM_TOKEN, UNSPLASH_ACCESS_KEY, UNSPLASH_SECRET_KEY)
    
    # اجرا
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 ربات متوقف شد")
    except Exception as e:
        logger.error(f"❌ خطای کلی: {e}")
