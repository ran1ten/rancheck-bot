import os
import random
import time
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ВАЖНО: Эти переменные будут автоматически подставлены из main.py
codes_store = None
bot = None
loop = None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Нажми /getcode, чтобы получить код доступа к сайту."
    )

async def get_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = str(random.randint(100000, 999999))
    expire_time = time.time() + 600
    codes_store[code] = expire_time
    await update.message.reply_text(
        f"Ваш код: {code}\nДействителен 10 минут.", parse_mode="Markdown"
    )
    asyncio.create_task(delete_code_after(code, 600))

async def delete_code_after(code, delay):
    await asyncio.sleep(delay)
    codes_store.pop(code, None)

async def setup_bot(codes_ref, app_loop):
    global codes_store, bot, loop
    codes_store = codes_ref
    loop = app_loop
    bot = Application.builder().token(os.getenv("BOT_TOKEN")).build()
    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(CommandHandler("getcode", get_code))
    await bot.initialize()
    # Включаем webhook
    await bot.bot.set_webhook(f"{os.getenv('RENDER_EXTERNAL_URL')}/webhook")
    return bot