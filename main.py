# main.py
import os
from aiogram import Bot, Dispatcher
from aiogram.types import ContentType, Update
from supabase import create_client, Client
import httpx
from fastapi import FastAPI, Request, Response

# Переменные окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")      # ← API-ключ Yandex Cloud
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")  # ← ID каталога Yandex Cloud
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

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

# === TTS: текст → голос (Yandex SpeechKit) ===
async def text_to_speech_ogg(text: str, output_path: str) -> str | None:
    try:
        # Формируем SSML-разметку
        ssml_content = f'<speak><lang xml:lang="es-ES">{text}</lang></speak>'

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize",
                headers={"Authorization": f"Api-Key {YANDEX_API_KEY}"},
                data={
                    "ssml": ssml_content,       # ← ИСПОЛЬЗУЕМ ssml, НЕ text
                    "folderId": YANDEX_FOLDER_ID,
                    "voice": "madirus",
                    "format": "oggopus"
                }
            )
        if response.status_code != 200:
            print(f"Yandex TTS error: {response.text}")
            return None

        with open(output_path, "wb") as f:
            f.write(response.content)
        return output_path

    except Exception as e:
        print(f"TTS error: {e}")
        return None

# === STT: голос → текст (Yandex SpeechKit) ===
async def transcribe_with_yandex(ogg_path: str) -> str:
    try:
        with open(ogg_path, "rb") as f:
            audio_data = f.read()

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize",
                headers={"Authorization": f"Api-Key {YANDEX_API_KEY}"},
                params={
                    "folderId": YANDEX_FOLDER_ID,
                    "lang": "es-ES"  # Распознавание на испанском
                },
                content=audio_data
            )
        if response.status_code == 200:
            return response.json().get("result", "").strip()
        else:
            print(f"Yandex STT error: {response.text}")
            return ""
    except Exception as e:
        print(f"STT exception: {e}")
        return ""

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
                    "modelUri": f"gpt://b1gtelqnpt0qebniscns/yandexgpt/latest",
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
            return answer

        except Exception as e:
            print(f"YandexGPT exception: {e}")
            return "Lo siento, algo salió mal."

# === Отправка ответа ===
async def send_response_with_voice(message, response_text: str):
    voice_path = f"/tmp/resp_{message.message_id}.ogg"
    voice_file = await text_to_speech_ogg(response_text, voice_path)
    if voice_file and os.path.exists(voice_file):
        with open(voice_file, "rb") as f:
            await message.reply_voice(f)
        os.remove(voice_file)
    else:
        await message.reply(response_text)

# === Обработчики ===
@dp.message_handler(content_types=ContentType.VOICE)
async def handle_voice(message):
    try:
        voice = await message.voice.get_file()
        file_path = f"/tmp/voice_{message.from_user.id}.ogg"
        await bot.download_file(voice.file_path, file_path)

        user_text = await transcribe_with_yandex(file_path)
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