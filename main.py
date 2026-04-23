import os
import random
import logging
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
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

# Разрешённый адрес вашего сайта (GitHub Pages)
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://ranTen.github.io")  # замените на свой

# Хранилище кодов (простой словарь, для демо)
code_storage = {}

# ---------- 2. FastAPI приложение ----------
app = FastAPI()

# CORS – чтобы ваш сайт мог вызывать API
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Модель запроса для проверки кода
class CodeRequest(BaseModel):
    code: str

# Эндпоинт для проверки кода (вызывается с сайта)
@app.post("/verify")
async def verify_code(request: CodeRequest):
    code = request.code.strip()
    expire_time = code_storage.get(code)
    if not expire_time:
        raise HTTPException(status_code=400, detail="Неверный или просроченный код")
    if datetime.now() > expire_time:
        code_storage.pop(code, None)
        raise HTTPException(status_code=400, detail="Код истек")
    # Код верный – удаляем его (одноразовый) и возвращаем токен
    code_storage.pop(code, None)
    import uuid
    access_token = str(uuid.uuid4())
    logger.info(f"Успешная проверка кода {code}, выдан токен {access_token[:8]}...")
    return {"success": True, "token": access_token}

# Эндпоинт для проверки работоспособности (health check)
@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

# ---------- 3. Telegram-бот (обработчики команд) ----------
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
    # Автоматическое удаление через 10 минут
    asyncio.create_task(delete_code_after(code, 600))

async def delete_code_after(code: str, delay: int):
    await asyncio.sleep(delay)
    code_storage.pop(code, None)
    logger.info(f"Код {code} удалён по истечении времени")

# ---------- 4. Настройка вебхука для Telegram ----------
async def setup_webhook(application: Application):
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not render_url:
        logger.error("RENDER_EXTERNAL_URL не задан, вебхук не будет работать")
        return False
    webhook_path = "/webhook"
    full_webhook_url = f"{render_url}{webhook_path}"
    await application.bot.set_webhook(url=full_webhook_url)
    logger.info(f"✅ Вебхук установлен на {full_webhook_url}")
    return True

# ---------- 5. Событие запуска FastAPI (инициализация бота и вебхука) ----------
@app.on_event("startup")
async def startup_event():
    # Создаём приложение бота
    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("getcode", get_code))
    
    # Устанавливаем вебхук
    success = await setup_webhook(bot_app)
    if not success:
        logger.warning("Вебхук не установлен, бот не будет получать обновления")
    
    # Сохраняем bot_app в состояние приложения, чтобы использовать в вебхуке
    app.state.bot_app = bot_app

# ---------- 6. Эндпоинт для вебхука Telegram ----------
from fastapi import Request
@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Принимает обновления от Telegram и передаёт их боту."""
    try:
        data = await request.json()
        update = Update.de_json(data, app.state.bot_app.bot)
        await app.state.bot_app.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Ошибка обработки вебхука: {e}")
        raise HTTPException(status_code=500, detail="Internal error")

# ---------- 7. Точка входа (если запускаем файл напрямую) ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
