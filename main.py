# main.py
import os
import asyncio
from aiogram import Bot, Dispatcher
from aiogram.types import ContentType, Update
from supabase import create_client, Client
import httpx
from fastapi import FastAPI, Request, Response
from dotenv import load_dotenv

load_dotenv("env.txt")

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Инициализация
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()

# Вебхук — используем фиксированный путь, а токен проверяем вручную
WEBHOOK_PATH = "/webhook"  # ← Без токена в URL!

SYSTEM_PROMPT = (
    "Eres un profesor amable y paciente de español como lengua extranjera. "
    "Corrige errores gramaticales, de vocabulario o pronunciación de forma clara y sencilla. "
    "Explica brevemente por qué algo está mal y da un ejemplo correcto. "
    "Haz preguntas para mantener la conversación. "
    "Responde SIEMPRE en español, incluso si el usuario escribe en otro idioma. "
    "Adapta tu lenguaje al nivel principiante."
)

async def transcribe_with_deepgram(ogg_path: str) -> str:
    async with httpx.AsyncClient() as client:
        with open(ogg_path, "rb") as f:
            resp = await client.post(
                "https://api.deepgram.com/v1/listen?model=nova-2&language=es&smart_format=true",
                headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "audio/ogg"},
                content=f.read()
            )
    return resp.json()["results"]["channels"][0]["alternatives"][0]["transcript"].strip() if resp.status_code == 200 else ""

async def get_chat_history(user_id: int):
    try:
        r = supabase.table("messages").select("*").eq("user_id", user_id).order("created_at", desc=False).limit(6).execute()
        return [{"role": m["role"], "content": m["content"]} for m in r.data]
    except: return []

async def save_message(user_id: int, role: str, content: str):
    try:
        supabase.table("messages").insert({"user_id": user_id, "role": role, "content": content}).execute()
    except: pass

async def get_llm_response(user_id: int, text: str) -> str:
    history = await get_chat_history(user_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [{"role": "user", "content": text}]
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "HTTP-Referer": "https://botesp-1.onrender.com", "X-Title": "Spanish Bot"},
            json={"model": "mistralai/mistral-7b-instruct:free", "messages": messages}
        )
    try:
        ans = r.json()["choices"][0]["message"]["content"].strip()
    except:
        ans = "Lo siento, tuve un problema técnico."
    await save_message(user_id, "user", text)
    await save_message(user_id, "assistant", ans)
    return ans

@dp.message_handler(content_types=ContentType.VOICE)
async def handle_voice(message):
    voice = await message.voice.get_file()
    path = f"/tmp/voice_{message.from_user.id}.ogg"
    await bot.download_file(voice.file_path, path)
    text = await transcribe_with_deepgram(path)
    if not text:
        await message.reply("No entendí tu mensaje.")
        return
    resp = await get_llm_response(message.from_user.id, text)
    await message.reply(resp)

@dp.message_handler(content_types=ContentType.TEXT)
async def handle_text(message):
    resp = await get_llm_response(message.from_user.id, message.text)
    await message.reply(resp)

@app.on_event("startup")
async def on_startup():
    # Устанавливаем вебхук на простой URL
    webhook_url = f"botesp-1.onrender.com/webhook"
    await bot.set_webhook(webhook_url)
    print(f"✅ Webhook установлен: {webhook_url}")

@app.post("/webhook")
async def webhook(request: Request):
    # Получаем данные
    data = await request.json()
    update = Update(**data)
    # Обрабатываем
    await dp.process_update(update)
    return Response(status_code=200)  # Telegram требует 200 OK

@app.get("/")
async def health():
    return {"status": "ok"}