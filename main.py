import os
import random
import logging
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import uvicorn

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- 1. Конфигурация ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения")

ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://ranTen.github.io")

# Хранилище кодов
code_storage = {}

# ---------- 2. FastAPI приложение ----------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class CodeRequest(BaseModel):
    code: str

@app.post("/verify")
async def verify_code(request: CodeRequest):
    code = request.code.strip()
    expire_time = code_storage.get(code)
    if not expire_time:
        raise HTTPException(status_code=400, detail="Неверный или просроченный код")
    if datetime.now() > expire_time:
        code_storage.pop(code, None)
        raise HTTPException(status_code=400, detail="Код истек")
    code_storage.pop(code, None)
    import uuid
    access_token = str(uuid.uuid4())
    logger.info(f"Успешная проверка кода {code}, выдан токен {access_token[:8]}...")
    return {"success": True, "token": access_token}

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

# ---------- 3. Telegram-бот (обработчики) ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для выдачи одноразовых кодов.\n"
        "Напиши /getcode – получишь 6-значный код для входа на сайт."
    )

async def get_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = str(random.randint(100000, 999999))
    expires_at = datetime.now() + timedelta(minutes=10)
    code_storage[code] = expires_at
    await update.message.reply_text(
        f"🔑 Ваш код: `{code}`\n"
        f"⏳ Действителен до {expires_at.strftime('%H:%M:%S')} (10 минут).\n"
        "Введите его на сайте для входа.",
        parse_mode="Markdown"
    )
    asyncio.create_task(delete_code_after(code, 600))

async def delete_code_after(code: str, delay: int):
    await asyncio.sleep(delay)
    code_storage.pop(code, None)
    logger.info(f"Код {code} удалён по истечении времени")

# ---------- 4. Инициализация бота и установка вебхука ----------
async def init_bot() -> Application:
    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("getcode", get_code))
    # Обязательная инициализация и запуск
    await bot_app.initialize()
    await bot_app.start()
    # Устанавливаем вебхук
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        webhook_path = "/webhook"
        full_webhook_url = f"{render_url}{webhook_path}"
        await bot_app.bot.set_webhook(url=full_webhook_url)
        logger.info(f"✅ Вебхук установлен на {full_webhook_url}")
    else:
        logger.warning("RENDER_EXTERNAL_URL не задан, вебхук не установлен")
    return bot_app

# Глобальная переменная для бота
bot_application = None

@app.on_event("startup")
async def startup_event():
    global bot_application
    bot_application = await init_bot()
    logger.info("Бот инициализирован и готов принимать обновления")

@app.on_event("shutdown")
async def shutdown_event():
    global bot_application
    if bot_application:
        await bot_application.stop()
        await bot_application.shutdown()
        logger.info("Бот остановлен")

# ---------- 5. Эндпоинт вебхука ----------
@app.post("/webhook")
async def telegram_webhook(request: Request):
    global bot_application
    if not bot_application:
        raise HTTPException(status_code=500, detail="Бот не инициализирован")
    try:
        data = await request.json()
        update = Update.de_json(data, bot_application.bot)
        await bot_application.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Ошибка обработки вебхука: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error")

# ---------- 6. Запуск (для локального тестирования) ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
