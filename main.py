import os
import random
import time
import uuid
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# НАСТРОЙКИ - ОБЯЗАТЕЛЬНО измените!
ALLOWED_ORIGIN = "https://ran1ten.github.io/ranCheck"  # <-- ССЫЛКА НА ВАШ САЙТ (без слеша в конце)
BOT_TOKEN = os.getenv("8785747497:AAGcet1Lg_ReoRhoTdBxg_3nk3BeZHshdb8")  # Будет добавлено на Render

# Локальное хранилище кодов (для демонстрации)
codes_store = {}

# НАСТРОЙКА CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN, "https://render.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class CodeRequest(BaseModel):
    code: str

class CodeResponse(BaseModel):
    success: bool
    token: str = None
    error: str = None

@app.get("/")
async def root():
    return {"status": "ok", "message": "Server is running"}

@app.post("/verify", response_model=CodeResponse)
async def verify_code(request: CodeRequest):
    code = request.code.strip()
    expire_time = codes_store.get(code)
    if not expire_time:
        raise HTTPException(status_code=400, detail="Неверный или просроченный код")
    if time.time() > expire_time:
        codes_store.pop(code, None)
        raise HTTPException(status_code=400, detail="Код истек")

    codes_store.pop(code, None)
    access_token = str(uuid.uuid4())
    return {"success": True, "token": access_token}

# Функции для бота будут добавлены после настройки бота
# ...
