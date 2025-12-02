# main.py
import os
from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ContentType
from supabase import create_client, Client
import httpx
from dotenv import load_dotenv

# Загружаем env.txt (не .env!)
load_dotenv("env.txt")

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

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
        with open(ogg_path, "rb") as audio_file:
            response = await client.post(
                "https://api.deepgram.com/v1/listen?model=nova-2&language=es&smart_format=true",
                headers={
                    "Authorization": f"Token {DEEPGRAM_API_KEY}",
                    "Content-Type": "audio/ogg"
                },
                content=audio_file.read()
            )
    if response.status_code == 200:
        data = response.json()
        return data["results"]["channels"][0]["alternatives"][0]["transcript"].strip()
    return ""

async def get_chat_history(user_id: int):
    try:
        response = supabase.table("messages").select("*") \
            .eq("user_id", user_id) \
            .order("created_at", desc=False) \
            .limit(6) \
            .execute()
        return [{"role": r["role"], "content": r["content"]} for r in response.data]
    except Exception as e:
        print("Ошибка истории:", e)
        return []

async def save_message(user_id: int, role: str, content: str):
    try:
        supabase.table("messages").insert({
            "user_id": user_id,
            "role": role,
            "content": content
        }).execute()
    except Exception as e:
        print("Ошибка сохранения:", e)

async def get_llm_response(user_id: int, user_text: str) -> str:
    history = await get_chat_history(user_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://your-bot-site.com",
                "X-Title": "Spanish Tutor Bot"
            },
            json={
                "model": "mistralai/mistral-7b-instruct:free",
                "messages": messages,
                "temperature": 0.7
            }
        )
    try:
        answer = response.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return "Lo siento, tuve un problema técnico. ¿Podrías repetirlo?"

    await save_message(user_id, "user", user_text)
    await save_message(user_id, "assistant", answer)
    return answer

@dp.message_handler(content_types=ContentType.VOICE)
async def handle_voice(message: types.Message):
    voice = await message.voice.get_file()
    file_path = f"/tmp/voice_{message.from_user.id}.ogg"
    await bot.download_file(voice.file_path, file_path)

    try:
        user_text = await transcribe_with_deepgram(file_path)
        if not user_text:
            await message.reply("No entendí tu mensaje. ¿Puedes repetirlo?")
            return
        response_text = await get_llm_response(message.from_user.id, user_text)
        await message.reply(response_text)
    except Exception as e:
        await message.reply("Hubo un error al procesar tu voz.")
        print(f"Error: {e}")

@dp.message_handler(content_types=ContentType.TEXT)
async def handle_text(message: types.Message):
    response = await get_llm_response(message.from_user.id, message.text)
    await message.reply(response)

if __name__ == "__main__":
    print("🇪🇸 Spanish Tutor Bot (con voz) iniciado...")
    executor.start_polling(dp, skip_updates=True)