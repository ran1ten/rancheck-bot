import os
import logging
import random
import asyncio
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Настройка логирования (полезно для отладки на Render)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Получаем токен из переменных окружения (устанавливается на Render)
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения")

# Хранилище кодов (простая глобальная переменная)
# В реальном проекте лучше использовать Redis или базу данных, но для демо хватит.
code_storage = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    await update.message.reply_text(
        "Привет! Я бот для выдачи кодов доступа к сайту.\n"
        "Напиши /getcode, чтобы получить одноразовый код."
    )

async def get_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерация 6-значного кода, действительного 10 минут"""
    code = str(random.randint(100000, 999999))
    expires_at = datetime.now() + timedelta(minutes=10)
    code_storage[code] = expires_at
    await update.message.reply_text(
        f"🔑 Ваш код: `{code}`\n"
        f"⏳ Действителен до {expires_at.strftime('%H:%M:%S')} (10 минут).\n"
        "Введите его на сайте для входа.",
        parse_mode="Markdown"
    )
    # Автоматически удалим код через 10 минут
    asyncio.create_task(delete_code_after(code, 600))

async def delete_code_after(code: str, delay: int):
    await asyncio.sleep(delay)
    code_storage.pop(code, None)
    logger.info(f"Код {code} истёк и удалён")

# Основная функция – запуск бота в режиме вебхука
def main():
    # Создаём приложение
    app = Application.builder().token(TOKEN).build()
    
    # Регистрируем команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("getcode", get_code))
    
    # Для работы на Render используем вебхук
    # Render передаёт порт через переменную PORT и внешний URL через RENDER_EXTERNAL_URL
    port = int(os.environ.get("PORT", 8080))
    webhook_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not webhook_url:
        logger.error("RENDER_EXTERNAL_URL не задан, вебхук не будет работать")
        return
    
    # Путь, на который Telegram будет присылать обновления
    webhook_path = "/webhook"
    full_webhook_url = f"{webhook_url}{webhook_path}"
    
    logger.info(f"Запуск вебхука на порту {port}, URL: {full_webhook_url}")
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=full_webhook_url,
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
