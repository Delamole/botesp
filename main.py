# main.py
import os
import subprocess
from aiogram import Bot, Dispatcher
from aiogram.types import (
    ContentType, Update, Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from supabase import create_client, Client
import httpx
from fastapi import FastAPI, Request, Response

# === Переменные окружения ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# === Инициализация ===
bot = Bot(token=BOT_TOKEN)
Bot.set_current(bot)
dp = Dispatcher(bot)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()

# === Системный промпт ===
SYSTEM_PROMPT = (
    "Eres un profesor amable y paciente de español como lengua extranjera. "
    "Corrige errores gramaticales, de vocabulario o pronunciación de forma clara y sencilla. "
    "Explica brevemente por qué algo está mal y da un ejemplo correcto. "
    "Haz preguntas para mantener la conversación. "
    "Responde SIEMPRE en español, incluso si el usuario escribe en otro idioma. "
    "Adapta tu lenguaje al nivel principiante."
)

# === Глобальное хранилище последних ответов ===
last_responses = {}

# === TTS через ElevenLabs ===
async def text_to_speech_ogg(text: str, output_path: str) -> str | None:
    """Генерирует .ogg через ElevenLabs + ffmpeg"""
    if not ELEVENLABS_API_KEY:
        print("⚠️ ELEVENLABS_API_KEY not set — skipping TTS")
        return None

    try:
        # 👇 ЛУЧШИЕ ГОЛОСА ДЛЯ ИСПАНСКОГО (см. раздел ниже)
        voice_id = "21m00Tcm4TlvDq8ikWAM"  # Rachel — универсальный, поддерживает испанский
        model_id = "eleven_multilingual_v2"  # Обязательно! Поддерживает испанский

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "Content-Type": "application/json"
                },
                json={
                    "text": text,
                    "model_id": model_id,
                    "voice_settings": {
                        "stability": 0.7,
                        "similarity_boost": 0.8
                    }
                }
            )

        if response.status_code != 200:
            print(f"❌ ElevenLabs error {response.status_code}: {response.text}")
            return None

        # Сохраняем .mp3 → конвертируем в .ogg
        mp3_path = output_path.replace(".ogg", ".mp3")
        with open(mp3_path, "wb") as f:
            f.write(response.content)

        subprocess.run(["ffmpeg", "-y", "-i", mp3_path, "-acodec", "libopus", output_path], check=True)
        os.remove(mp3_path)
        return output_path

    except Exception as e:
        print(f"TTS (ElevenLabs) error: {e}")
        return None

# === Работа с историей ===
async def get_chat_history(user_id: int):
    try:
        response = supabase.table("messages").select("*") \
            .eq("user_id", user_id) \
            .order("created_at", desc=False) \
            .limit(6) \
            .execute()
        return [{"role": r["role"], "content": r["content"]} for r in response.data]
    except Exception as e:
        print(f"History error: {e}")
        return []

async def save_message(user_id: int, role: str, content: str):
    try:
        supabase.table("messages").insert({
            "user_id": user_id,
            "role": role,
            "content": content
        }).execute()
    except Exception as e:
        print(f"Save error: {e}")

# === Ответ от YandexGPT ===
async def get_llm_response(user_id: int, user_text: str) -> str:
    history = await get_chat_history(user_id)
    messages = [{"role": "system", "text": SYSTEM_PROMPT}]
    for msg in history:
        role = "user" if msg["role"] == "user" else "assistant"
        messages.append({"role": role, "text": msg["content"]})
    messages.append({"role": "user", "text": user_text})

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                headers={
                    "Authorization": f"Api-Key {YANDEX_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest",
                    "completionOptions": {
                        "stream": False,
                        "temperature": 0.6,
                        "maxTokens": "2000"
                    },
                    "messages": messages
                }
            )
            if response.status_code != 200:
                print(f"YandexGPT error: {response.text}")
                return "Lo siento, tuve un problema técnico."
            data = response.json()
            answer = data["result"]["alternatives"][0]["message"]["text"].strip()
            await save_message(user_id, "user", user_text)
            await save_message(user_id, "assistant", answer)
            last_responses[user_id] = answer
            return answer
        except Exception as e:
            print(f"YandexGPT exception: {e}")
            return "Lo siento, algo salió mal."

# === Отправка голоса + кнопка "Texto" ===
async def send_response_with_voice(message: Message, response_text: str):
    voice_path = f"/tmp/resp_{message.message_id}.ogg"
    voice_file = await text_to_speech_ogg(response_text, voice_path)
    if voice_file and os.path.exists(voice_file):
        await message.reply_voice(open(voice_file, "rb"))
        os.remove(voice_file)
        # 👇 Кнопка с эмодзи
        keyboard = InlineKeyboardMarkup().add(
            InlineKeyboardButton("📝 Texto", callback_data=f"text_{message.from_user.id}")
        )
        await message.reply("¿Quieres ver la transcripción?", reply_markup=keyboard)
    else:
        await message.reply(response_text)

# === /start ===
@dp.message_handler(commands=["start"])
async def cmd_start(message: Message):
    user_name = message.from_user.first_name or "amigo"
    welcome = f"¡Hola, {user_name}! ¡Soy un Hablador, tu amigo de la práctica del español!"
    await message.answer(welcome)
    await message.answer("🎙️ ¿Listo para practicar? ¡Háblame en español!")
    await message.answer_sticker("CAACAgIAAxkBAAEUY9tpME6m_cOsHmDgPUSbPhr7nmbTHQACDooAAm0biEnNOhsS-bVUkTYE")

# === Обработчики сообщений ===
@dp.message_handler(content_types=ContentType.VOICE)
async def handle_voice(message: Message):
    await message.reply("🎙️ Por ahora solo acepto mensajes de texto. ¡Escríbeme en español!")

@dp.message_handler(content_types=ContentType.TEXT)
async def handle_text(message: Message):
    try:
        response_text = await get_llm_response(message.from_user.id, message.text)
        await send_response_with_voice(message, response_text)
    except Exception as e:
        print(f"Text handler error: {e}")
        await message.reply("Lo siento, algo salió mal.")

# === Кнопка "Texto" с эмодзи ===
@dp.callback_query_handler(lambda c: c.data.startswith('text_'))
async def process_text_callback(callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    await callback_query.answer()
    if user_id in last_responses:
        text = last_responses[user_id]
        formatted = f"🎙️ **Texto:**\n\n{text}\n\n😊 ¡Sigue practicando!"
        await callback_query.message.reply(formatted, parse_mode="Markdown")
    else:
        await callback_query.message.reply("Lo siento, no tengo el texto guardado.")

# === Webhooks ===
@app.on_event("startup")
async def on_startup():
    webhook_url = "https://botesp-1.onrender.com/webhook"
    await bot.set_webhook(webhook_url)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update(**data)
        await dp.process_update(update)
        return Response(status_code=200)
    except Exception as e:
        print(f"Webhook error: {e}")
        return Response(status_code=500)

@app.get("/")
async def health_check():
    return {"status": "ok"}