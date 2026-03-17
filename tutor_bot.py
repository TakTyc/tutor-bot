import logging
import os
from io import BytesIO
from datetime import datetime, timedelta, date, timezone

from dotenv import load_dotenv
from telegram import (
Update,
InlineKeyboardButton,
InlineKeyboardMarkup,
)
from telegram.ext import (
ApplicationBuilder,
CommandHandler,
MessageHandler,
CallbackQueryHandler,
ContextTypes,
filters,
)

from openai import OpenAI

#------------------- Конфиг -------------------

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

    if not TELEGRAM_BOT_TOKEN
	raise RuntimeError(
	    "TELEGRAM_BOT_TOKEN не задан (в .env или переменных окружения)."
	)

    if not OPENAI_API_KEY:
	logging.warning("OPENAI_API_KEY не задан. ИИ-ответы работать не будут.")
		client = None
    else:
	client = OpenAI(api_key=OPENAI_API_KEY)

	твой ID — без лимитов и оплаты

	FREE_USER_IDS = {5418608670}

	Тестовый режим подписок (без реальной оплаты Stars)

	TEST_SUBSCRIPTION_MODE = True

	Хранилище состояния пользователей в памяти.

	В реальном боте лучше хранить в БД или файле.

user_state = {}

структура:

user_state[user_id] = {

"free_used_today": int,

"last_date": date,

"subscription_expires_at": datetime | None

}

Настройки подписок

SUB_PLANS = {
"day": {
"title": "Подписка на 1 день",
"stars": 10,
"delta": timedelta(days=1),
},
"month": {
"title": "Подписка на 1 месяц",
"stars": 100,
"delta": timedelta(days=30),
},
"year": {
"title": "Подписка на 1 год",
"stars": 1000,
"delta": timedelta(days=365),
},
}

MAX_FREE_PER_DAY = 5

Логирование

logging.basicConfig(
format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
level=logging.INFO,
)
logger = logging.getLogger(name)

#------------------- Вспомогательные для ИИ -------------------

async def ask_ai_text(prompt: str, system_prompt: str = None) -> str:
"""
Текстовый запрос к ИИ (репетитор).
"""
if not client:
return "Извини, ИИ-часть ещё не настроена (нет API ключа)."

if system_prompt is None:
system_prompt = (
"Ты доброжелательный и понятный репетитор для школьников. "
"Объясняй простым языком, по шагам, по-русски."
)

resp = client.chat.completions.create(
model="gpt-4.1-mini",
messages=[
{"role": "system", "content": system_prompt},
{"role": "user", "content": prompt},
],
temperature=0.4,
)
return resp.choices[0].message.content

async def transcribe_audio(file_bytes: bytes, file_name: str = "audio.ogg") -> str:
"""
Распознавание речи.
"""
	if not client:
		return "Извини, распознавание речи ещё не настроено (нет API ключа)."

	audio_file = BytesIO(file_bytes)
	audio_file.name = file_name

transcription = client.audio.transcriptions.create(
model="gpt-4o-mini-transcribe",
file=audio_file,
response_format="text",
)
return transcription

async def analyze_image_with_question(image_bytes: bytes, question: str) -> str:
"""
Анализ изображения + вопрос к нему (фото задачи).
"""
if not client:
return "Извини, анализ изображений ещё не настроен (нет API ключа)."

import base64

b64 = base64.b64encode(image_bytes).decode("utf-8")
img_data_url = f"data:image/jpeg;base64,{b64}"

resp = client.chat.completions.create(
model="gpt-4.1-mini",
messages=[
{
"role": "system",
"content": (
"Ты репетитор, который помогает по учебным заданиям. "
"Смотри на картинку (например, фото из тетради или задачи) "
"и помоги решить или объяснить."
),
},
{
"role": "user",
"content": [
{
"type": "input_text",
"text": question or "Посмотри на изображение и объясни, что на нём.",
},
{
"type": "input_image",
"image_url": {"url": img_data_url},
},
],
},
],
temperature=0.4,
)
return resp.choices[0].message.content

#------------------- Доступ / подписки -------------------

def get_user_state(user_id: int):"""
Получить и обновить (при смене дня) состояние пользователя.
"""
today = date.today()state = user_state.get(user_id)
if not state:
state = {
"free_used_today": 0,
"last_date": today,
"subscription_expires_at": None,
}
user_state[user_id] = state
return state

if state["last_date"] != today:
state["free_used_today"] = 0
state["last_date"] = today

return state

def has_active_subscription(state) -> bool:
exp = state.get("subscription_expires_at")
if not exp:
return False
now = datetime.now(timezone.utc)
return exp > now

def add_subscription(user_id: int, plan_key: str):
"""
Активировать/продлить подписку пользователю.
"""
state = get_user_state(user_id)
now = datetime.now(timezone.utc)
plan = SUB_PLANS[plan_key]

current_exp = state.get("subscription_expires_at")
if current_exp and current_exp > now:
new_exp = current_exp + plan["delta"]
else:
new_exp = now + plan["delta"]

state["subscription_expires_at"] = new_exp
logger.info (http://logger.info/)(
"Подписка %s активирована для %s до %s",
plan_key,
user_id,
new_exp.isoformat(),
)
return new_exp

async def send_paywall(update: Update, context: ContextTypes.DEFAULT_TYPE):
keyboard = InlineKeyboardMarkup(
[
[
InlineKeyboardButton("1 день — 10⭐️", callback_data="sub_day"),
],
[
InlineKeyboardButton("1 месяц — 100⭐️", callback_data="sub_month"),
],
[
InlineKeyboardButton("1 год — 1000⭐️", callback_data="sub_year"),
],
]
)

await update.message.reply_text(
"Ты использовал 5 бесплатных вопросов на сегодня.\n\n"
"Чтобы продолжить пользоваться ботом‑репетитором, "
"оформи одну из подписок через Telegram Stars:",
reply_markup=keyboard,
)

async def ensure_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
"""
Проверяет доступ к ответу (лимит + подписка).
True -> можно продолжать обработку.
False -> отправлен paywall.
"""
user = update.effective_user
user_id = user.id (http://user.id/)

if user_id in FREE_USER_IDS:
return True

state = get_user_state(user_id)

if has_active_subscription(state):
return True

if state["free_used_today"] < MAX_FREE_PER_DAY:
state["free_used_today"] += 1
remaining = MAX_FREE_PER_DAY - state["free_used_today"]
if remaining > 0:
await update.message.reply_text(
f"Вопрос принят! Сегодня осталось ещё {remaining} бесплатных."
)
else:
await update.message.reply_text(
"Вопрос принят! Это был твой 5‑й бесплатный вопрос на сегодня."
)
return True

await send_paywall(update, context)
return False

#------------------- Stars / подписки: заглушка -------------------

async def handle_subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
"""
Обработчик нажатий на кнопки подписки.
В TEST_SUBSCRIPTION_MODE сразу активирует подписку без реальной оплаты.
"""
query = update.callback_query
await query.answer()
data = query.data
user = query.from_user
user_id = user.id (http://user.id/)

if data not in ("sub_day", "sub_month", "sub_year"):
await query.edit_message_text("Неизвестный тип подписки.")
return

plan_key = data.split("_", 1)[1]
plan = SUB_PLANS[plan_key]

if TEST_SUBSCRIPTION_MODE:
new_exp = add_subscription(user_id, plan_key)
await query.edit_message_text(
f"{plan['title']} активирована (ТЕСТОВЫЙ РЕЖИМ без реальной оплаты).\n"
f"Подписка действует до: {new_exp.strftime('%Y-%m-%d %H:%M UTC')}."
)
return

await query.edit_message_text(
"Обработка реальной оплаты через Telegram Stars ещё не реализована. "
"Нужно дополнить этот блок по официальной документации Bot API."
)

#------------------- Хендлеры Telegram -------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
user = update.effective_user
text = (
f"Привет, {user.first_name or 'ученик'}!\n\n"
"Я бот‑репетитор с ИИ.\n\n"
"Я умею:\n"
"• отвечать на текстовые вопросы\n"
"• понимать голосовые сообщения\n"
"• смотреть на фото задач\n\n"
f"Каждый пользователь получает {MAX_FREE_PER_DAY} бесплатных вопросов в день.\n"
"После этого можно оформить подписку через Telegram Stars:\n"
"• 1 день — 10⭐️\n"
"• 1 месяц — 100⭐️\n"
"• 1 год — 1000⭐️\n\n"
"Просто задай вопрос текстом, голосом или пришли фото задания."
)
await update.message.reply_text(text)
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
text = (
"Как пользоваться ботом:\n"
"• Напиши учебный вопрос — я объясню.\n"
"• Отправь голосовое — я распознаю и отвечу.\n"
"• Отправь фото задачи — я посмотрю и помогу.\n\n"
f"У тебя есть {MAX_FREE_PER_DAY} бесплатных вопросов в день. "
"Дальше можно оформить подписку через Telegram Stars.\n"
"Ты всегда можешь вызвать это сообщение командой /help."
)
await update.message.reply_text(text)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not await ensure_access(update, context):
return

user_text = update.message.text

await update.message.chat.send_action("typing")
try:
answer = await ask_ai_text(user_text)
except Exception as e:
logger.exception("Ошибка при запросе к OpenAI (текст): %s", e)
answer = "Что-то пошло не так с ИИ. Попробуй ещё раз позже."

await update.message.reply_text(answer)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not await ensure_access(update, context):
return

voice = update.message.voice
if not voice:
return

await update.message.chat.send_action("typing")
try:
file = await context.bot.get_file(voice.file_id)
file_bytes = await file.download_as_bytearray()

recognized_text = await transcribe_audio(file_bytes, file_name="voice.ogg")
logger.info (http://logger.info/)("Распознанный текст из голосового: %s", recognized_text)

answer = await ask_ai_text(
f"Ученик сказал голосом: «{recognized_text}». Ответь ему как репетитор."
)

await update.message.reply_text(
f"Я понял из голосового:\n\n«{recognized_text}»\n\nМой ответ:\n{answer}"
)
except Exception as e:
logger.exception("Ошибка при обработке голосового: %s", e)
await update.message.reply_text(
"Не получилось обработать голосовое. Попробуй ещё раз или напиши текстом."
)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not await ensure_access(update, context):
return

photos = update.message.photo
if not photos:
return

await update.message.chat.send_action("typing")
photo = photos[-1]
try:
file = await context.bot.get_file(photo.file_id)
file_bytes = await file.download_as_bytearray()

caption = update.message.caption or ""
question = caption.strip() or "Помоги разобрать это задание по фото."

answer = await analyze_image_with_question(file_bytes, question)
await update.message.reply_text(answer)
except Exception as e:
logger.exception("Ошибка при обработке фото: %s", e)
await update.message.reply_text(
"Не удалось обработать фото. Попробуй ещё раз или добавь подпись с вопросом."
)

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(
"Извин
)

def main():
app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_command))

app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
app.add_handler(MessageHandler(filters.VOICE, handle_voice))
app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

app.add_handler(
CallbackQueryHandler(
handle_subscription_callback,
pattern="^sub_",
)
)

app.add_handler(MessageHandler(filters.COMMAND, unknown))

logger.info (http://logger.info/)("Бот запущен. Ожидаю сообщения...")
app.run_polling()

if name == "main":
main()