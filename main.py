import os
import random
import logging
import uuid
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Request, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import secrets

# PostgreSQL поддержка (если доступен psycopg2 и задана DATABASE_URL)
try:
    import psycopg2
    from psycopg2 import pool
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Конфигурация из переменных окружения ----------
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

# ---------- Выбор базы данных (PostgreSQL или SQLite) ----------
DATABASE_URL = os.environ.get("DATABASE_URL")
USE_POSTGRES = False
conn_pool = None

if PSYCOPG2_AVAILABLE and DATABASE_URL:
    USE_POSTGRES = True
    try:
        conn_pool = psycopg2.pool.SimpleConnectionPool(1, 10, dsn=DATABASE_URL)
        logger.info("PostgreSQL пул соединений создан")
    except Exception as e:
        logger.error(f"Ошибка подключения к PostgreSQL: {e}. Будет использован SQLite.")
        USE_POSTGRES = False

# Функции для получения/возврата соединения
def get_db_conn():
    if USE_POSTGRES:
        return conn_pool.getconn()
    else:
        import sqlite3
        return sqlite3.connect("logs.db")

def put_db_conn(conn):
    if USE_POSTGRES:
        conn_pool.putconn(conn)
    else:
        conn.close()

# ---------- Инициализация таблиц (BIGINT для PostgreSQL) ----------
def init_db():
    if USE_POSTGRES:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS telegram_logs (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username TEXT,
                message TEXT,
                bot_response TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS web_logs (
                id SERIAL PRIMARY KEY,
                ip_address TEXT,
                entered_code TEXT,
                uploaded_mods TEXT,
                site_response TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS whitelist (
                user_id BIGINT PRIMARY KEY,
                added_by BIGINT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        cur.close()
        put_db_conn(conn)
        logger.info("Таблицы PostgreSQL созданы/проверены (BIGINT)")
    else:
        import sqlite3
        with sqlite3.connect("logs.db") as conn:
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
        logger.info("Таблицы SQLite созданы/проверены")

# ---------- Функции логирования ----------
def log_telegram(user_id: int, username: str, msg: str, bot_resp: str):
    conn = get_db_conn()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO telegram_logs (user_id, username, message, bot_response, timestamp) VALUES (%s, %s, %s, %s, %s)",
                (user_id, username, msg, bot_resp, datetime.now())
            )
            conn.commit()
            cur.close()
        else:
            conn.execute(
                "INSERT INTO telegram_logs (user_id, username, message, bot_response, timestamp) VALUES (?, ?, ?, ?, ?)",
                (user_id, username, msg, bot_resp, datetime.now())
            )
            conn.commit()
    finally:
        put_db_conn(conn)

def log_web(ip: str, code: str, mods: str, resp: str):
    conn = get_db_conn()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO web_logs (ip_address, entered_code, uploaded_mods, site_response, timestamp) VALUES (%s, %s, %s, %s, %s)",
                (ip, code, mods, resp, datetime.now())
            )
            conn.commit()
            cur.close()
        else:
            conn.execute(
                "INSERT INTO web_logs (ip_address, entered_code, uploaded_mods, site_response, timestamp) VALUES (?, ?, ?, ?, ?)",
                (ip, code, mods, resp, datetime.now())
            )
            conn.commit()
    finally:
        put_db_conn(conn)

# ---------- Белый список ----------
def is_whitelisted(user_id: int) -> bool:
    if not WHITELIST_ENABLED:
        return True
    conn = get_db_conn()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM whitelist WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            cur.close()
        else:
            cur = conn.execute("SELECT 1 FROM whitelist WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
        return row is not None
    finally:
        put_db_conn(conn)

def add_to_whitelist(user_id: int, added_by: int) -> bool:
    conn = get_db_conn()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute("INSERT INTO whitelist (user_id, added_by) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING", (user_id, added_by))
            conn.commit()
            cur.close()
        else:
            conn.execute("INSERT OR IGNORE INTO whitelist (user_id, added_by) VALUES (?, ?)", (user_id, added_by))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Ошибка добавления в whitelist: {e}")
        return False
    finally:
        put_db_conn(conn)

def remove_from_whitelist(user_id: int) -> bool:
    conn = get_db_conn()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute("DELETE FROM whitelist WHERE user_id = %s", (user_id,))
            conn.commit()
            cur.close()
        else:
            conn.execute("DELETE FROM whitelist WHERE user_id = ?", (user_id,))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Ошибка удаления из whitelist: {e}")
        return False
    finally:
        put_db_conn(conn)

def get_whitelist():
    conn = get_db_conn()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute("SELECT user_id, added_by, added_at FROM whitelist ORDER BY added_at")
            rows = cur.fetchall()
            cur.close()
            return [{"user_id": r[0], "added_by": r[1], "added_at": r[2]} for r in rows]
        else:
            cur = conn.execute("SELECT user_id, added_by, added_at FROM whitelist ORDER BY added_at")
            rows = cur.fetchall()
            return [{"user_id": r[0], "added_by": r[1], "added_at": r[2]} for r in rows]
    finally:
        put_db_conn(conn)

# ---------- FastAPI приложение ----------
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
    log_web(client_ip, "", f"{log.mod_name} -> {log.verdict}", "OK")
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
    conn = get_db_conn()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            query = "SELECT * FROM telegram_logs"
            params = []
            if user_id:
                query += " WHERE user_id = %s"
                params.append(user_id)
            elif username:
                query += " WHERE username = %s"
                params.append(username)
            query += " ORDER BY timestamp DESC"
            cur.execute(query, params)
            rows = cur.fetchall()
            cur.close()
            return [{"id": r[0], "user_id": r[1], "username": r[2], "message": r[3], "bot_response": r[4], "timestamp": r[5]} for r in rows]
        else:
            import sqlite3
            conn.row_factory = sqlite3.Row
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
    finally:
        put_db_conn(conn)

@app.get("/api/web-logs")
async def get_web_logs(auth: bool = Depends(verify_auth), ip: str = None):
    conn = get_db_conn()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            if ip:
                cur.execute("SELECT * FROM web_logs WHERE ip_address = %s ORDER BY timestamp DESC", (ip,))
            else:
                cur.execute("SELECT * FROM web_logs ORDER BY timestamp DESC")
            rows = cur.fetchall()
            cur.close()
            return [{"id": r[0], "ip_address": r[1], "entered_code": r[2], "uploaded_mods": r[3], "site_response": r[4], "timestamp": r[5]} for r in rows]
        else:
            import sqlite3
            conn.row_factory = sqlite3.Row
            if ip:
                rows = conn.execute("SELECT * FROM web_logs WHERE ip_address = ? ORDER BY timestamp DESC", (ip,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM web_logs ORDER BY timestamp DESC").fetchall()
            return [dict(row) for row in rows]
    finally:
        put_db_conn(conn)

# ---------- Telegram бот ----------
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

def is_admin(update: Update) -> bool:
    if not ADMIN_USER_ID:
        return False
    return update.effective_user.id == ADMIN_USER_ID

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Нет прав.")
        return
    conn = get_db_conn()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(DISTINCT user_id) FROM telegram_logs")
            users_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM telegram_logs WHERE message = '/getcode'")
            codes_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM web_logs WHERE uploaded_mods != ''")
            web_checks = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM web_logs WHERE site_response = 'Доступ разрешён'")
            logins = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM whitelist")
            whitelist_count = cur.fetchone()[0]
            cur.close()
        else:
            cur = conn.execute("SELECT COUNT(DISTINCT user_id) FROM telegram_logs")
            users_count = cur.fetchone()[0]
            cur = conn.execute("SELECT COUNT(*) FROM telegram_logs WHERE message = '/getcode'")
            codes_count = cur.fetchone()[0]
            cur = conn.execute("SELECT COUNT(*) FROM web_logs WHERE uploaded_mods != ''")
            web_checks = cur.fetchone()[0]
            cur = conn.execute("SELECT COUNT(*) FROM web_logs WHERE site_response = 'Доступ разрешён'")
            logins = cur.fetchone()[0]
            cur = conn.execute("SELECT COUNT(*) FROM whitelist")
            whitelist_count = cur.fetchone()[0]
    finally:
        put_db_conn(conn)
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
    conn = get_db_conn()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute("SELECT timestamp, user_id, username, message, bot_response FROM telegram_logs ORDER BY timestamp DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
            cur.close()
        else:
            rows = conn.execute("SELECT timestamp, user_id, username, message, bot_response FROM telegram_logs ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
    finally:
        put_db_conn(conn)
    if not rows:
        await update.message.reply_text("Логов нет.")
        return
    msg = "📜 **Последние логи Telegram:**\n\n"
    for row in rows:
        ts, uid, uname, msg_text, bot_resp = row[0], row[1], row[2], row[3], row[4]
        uname = uname or '?'
        msg += f"🕒 {ts}\n👤 {uid} (@{uname})\n💬 {msg_text}\n🤖 {bot_resp[:100]}\n\n"
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
    conn = get_db_conn()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute("SELECT timestamp, ip_address, entered_code, uploaded_mods, site_response FROM web_logs ORDER BY timestamp DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
            cur.close()
        else:
            rows = conn.execute("SELECT timestamp, ip_address, entered_code, uploaded_mods, site_response FROM web_logs ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
    finally:
        put_db_conn(conn)
    if not rows:
        await update.message.reply_text("Логов сайта нет.")
        return
    msg = "🌐 **Последние логи сайта:**\n\n"
    for row in rows:
        ts, ip, code, mods, resp = row[0], row[1], row[2], row[3], row[4]
        msg += f"🕒 {ts}\n🌍 IP: {ip}\n🔑 Код: {code}\n📦 Моды: {mods[:100]}\n📝 Ответ: {resp}\n\n"
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
    conn = get_db_conn()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute("SELECT timestamp, message, bot_response FROM telegram_logs WHERE user_id = %s ORDER BY timestamp DESC LIMIT 20", (uid,))
            rows = cur.fetchall()
            cur.close()
        else:
            rows = conn.execute("SELECT timestamp, message, bot_response FROM telegram_logs WHERE user_id = ? ORDER BY timestamp DESC LIMIT 20", (uid,)).fetchall()
    finally:
        put_db_conn(conn)
    if not rows:
        await update.message.reply_text(f"Логов для пользователя {uid} не найдено.")
        return
    msg = f"📄 **Логи пользователя {uid}:**\n\n"
    for row in rows:
        ts, msg_text, bot_resp = row[0], row[1], row[2]
        msg += f"🕒 {ts}\n💬 {msg_text}\n🤖 {bot_resp[:100]}\n\n"
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
        await update.message.reply_text("Укажите текст рассылки.\nПример: /broadcast Всем привет!")
        return
    text = " ".join(context.args)
    conn = get_db_conn()
    try:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT user_id FROM telegram_logs")
            users = cur.fetchall()
            cur.close()
        else:
            users = conn.execute("SELECT DISTINCT user_id FROM telegram_logs").fetchall()
    finally:
        put_db_conn(conn)
    if not users:
        await update.message.reply_text("Нет пользователей для рассылки.")
        return
    sent = 0
    failed = 0
    for (uid,) in users:
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
        "/logs [N] – Последние N логов Telegram\n"
        "/web_logs [N] – Последние N логов сайта\n"
        "/user_logs <ID> – Логи конкретного пользователя\n"
        "/broadcast <текст> – Массовая рассылка всем пользователям\n"
        "/whitelist – Показать белый список\n"
        "/whitelist_add <ID> – Добавить в белый список\n"
        "/whitelist_remove <ID> – Удалить из белого списка\n"
        "/list – Показать это сообщение\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg_text = update.message.text
    log_telegram(user.id, user.username, msg_text, "(неизвестная команда, ответ не отправлен)")

# ---------- Вебхук и запуск бота ----------
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
    # Логирование всех текстовых сообщений
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
        logger.warning("Админ-команды отключены (ADMIN_USER_ID не задан)")
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
