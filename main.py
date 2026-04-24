import os
import random
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------- 1. НАСТРОЙКА ЛОГИРОВАНИЯ ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- 2. КОНФИГУРАЦИЯ ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения")

# Адрес вашего сайта на GitHub Pages (БЕЗ слеша в конце!)
ALLOWED_ORIGIN = "https://ran1ten.github.io"

# ---------- 3. ПОДКЛЮЧЕНИЕ К БАЗЕ ДАННЫХ ----------
DATABASE_URL = "logs.db"

def get_db():
    conn = sqlite3.connect(DATABASE_URL)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Создаёт таблицы, если их нет. Вызывается при старте приложения."""
    with get_db() as conn:
        conn.execute('''
        CREATE TABLE IF NOT EXISTS telegram_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            message TEXT,
            bot_response TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        conn.execute('''
        CREATE TABLE IF NOT EXISTS web_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT,
            entered_code TEXT,
            uploaded_mods TEXT,
            site_response TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        conn.commit()
    logger.info("База данных инициализирована")

# Функции логирования
def log_telegram(user_id: int, username: str, msg: str, bot_resp: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO telegram_logs (user_id, username, message, bot_response, timestamp) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, msg, bot_resp, datetime.now())
        )
        conn.commit()

def log_web(ip: str, code: str, mods: str, resp: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO web_logs (ip_address, entered_code, uploaded_mods, site_response, timestamp) VALUES (?, ?, ?, ?, ?)",
            (ip, code, mods, resp, datetime.now())
        )
        conn.commit()

# ---------- 4. FASTAPI ПРИЛОЖЕНИЕ ----------
app = FastAPI()

# CORS – разрешаем запросы с вашего сайта
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Хранилище одноразовых кодов
code_storage = {}

# Модель запроса для проверки кода
class CodeRequest(BaseModel):
    code: str

# ---------- 5. ЭНДПОИНТЫ API ----------

@app.post("/verify")
async def verify_code(request: CodeRequest, req: Request):
    client_ip = req.client.host
    code = request.code.strip()

    expire = code_storage.get(code)
    if not expire:
        log_web(client_ip, code, "", "Неверный или просроченный код")
        raise HTTPException(400, detail="Неверный или просроченный код")
    if datetime.now() > expire:
        code_storage.pop(code, None)
        log_web(client_ip, code, "", "Код истек")
        raise HTTPException(400, detail="Код истек")

    # Код верный – удаляем и выдаём токен
    code_storage.pop(code, None)
    token = str(uuid.uuid4())
    log_web(client_ip, code, "", "Доступ разрешён")
    return {"success": True, "token": token}

@app.get("/health")
async def health():
    return {"status": "ok"}

# ---------- API ДЛЯ АДМИН-ПАНЕЛИ ----------
@app.get("/api/telegram-logs")
async def get_telegram_logs(user_id: int = None, username: str = None):
    with get_db() as conn:
        query = "SELECT * FROM telegram_logs"
        params = []
        if user_id:
            query += " WHERE user_id = ?"
            params.append(user_id)
        elif username:
            query += " WHERE username = ?"
            params.append(username)
        query += " ORDER BY timestamp DESC"
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]

@app.get("/api/web-logs")
async def get_web_logs(ip: str = None):
    with get_db() as conn:
        if ip:
            rows = conn.execute("SELECT * FROM web_logs WHERE ip_address = ? ORDER BY timestamp DESC", (ip,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM web_logs ORDER BY timestamp DESC").fetchall()
    return [dict(row) for row in rows]

# ---------- 6. TELEGRAM БОТ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    response = "👋 Привет! Я бот для выдачи одноразовых кодов.\nНапиши /getcode"
    await update.message.reply_text(response)
    log_telegram(user.id, user.username, "/start", response)

async def get_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    code = str(random.randint(100000, 999999))
    expires_at = datetime.now() + timedelta(minutes=10)
    code_storage[code] = expires_at
    response = f"🔑 Ваш код: `{code}`\n⏳ Действителен до {expires_at.strftime('%H:%M:%S')} (10 минут).\nВведите его на сайте для входа."
    await update.message.reply_text(response, parse_mode="Markdown")
    log_telegram(user.id, user.username, "/getcode", response)

# Функция для установки вебхука при старте
async def setup_webhook(application: Application):
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not render_url:
        logger.error("RENDER_EXTERNAL_URL не задан, вебхук не будет работать")
        return False
    webhook_path = "/webhook"
    full_url = f"{render_url}{webhook_path}"
    await application.bot.set_webhook(url=full_url)
    logger.info(f"✅ Вебхук установлен на {full_url}")
    return True

# Глобальная переменная для бота
bot_app = None

@app.on_event("startup")
async def startup():
    global bot_app
    init_db()
    # Создаём и инициализируем бота
    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("getcode", get_code))
    await bot_app.initialize()
    await bot_app.start()
    await setup_webhook(bot_app)
    logger.info("Бот запущен и готов принимать сообщения")

@app.on_event("shutdown")
async def shutdown():
    global bot_app
    if bot_app:
        await bot_app.stop()
        await bot_app.shutdown()
        logger.info("Бот остановлен")

# Эндпоинт для вебхука Telegram
@app.post("/webhook")
async def webhook(request: Request):
    global bot_app
    if not bot_app:
        raise HTTPException(500, "Бот не инициализирован")
    try:
        data = await request.json()
        update = Update.de_json(data, bot_app.bot)
        await bot_app.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Ошибка вебхука: {e}", exc_info=True)
        raise HTTPException(500, "Internal error")

# ---------- 7. ТОЧКА ВХОДА ДЛЯ UVICORN ----------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
