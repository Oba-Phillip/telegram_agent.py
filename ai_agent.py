import os
import json
import logging
import asyncio
import requests
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from typing import Dict, Any, Optional

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("telegram_bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

class TelegramPoster:
    def __init__(self):
        self.bot: Optional[Bot] = None
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.scheduler = AsyncIOScheduler()
        self.config = self._load_config()
        self.rate_limit_delay = 1
        self._is_ready = asyncio.Event()

    def set_bot(self, bot: Bot):
        self.bot = bot

    def _load_config(self) -> Dict[str, Any]:
        try:
            with open('config.json', 'r') as f:
                config = json.load(f)
            img_path = config.get('image_path', '').strip()
            config['image_path'] = self._validate_image_path(img_path) or None
            return config
        except Exception as e:
            logger.error(f"Config loading failed: {e}")
            return {"scheduled_posts": []}

    def _validate_image_path(self, path: str) -> Optional[str]:
        try:
            path_obj = Path(path).expanduser().resolve()
            if path_obj.is_file() and os.access(path_obj, os.R_OK):
                return str(path_obj)
            logger.error(f"Invalid image path: {path_obj}")
        except Exception as e:
            logger.error(f"Path validation error: {e}")
        return None

    async def initialize(self):
        if not self.bot:
            raise RuntimeError("Bot instance not set")
        me = await self.bot.get_me()
        logger.info(f"Bot initialized as @{me.username}")
        self._setup_scheduler()
        self.scheduler.start()
        self._is_ready.set()

    def _setup_scheduler(self):
        for idx, post in enumerate(self.config.get("scheduled_posts", [])):
            try:
                post_time = datetime.strptime(post["time"], "%Y-%m-%d %H:%M:%S")
                repeat = post.get("repeat", "once")
                trigger = CronTrigger(
                    hour=post_time.hour,
                    minute=post_time.minute,
                    **({"day": "*"} if repeat == "daily" else
                       {"day_of_week": post_time.weekday()} if repeat == "weekly" else
                       {"day": post_time.day} if repeat == "monthly" else
                       {"year": post_time.year, "month": post_time.month, "day": post_time.day})
                )
                self.scheduler.add_job(self._send_post, trigger, args=[post], id=f"post_{idx}")
            except Exception as e:
                logger.error(f"Failed to schedule post: {e}")

    async def _send_post(self, post: Dict[str, Any]):
        try:
            if "media" in post:
                await self._send_media(post["text"], post["media"])
            else:
                await self._send_text(post["text"])
        except Exception as e:
            logger.error(f"Send post error: {e}")

    async def _send_text(self, text: str):
        await self.bot.send_message(chat_id=self.chat_id, text=text, parse_mode="HTML")
        logger.info(f"Sent message: {text[:50]}...")

    async def _send_media(self, caption: str, media_path: str):
        try:
            full_path = Path(media_path).expanduser().resolve()
            if not full_path.exists():
                logger.error(f"Media file not found: {full_path}")
                return

            url = f"https://api.telegram.org/bot{self.token}/sendPhoto"
            with open(full_path, "rb") as f:
                files = {"photo": f}
                data = {"chat_id": self.chat_id, "caption": caption}
                response = requests.post(url, data=data, files=files)

            if response.status_code == 200:
                logger.info(f"Successfully sent media: {caption[:50]}...")
            else:
                logger.error(f"Failed to send media: {response.text}")
        except Exception as e:
            logger.error(f"Error uploading media via HTTPS: {e}")

    async def send_default_image(self, update: Update):
        if not self._is_ready.is_set():
            await update.message.reply_text("Bot not ready yet.")
            return
        default_image = self.config.get("image_path")
        if not default_image:
            await update.message.reply_text("No default image configured.")
            return
        await self._send_media("Here's the requested image!", default_image)

    async def wait_until_ready(self):
        await self._is_ready.wait()

    async def shutdown(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self._is_ready.clear()
        logger.info("Scheduler stopped.")

async def command_send_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        poster = context.application.poster
        if not poster:
            await update.message.reply_text("Bot configuration missing.")
            return
        await asyncio.wait_for(poster.wait_until_ready(), timeout=10.0)
        await poster.send_default_image(update)
    except asyncio.TimeoutError:
        await update.message.reply_text("Bot initializing, try again soon.")
    except Exception as e:
        logger.error(f"Command error: {e}")
        await update.message.reply_text("Error processing your request.")

async def run_bot():
    application = None
    poster = None
    try:
        application = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
        poster = TelegramPoster()
        poster.set_bot(application.bot)
        application.poster = poster
        application.add_handler(CommandHandler("sendimage", command_send_image))
        await application.initialize()
        await application.start()
        await poster.initialize()
        await application.updater.start_polling()
        logger.info("Bot operational.")
        while True:
            await asyncio.sleep(3600)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        if poster:
            await poster.shutdown()
        if application:
            if hasattr(application, 'updater') and application.updater.running:
                await application.updater.stop()
            await application.stop()
            await application.shutdown()
        logger.info("Shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Startup failed: {e}")
