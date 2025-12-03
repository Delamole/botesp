# main.py
import os
import subprocess
from aiogram import Bot, Dispatcher
from aiogram.types import ContentType, Update
from supabase import create_client, Client
import httpx
from fastapi import FastAPI, Request, Response

# Переменные окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")  # ← ДОБАВЬТЕ ЭТО В RENDER

# Инициализация
bot = Bot(token=BOT_TOKEN)
Bot.set_current(bot)
dp = Dispatcher(bot)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()

# Системный промпт на испанском
SYSTEM_PROMPT = (
    "Eres un profesor amable y paciente de español como lengua extranjera. "
    "Corrige errores gramaticales, de vocabulario o pronunciación de forma clara y sencilla. "
    "Explica brevemente por qué algo está mal y da un ejemplo correcto. "
    "Haz preguntas para mantener la conversación. "
    "Responde SIEMPRE en español, incluso si el usuario escribe en otro idioma. "
    "Adapta tu lenguaje al nivel principiante."
)

# === TTS через ElevenLabs ===
async def text_to_speech_ogg(text: str, output_path: str) -> str | None:
    """
    Генерирует .ogg через ElevenLabs (нейросетевой TTS)
    """
    try:
        if not ELEVENLABS_API_KEY:
            print("⚠️ ELEVENLABS_API_KEY not set — skipping TTS")
            return None

        # Голос Rachel — поддерживает испанский в мультиязычной модели
        voice_id = "pNInz6obpgDQGcFXXJ0n"  # Rachel
        model_id = "eleven_multilingual_v2"

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

        # Сохраняем .mp3
        mp3_path = output_path.replace(".ogg", ".mp3")
        with open(mp3_path, "wb") as f:
            f.write(response.content)

        # Конвертируем в .ogg (требование Telegram Voice)
        subprocess.run([
            "ffmpeg", "-y", "-i", mp3_path, "-acodec", "libopus", output_path
        ], check=True, capture_output=True)

        os.remove(mp3_path)
        return output_path

    except Exception as e:
        print(f"TTS (ElevenLabs) error: {e}")
        return None

# === Распознавание речи → Deepgram ===
async def transcribe_with_deepgram(ogg_path: str) -> str:
    async with httpx.AsyncClient() as client:
        with open(ogg_path, "rb") as f:
            resp = await client.post(
                "https://api.deepgram.com/v1/listen?model=nova-2&language=es&smart_format=true",
                headers={
                    "Authorization": f"Token {DEEPGRAM_API_KEY}",
                    "Content-Type": "audio/ogg"
                },
                content=f.read()
            )
    if resp.status_code == 200:
        data = resp.json()
        return data["results"]["channels"][0]["alternatives"][0]["transcript"].strip()
    return ""

# === История и ИИ ===
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

async def get_llm_response(user_id: int, user_text: str) -> str:
    history = await get_chat_history(user_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "HTTP-Referer": "https://botesp-1.onrender.com",
                    "X-Title": "Spanish Tutor Bot"
                },
                json={
                    "model": "mistralai/mistral-7b-instruct:free",
                    "messages": messages,
                    "temperature": 0.7
                }
            )

            if response.status_code != 200:
                print(f"❌ OpenRouter HTTP {response.status_code}: {response.text}")
                return "Lo siento, no tengo créditos disponibles."

            data = response.json()
            if "choices" not in data or not data["choices"]:
                print(f"❌ OpenRouter invalid response: {data}")
                return "Lo siento, el modelo no responde."

            answer = data["choices"][0]["message"]["content"].strip()
            await save_message(user_id, "user", user_text)
            await save_message(user_id, "assistant", answer)
            return answer

        except Exception as e:
            print(f"❌ OpenRouter exception: {e}")
            return "Lo siento, tuve un problema técnico."

# === Отправка ответа с голосом ===
async def send_response_with_voice(message, response_text: str):
    try:
        voice_path = f"/tmp/resp_{message.message_id}.ogg"
        voice_file = await text_to_speech_ogg(response_text)
        if voice_file and os.path.exists(voice_file):
            with open(voice_file, "rb") as f:
                await message.reply_voice(f)
            os.remove(voice_file)
        else:
            await message.reply(response_text)
    except Exception as e:
        print(f"Send voice error: {e}")
        await message.reply(response_text)

# === Обработчики ===
@dp.message_handler(content_types=ContentType.VOICE)
async def handle_voice(message):
    try:
        voice = await message.voice.get_file()
        file_path = f"/tmp/voice_{message.from_user.id}.ogg"
        await bot.download_file(voice.file_path, file_path)

        user_text = await transcribe_with_deepgram(file_path)
        if not user_text:
            await message.reply("No entendí tu mensaje. ¿Puedes repetirlo?")
            return

        response_text = await get_llm_response(message.from_user.id, user_text)
        await send_response_with_voice(message, response_text)
    except Exception as e:
        print(f"Voice handler error: {e}")
        await message.reply("Hubo un error al procesar tu voz.")

@dp.message_handler(content_types=ContentType.TEXT)
async def handle_text(message):
    try:
        response_text = await get_llm_response(message.from_user.id, message.text)
        await send_response_with_voice(message, response_text)
    except Exception as e:
        print(f"Text handler error: {e}")
        await message.reply("Lo siento, algo salió mal.")

# === Webhooks ===
@app.on_event("startup")
async def on_startup():
    webhook_url = "https://botesp-1.onrender.com/webhook"
    await bot.set_webhook(webhook_url)
    print(f"✅ Webhook set: {webhook_url}")

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