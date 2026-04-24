import os
import random
import logging
import sqlite3
import uuid
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Request, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import secrets

# ==================== НАСТРОЙКИ ====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

ALLOWED_ORIGIN = "https://ran1ten.github.io"

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise ValueError("ADMIN_PASSWORD не задан")

ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID")
if ADMIN_USER_ID:
    try:
        ADMIN_USER_ID = int(ADMIN_USER_ID)
    except ValueError:
        ADMIN_USER_ID = None
        logger.warning("ADMIN_USER_ID должен быть числом")
else:
    ADMIN_USER_ID = None

WHITELIST_ENABLED = os.environ.get("WHITELIST_ENABLED", "false").lower() == "true"

DATABASE_URL = "logs.db"

# ==================== БАЗА ДАННЫХ ====================
def get_db():
    conn = sqlite3.connect(DATABASE_URL)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
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
        conn.execute('''
        CREATE TABLE IF NOT EXISTS whitelist (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER,
            added_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        conn.commit()
    logger.info("База данных инициализирована")

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

def is_whitelisted(user_id: int) -> bool:
    if not WHITELIST_ENABLED:
        return True
    with get_db() as conn:
        row = conn.execute("SELECT 1 FROM whitelist WHERE user_id = ?", (user_id,)).fetchone()
        return row is not None

def add_to_whitelist(user_id: int, added_by: int) -> bool:
    try:
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO whitelist (user_id, added_by) VALUES (?, ?)", (user_id, added_by))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Ошибка добавления в whitelist: {e}")
        return False

def remove_from_whitelist(user_id: int) -> bool:
    try:
        with get_db() as conn:
            conn.execute("DELETE FROM whitelist WHERE user_id = ?", (user_id,))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Ошибка удаления из whitelist: {e}")
        return False

def get_whitelist():
    with get_db() as conn:
        rows = conn.execute("SELECT user_id, added_by, added_at FROM whitelist ORDER BY added_at").fetchall()
        return [dict(row) for row in rows]

# ==================== FASTAPI ====================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBasic()
def verify_auth(credentials: HTTPBasicCredentials = Depends(security)):
    if not (secrets.compare_digest(credentials.username, ADMIN_USERNAME) and
            secrets.compare_digest(credentials.password, ADMIN_PASSWORD)):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return True

code_storage = {}

class CodeRequest(BaseModel):
    code: str

class WebActionLog(BaseModel):
    mod_name: str
    verdict: str

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
    code_storage.pop(code, None)
    token = str(uuid.uuid4())
    log_web(client_ip, code, "", "Доступ разрешён")
    return {"success": True, "token": token}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/api/log-web-action")
async def log_web_action(log: WebActionLog, req: Request):
    client_ip = req.client.host
    with get_db() as conn:
        conn.execute(
            "INSERT INTO web_logs (ip_address, entered_code, uploaded_mods, site_response, timestamp) VALUES (?, ?, ?, ?, ?)",
            (client_ip, "", f"{log.mod_name} -> {log.verdict}", "OK", datetime.now())
        )
        conn.commit()
    return {"status": "ok"}

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(auth: bool = Depends(verify_auth)):
    try:
        with open("admin.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        raise HTTPException(500, "admin.html not found")

@app.get("/api/telegram-logs")
async def get_telegram_logs(auth: bool = Depends(verify_auth), user_id: int = None, username: str = None):
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
async def get_web_logs(auth: bool = Depends(verify_auth), ip: str = None):
    with get_db() as conn:
        if ip:
            rows = conn.execute("SELECT * FROM web_logs WHERE ip_address = ? ORDER BY timestamp DESC", (ip,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM web_logs ORDER BY timestamp DESC").fetchall()
    return [dict(row) for row in rows]

# ==================== ТЕЛЕГРАМ БОТ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    response = "👋 Привет! Я бот для выдачи одноразовых кодов.\nНапиши /getcode"
    await update.message.reply_text(response)
    log_telegram(user.id, user.username, "/start", response)

async def get_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if WHITELIST_ENABLED and not is_whitelisted(user.id):
        response = "❌ Вы не в белом списке. Доступ запрещён."
        await update.message.reply_text(response)
        log_telegram(user.id, user.username, "/getcode", response)
        return
    code = str(random.randint(100000, 999999))
    expires_at = datetime.now() + timedelta(minutes=10)
    code_storage[code] = expires_at
    response = f"🔑 Ваш код: `{code}`\n⏳ Действителен до {expires_at.strftime('%H:%M:%S')} (10 минут).\nВведите его на сайте для входа."
    await update.message.reply_text(response, parse_mode="Markdown")
    log_telegram(user.id, user.username, "/getcode", response)

# ==================== АДМИН-КОМАНДЫ ====================
def is_admin(update: Update) -> bool:
    if not ADMIN_USER_ID:
        return False
    return update.effective_user.id == ADMIN_USER_ID

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Нет прав.")
        return
    with get_db() as conn:
        users_count = conn.execute("SELECT COUNT(DISTINCT user_id) FROM telegram_logs").fetchone()[0]
        codes_count = conn.execute("SELECT COUNT(*) FROM telegram_logs WHERE message = '/getcode'").fetchone()[0]
        web_checks = conn.execute("SELECT COUNT(*) FROM web_logs WHERE uploaded_mods != ''").fetchone()[0]
        logins = conn.execute("SELECT COUNT(*) FROM web_logs WHERE site_response = 'Доступ разрешён'").fetchone()[0]
        whitelist_count = conn.execute("SELECT COUNT(*) FROM whitelist").fetchone()[0]
    msg = (
        f"📊 **Статистика**\n"
        f"👥 Пользователей в Telegram: {users_count}\n"
        f"🔑 Выдано кодов: {codes_count}\n"
        f"🌐 Проверок модов на сайте: {web_checks}\n"
        f"✅ Успешных входов: {logins}\n"
        f"📋 В белом списке: {whitelist_count}\n"
        f"⚙️ Режим whitelist: {'Включён' if WHITELIST_ENABLED else 'Выключен'}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Нет прав.")
        return
    limit = 10
    if context.args and context.args[0].isdigit():
        limit = int(context.args[0])
    with get_db() as conn:
        rows = conn.execute(
            "SELECT timestamp, user_id, username, message, bot_response FROM telegram_logs ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
    if not rows:
        await update.message.reply_text("Логов нет.")
        return
    msg = "📜 **Последние логи Telegram:**\n\n"
    for row in rows:
        msg += f"🕒 {row['timestamp']}\n👤 {row['user_id']} (@{row['username'] or '?'})\n💬 {row['message']}\n🤖 {row['bot_response'][:100]}\n\n"
        if len(msg) > 3800:
            await update.message.reply_text(msg)
            msg = ""
    if msg:
        await update.message.reply_text(msg, parse_mode="Markdown")

async def web_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Нет прав.")
        return
    limit = 10
    if context.args and context.args[0].isdigit():
        limit = int(context.args[0])
    with get_db() as conn:
        rows = conn.execute(
            "SELECT timestamp, ip_address, entered_code, uploaded_mods, site_response FROM web_logs ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
    if not rows:
        await update.message.reply_text("Логов сайта нет.")
        return
    msg = "🌐 **Последние логи сайта:**\n\n"
    for row in rows:
        msg += f"🕒 {row['timestamp']}\n🌍 IP: {row['ip_address']}\n🔑 Код: {row['entered_code']}\n📦 Моды: {row['uploaded_mods'][:100]}\n📝 Ответ: {row['site_response']}\n\n"
        if len(msg) > 3800:
            await update.message.reply_text(msg)
            msg = ""
    if msg:
        await update.message.reply_text(msg, parse_mode="Markdown")

async def user_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Нет прав.")
        return
    if not context.args:
        await update.message.reply_text("Укажите user_id: /user_logs 123456789")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return
    with get_db() as conn:
        rows = conn.execute(
            "SELECT timestamp, message, bot_response FROM telegram_logs WHERE user_id = ? ORDER BY timestamp DESC LIMIT 20",
            (uid,)
        ).fetchall()
    if not rows:
        await update.message.reply_text(f"Логов для пользователя {uid} не найдено.")
        return
    msg = f"📄 **Логи пользователя {uid}:**\n\n"
    for row in rows:
        msg += f"🕒 {row['timestamp']}\n💬 {row['message']}\n🤖 {row['bot_response'][:100]}\n\n"
        if len(msg) > 3800:
            await update.message.reply_text(msg)
            msg = ""
    if msg:
        await update.message.reply_text(msg, parse_mode="Markdown")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Нет прав.")
        return
    if not context.args:
        await update.message.reply_text("Укажите текст рассылки после команды.\nПример: /broadcast Всем привет!")
        return
    text = " ".join(context.args)
    with get_db() as conn:
        users = conn.execute("SELECT DISTINCT user_id FROM telegram_logs").fetchall()
    if not users:
        await update.message.reply_text("Нет пользователей для рассылки.")
        return
    sent = 0
    failed = 0
    for row in users:
        uid = row['user_id']
        try:
            await context.bot.send_message(chat_id=uid, text=f"📢 *Объявление:* {text}", parse_mode="Markdown")
            sent += 1
        except Exception as e:
            logger.error(f"Не удалось отправить {uid}: {e}")
            failed += 1
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"Рассылка завершена: отправлено {sent}, ошибок {failed}.")

async def whitelist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Нет прав.")
        return
    users = get_whitelist()
    if not users:
        await update.message.reply_text("Белый список пуст.")
        return
    msg = "📋 **Белый список:**\n\n"
    for u in users:
        msg += f"• `{u['user_id']}` (добавлен {u['added_at']})\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def whitelist_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Нет прав.")
        return
    if not context.args:
        await update.message.reply_text("Укажите user_id: /whitelist_add 123456789")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return
    if add_to_whitelist(uid, update.effective_user.id):
        await update.message.reply_text(f"✅ Пользователь `{uid}` добавлен в белый список.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Ошибка при добавлении.")

async def whitelist_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Нет прав.")
        return
    if not context.args:
        await update.message.reply_text("Укажите user_id: /whitelist_remove 123456789")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return
    if remove_from_whitelist(uid):
        await update.message.reply_text(f"✅ Пользователь `{uid}` удалён из белого списка.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Ошибка при удалении.")

async def list_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Нет прав.")
        return
    msg = (
        "🤖 **Доступные команды администратора:**\n\n"
        "/stats – Статистика использования бота и сайта\n"
        "/logs [N] – Последние N логов Telegram (по умолч. 10)\n"
        "/web_logs [N] – Последние N логов сайта\n"
        "/user_logs <ID> – Логи конкретного пользователя\n"
        "/broadcast <текст> – Массовая рассылка всем пользователям\n"
        "/whitelist – Показать белый список\n"
        "/whitelist_add <ID> – Добавить пользователя в белый список\n"
        "/whitelist_remove <ID> – Удалить из белого списка\n"
        "/list – Показать это сообщение\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# Обработчик всех остальных текстовых сообщений (логирование)
async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg_text = update.message.text
    log_telegram(user.id, user.username, msg_text, "(неизвестная команда, ответ не отправлен)")
    # Не отвечаем, чтобы не раздражать

# ==================== ВЕБХУК И ЗАПУСК ====================
async def setup_webhook(application: Application):
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not render_url:
        logger.error("RENDER_EXTERNAL_URL не задан")
        return False
    full_url = f"{render_url}/webhook"
    await application.bot.set_webhook(url=full_url)
    logger.info(f"Вебхук установлен на {full_url}")
    return True

bot_app = None

@app.on_event("startup")
async def startup():
    global bot_app
    init_db()
    bot_app = Application.builder().token(BOT_TOKEN).build()
    # Общие команды
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("getcode", get_code))
    # Обработчик всех остальных текстовых сообщений (логирование)
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    # Админ-команды
    if ADMIN_USER_ID:
        bot_app.add_handler(CommandHandler("stats", stats))
        bot_app.add_handler(CommandHandler("logs", logs))
        bot_app.add_handler(CommandHandler("web_logs", web_logs))
        bot_app.add_handler(CommandHandler("user_logs", user_logs))
        bot_app.add_handler(CommandHandler("broadcast", broadcast))
        bot_app.add_handler(CommandHandler("whitelist", whitelist_cmd))
        bot_app.add_handler(CommandHandler("whitelist_add", whitelist_add))
        bot_app.add_handler(CommandHandler("whitelist_remove", whitelist_remove))
        bot_app.add_handler(CommandHandler("list", list_commands))
        logger.info(f"Админ-команды включены для user_id={ADMIN_USER_ID}")
    else:
        logger.warning("Админ-команды отключены")
    await bot_app.initialize()
    await bot_app.start()
    await setup_webhook(bot_app)
    logger.info("Бот запущен")

@app.on_event("shutdown")
async def shutdown():
    global bot_app
    if bot_app:
        await bot_app.stop()
        await bot_app.shutdown()
        logger.info("Бот остановлен")

@app.post("/webhook")
async def webhook(request: Request):
    global bot_app
    if not bot_app:
        raise HTTPException(500, "Bot not initialized")
    try:
        data = await request.json()
        update = Update.de_json(data, bot_app.bot)
        await bot_app.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        raise HTTPException(500, "Internal error")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
