import asyncio
import logging
import os
import base64
from datetime import datetime, timedelta, date, timezone
from io import BytesIO
from collections import deque

import asyncpg
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
    SuccessfulPayment,
)

from openai import OpenAI

# ------------------- Конфиг -------------------

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан.")

if not OPENAI_API_KEY:
    logging.warning("OPENAI_API_KEY не задан. ИИ-ответы работать не будут.")
    client = None
else:
    client = OpenAI(api_key=OPENAI_API_KEY)

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан (PostgreSQL).")

FREE_USER_IDS = {5418608670}
TEST_SUBSCRIPTION_MODE = False
MAX_FREE_PER_DAY = 5
TASK_XP_REWARD = 5
TASK_BALANCE_REWARD = 5
MAX_HISTORY_PER_USER = 20

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

db_pool: asyncpg.pool.Pool | None = None

# ------------------- Вопросы по предметам -------------------

SUBJECT_TASKS = {
    "math": [
        {
            "q": "Математика: чему равно 7 × 8?",
            "options": ["48", "54", "56", "64"],
            "answer_index": 2,
        },
        {
            "q": "Математика: чему равно √81?",
            "options": ["7", "8", "9", "10"],
            "answer_index": 2,
        },
    ],
    "russian": [
        {
            "q": "Русский: где слово с НЕ пишется слитно?",
            "options": ["не рад", "неправда", "не был", "не готов"],
            "answer_index": 1,
        },
        {
            "q": "Русский: укажи слово с безударной гласной в корне:",
            "options": ["гора", "лес", "трава", "дуб"],
            "answer_index": 2,
        },
    ],
    "english": [
        {
            "q": "English: Choose the correct translation: «Я учусь в школе.»",
            "options": [
                "I studying at school.",
                "I study at school.",
                "I am study at school.",
                "I am studying at the school yesterday.",
            ],
            "answer_index": 1,
        },
        {
            "q": "English: «cat» — это…",
            "options": ["кошка", "собака", "птица", "рыба"],
            "answer_index": 0,
        },
    ],
    "physics": [
        {
            "q": "Физика: какая величина измеряется в Ньютонах (Н)?",
            "options": ["Масса", "Сила", "Скорость", "Время"],
            "answer_index": 1,
        },
        {
            "q": "Физика: чему примерно равно ускорение свободного падения g?",
            "options": ["1 м/с²", "3 м/с²", "9,8 м/с²", "100 м/с²"],
            "answer_index": 2,
        },
    ],
}

SUBJECT_NAMES = {
    "math": "📐 Математика",
    "russian": "📚 Русский",
    "english": "🇬🇧 Английский",
    "physics": "⚡️ Физика",
}

# ------------------- Подписки/режимы -------------------

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


def get_rank(xp: int) -> str:
    if xp < 50:
        return "Новичок"
    if xp < 200:
        return "Стажёр учёного"
    if xp < 500:
        return "Юный академик"
    return "Профессор"


def mode_label(mode: str) -> str:
    if mode == "detailed":
        return "📚 Подробно"
    if mode == "simple":
        return "🙂 Простым языком"
    return "⚡️ Коротко"


def build_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚡️ Коротко", callback_data="mode_short"),
                InlineKeyboardButton(text="📚 Подробно", callback_data="mode_detailed"),
            ],
            [
                InlineKeyboardButton(
                    text="🙂 Простым языком", callback_data="mode_simple"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Главное меню", callback_data="menu_home"
                )
            ],
        ]
    )


def build_subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="1 день ⭐️ 10", callback_data="sub_day"
                ),
                InlineKeyboardButton(
                    text="1 месяц ⭐️ 100", callback_data="sub_month"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="1 год ⭐️ 1000", callback_data="sub_year"
                ),
            ],
        ]
    )


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Профиль", callback_data="menu_profile"
                ),
                InlineKeyboardButton(
                    text="💰 Пополнить баланс", callback_data="menu_topup"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📆 Задания по предметам", callback_data="menu_tasks"
                ),
                InlineKeyboardButton(
                    text="🏆 Лидерборд", callback_data="menu_top"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎛 Режим объяснения", callback_data="menu_mode"
                ),
                InlineKeyboardButton(
                    text="📝 Экзамен", callback_data="menu_exam"
                ),
            ],
        ]
    )


def build_subjects_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=SUBJECT_NAMES["math"], callback_data="task_math"
            ),
            InlineKeyboardButton(
                text=SUBJECT_NAMES["russian"], callback_data="task_russian"
            ),
        ],
        [
            InlineKeyboardButton(
                text=SUBJECT_NAMES["english"], callback_data="task_english"
            ),
            InlineKeyboardButton(
                text=SUBJECT_NAMES["physics"], callback_data="task_physics"
            ),
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="menu_home"
            ),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_exam_subjects_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=SUBJECT_NAMES["math"], callback_data="exam_math"
            ),
            InlineKeyboardButton(
                text=SUBJECT_NAMES["russian"], callback_data="exam_russian"
            ),
        ],
        [
            InlineKeyboardButton(
                text=SUBJECT_NAMES["english"], callback_data="exam_english"
            ),
            InlineKeyboardButton(
                text=SUBJECT_NAMES["physics"], callback_data="exam_physics"
            ),
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="menu_home"
            ),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ------------------- Работа с БД -------------------

async def init_db_pool():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                display_name TEXT,
                xp INTEGER DEFAULT 0,
                balance INTEGER DEFAULT 0,
                last_test_date DATE,
                mode TEXT DEFAULT 'short',
                subscription_expires_at TIMESTAMPTZ,
                free_used_today INTEGER DEFAULT 0,
                last_date DATE
            );
        """)


async def db_get_user(user_id: int) -> dict | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
        return dict(row) if row else None


async def db_upsert_user(state: dict):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, display_name, xp, balance, last_test_date,
                               mode, subscription_expires_at, free_used_today, last_date)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (user_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                xp = EXCLUDED.xp,
                balance = EXCLUDED.balance,
                last_test_date = EXCLUDED.last_test_date,
                mode = EXCLUDED.mode,
                subscription_expires_at = EXCLUDED.subscription_expires_at,
                free_used_today = EXCLUDED.free_used_today,
                last_date = EXCLUDED.last_date;
        """,
            state["user_id"],
            state["display_name"],
            state["xp"],
            state["balance"],
            state["last_test_date"],
            state["mode"],
            state["subscription_expires_at"],
            state["free_used_today"],
            state["last_date"],
        )


async def db_get_leaderboard(limit: int = 10) -> list[dict]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, display_name, xp, balance FROM users ORDER BY xp DESC LIMIT $1",
            limit,
        )
        return [dict(r) for r in rows]


# ------------------- Состояние пользователя (БД + память) -------------------

user_history: dict[int, deque[str]] = {}
exam_state: dict[int, dict] = {}
saved_items: dict[int, list[tuple[int, str, str]]] = {}
last_answer: dict[int, tuple[str, str]] = {}


async def get_user_state(user_id: int, display_name: str | None = None) -> dict:
    today = date.today()

    row = await db_get_user(user_id)
    if not row:
        state = {
            "user_id": user_id,
            "display_name": display_name or f"user_{user_id}",
            "xp": 0,
            "balance": 0,
            "last_test_date": None,
            "mode": "short",
            "subscription_expires_at": None,
            "free_used_today": 0,
            "last_date": today,
        }
        await db_upsert_user(state)
    else:
        state = row
        if display_name and display_name != state.get("display_name"):
            state["display_name"] = display_name

        last_date = state.get("last_date")
        if not last_date or last_date != today:
            state["free_used_today"] = 0
            state["last_date"] = today
            await db_upsert_user(state)

    if user_id not in user_history:
        user_history[user_id] = deque(maxlen=MAX_HISTORY_PER_USER)

    if user_id not in saved_items:
        saved_items[user_id] = []

    return state


async def save_user_state(state: dict):
    await db_upsert_user(state)


def has_active_subscription(state: dict) -> bool:
    exp = state.get("subscription_expires_at")
    if not exp:
        return False
    now = datetime.now(timezone.utc)
    return exp > now


async def add_subscription(user_id: int, plan_key: str) -> datetime:
    state = await get_user_state(user_id)
    now = datetime.now(timezone.utc)
    plan = SUB_PLANS[plan_key]

    current_exp = state.get("subscription_expires_at")
    if current_exp and current_exp > now:
        new_exp = current_exp + plan["delta"]
    else:
        new_exp = now + plan["delta"]

    state["subscription_expires_at"] = new_exp
    await save_user_state(state)
    return new_exp


async def send_paywall(message: Message) -> None:
    keyboard = build_subscription_keyboard()
    await message.answer(
        "Ты использовал 5 бесплатных вопросов на сегодня. ⏳\n\n"
        "Чтобы продолжить пользоваться ботом‑репетитором, "
        "оформи одну из подписок:",
        reply_markup=keyboard,
    )


async def ensure_access(message: Message) -> bool:
    user = message.from_user
    if not user:
        return False
    display_name = user.full_name or user.username or f"user_{user.id}"
    state = await get_user_state(user.id, display_name=display_name)

    if user.id in FREE_USER_IDS:
        return True

    if has_active_subscription(state):
        return True

    if state["free_used_today"] < MAX_FREE_PER_DAY:
        state["free_used_today"] += 1
        await save_user_state(state)
        remaining = MAX_FREE_PER_DAY - state["free_used_today"]
        if remaining > 0:
            await message.answer(
                f"Вопрос принят! ✅ Сегодня осталось ещё {remaining} бесплатных. 🎁"
            )
        else:
            await message.answer(
                "Вопрос принят! ✅ Это был твой 5‑й бесплатный вопрос на сегодня. ⭐️"
            )
        return True

    await send_paywall(message)
    return False


def format_profile(state: dict) -> str:
    today_free_left = max(0, MAX_FREE_PER_DAY - state["free_used_today"])
    sub_active = has_active_subscription(state)
    sub_text = "активна ✅" if sub_active else "нет ❌"
    rank = get_rank(state["xp"])
    test_today = (
        "✅ получена" if state.get("last_test_date") == date.today() else "❌ ещё нет"
    )
    return (
        f"📋 <b>Профиль</b>\n\n"
        f"Имя: {state['display_name']}\n"
        f"Ранг: <b>{rank}</b>\n"
        f"Баланс: <b>{state['balance']}</b> 💰\n"
        f"Опыт: <b>{state['xp']}</b> ⭐️\n"
        f"Бесплатных вопросов сегодня осталось: <b>{today_free_left}</b> 🎁\n"
        f"Подписка: {sub_text}\n"
        f"Награда за тест сегодня: {test_today}\n"
        f"Режим объяснения: {mode_label(state.get('mode', 'short'))}\n"
    )


async def format_leaderboard() -> str:
    rows = await db_get_leaderboard()
    if not rows:
        return "🏆 Пока нет данных для лидерборда."

    lines = ["🏆 <b>Лидерборд по опыту</b>\n"]
    for idx, row in enumerate(rows, start=1):
        name = row.get("display_name") or f"user_{row['user_id']}"
        xp = row.get("xp", 0)
        balance = row.get("balance", 0)
        rank = get_rank(xp)
        lines.append(f"{idx}) {name} — {xp} XP ({rank}), баланс {balance} 💰")
    return "\n".join(lines)


# ------------------- ИИ с режимами -------------------

def build_prompt_for_mode(state: dict, user_text: str) -> list[dict]:
    mode = state.get("mode", "short")
    if mode == "detailed":
        system = (
            "Ты доброжелательный и понятный репетитор. "
            "Отвечай очень подробно, по шагам, с примерами, но по‑русски и без лишней воды."
        )
    elif mode == "simple":
        system = (
            "Ты репетитор, который объясняет максимально простым языком, "
            "как другу, без сложных терминов. По‑русски."
        )
    else:
        system = (
            "Ты репетитор, отвечающий кратко и по делу. "
            "Дай 3–5 предложений, по‑русски."
        )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text},
    ]


async def ask_ai_text_with_mode(state: dict, prompt: str) -> str:
    if not client:
        return "Извини, ИИ‑часть ещё не настроена (нет API ключа)."

    messages = build_prompt_for_mode(state, prompt)
    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=messages,
        temperature=0.4,
    )
    return resp.choices[0].message.content


# ------------------- Дополнительные функции для голоса и фото -------------------

async def transcribe_audio(file_bytes: bytes, file_name: str = "voice.ogg") -> str:
    """Распознавание голосового сообщения через Whisper."""
    if not client:
        return "Голосовое распознавание недоступно (нет API ключа)."
    try:
        audio_file = BytesIO(file_bytes)
        audio_file.name = file_name
        transcription = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="ru"
        )
        return transcription.text
    except Exception as e:
        logger.exception("Ошибка распознавания голоса: %s", e)
        return "Не удалось распознать голосовое сообщение."


async def analyze_image_with_question(photo_bytes: bytes, question: str) -> str:
    """Анализ изображения через GPT-4o-mini с vision."""
    if not client:
        return "Анализ изображений недоступен (нет API ключа)."
    try:
        base64_image = base64.b64encode(photo_bytes).decode('utf-8')
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Ты репетитор. Помоги решить задание по этому изображению. Вопрос: {question}"
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=500
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.exception("Ошибка анализа изображения: %s", e)
        return "Не удалось проанализировать изображение."


# ------------------- Stars: подписка + пополнение -------------------

async def send_subscription_invoice(message: Message, plan_key: str):
    plan = SUB_PLANS[plan_key]
    prices = [LabeledPrice(label="XTR", amount=plan["stars"])]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"Оплатить {plan['stars']} ⭐️", pay=True)]]
    )
    await message.answer_invoice(
        title=plan["title"],
        description=f"Оформление подписки: {plan['title'].lower()}",
        prices=prices,
        provider_token="",
        payload=f"subscription_{plan_key}",
        currency="XTR",
        reply_markup=kb,
    )


async def send_topup_invoice(message: Message, amount: int):
    prices = [LabeledPrice(label="XTR", amount=amount)]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"Оплатить {amount} ⭐️", pay=True)]]
    )
    await message.answer_invoice(
        title="Пополнение баланса",
        description=f"Пополнение баланса на {amount} ⭐️",
        prices=prices,
        provider_token="",
        payload=f"topup_{amount}",
        currency="XTR",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("sub_"))
async def handle_subscription_callback(query: CallbackQuery) -> None:
    await query.answer()
    data = query.data
    user = query.from_user
    if not user or not data:
        return

    if data not in ("sub_day", "sub_month", "sub_year"):
        await query.message.edit_text("Неизвестный тип подписки. ❌")
        return

    plan_key = data.split("_", 1)[1]
    if TEST_SUBSCRIPTION_MODE:
        new_exp = await add_subscription(user.id, plan_key)
        await query.message.edit_text(
            f"{SUB_PLANS[plan_key]['title']} активирована ✅ (ТЕСТОВЫЙ РЕЖИМ).\n"
            f"Подписка действует до: {new_exp.strftime('%Y-%m-%d %H:%M UTC')} 🕒"
        )
    else:
        await send_subscription_invoice(query.message, plan_key)


@dp.callback_query(F.data.startswith("topup_"))
async def handle_topup_callback(query: CallbackQuery) -> None:
    await query.answer()
    data = query.data
    user = query.from_user
    if not user:
        return

    if data == "topup_custom":
        state = await get_user_state(user.id)
        state["mode"] = "topup_input"
        await save_user_state(state)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_home")]
            ]
        )
        await query.message.edit_text(
            "💰 Введи сумму пополнения в звёздах (целое число), например: 150",
            reply_markup=kb,
        )
        return

    try:
        amount = int(data.split("_")[1])
    except (IndexError, ValueError):
        await query.message.edit_text("Ошибка. Попробуй ещё раз.")
        return

    if TEST_SUBSCRIPTION_MODE:
        state = await get_user_state(user.id)
        state["balance"] += amount
        await save_user_state(state)
        await query.message.edit_text(
            f"Баланс пополнен на {amount} (ТЕСТОВЫЙ РЕЖИМ).\n"
            f"Текущий баланс: {state['balance']} 💰"
        )
    else:
        await send_topup_invoice(query.message, amount)


@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery) -> None:
    logger.info(f"Pre-checkout: {pre_checkout_query.invoice_payload}")
    await pre_checkout_query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message) -> None:
    user = message.from_user
    if not user:
        return

    payment: SuccessfulPayment = message.successful_payment
    payload = payment.invoice_payload

    logger.info(f"Successful payment from {user.id}, payload={payload}")

    if payload.startswith("subscription_"):
        plan_key = payload.replace("subscription_", "")
        if plan_key in SUB_PLANS:
            new_exp = await add_subscription(user.id, plan_key)
            await message.answer(
                f"✅ Оплата прошла успешно!\n"
                f"{SUB_PLANS[plan_key]['title']} активирована.\n"
                f"Подписка действует до: {new_exp.strftime('%Y-%m-%d %H:%M UTC')} 🕒\n\n"
                "Спасибо за поддержку! 🙏"
            )
        else:
            await message.answer(
                "✅ Оплата прошла успешно, но возникла ошибка активации. Обратитесь в поддержку."
            )
    elif payload.startswith("topup_"):
        try:
            amount = int(payload.replace("topup_", ""))
        except ValueError:
            amount = 0
        if amount > 0:
            state = await get_user_state(user.id)
            state["balance"] += amount
            await save_user_state(state)
            await message.answer(
                f"✅ Баланс пополнен на {amount} ⭐️!\n"
                f"Текущий баланс: {state['balance']} 💰"
            )
        else:
            await message.answer("✅ Оплата прошла, но возникла ошибка начисления.")
    else:
        await message.answer("✅ Оплата прошла успешно! Спасибо!")


@dp.message(Command("paysupport"))
async def pay_support_handler(message: Message) -> None:
    await message.answer(
        "🛟 Поддержка платежей\n\n"
        "Если у тебя возникли вопросы с оплатой или нужно вернуть средства:\n"
        "• Напиши @your_support_username\n\n"
        "Возврат возможен в течение 7 дней после покупки при наличии технических проблем."
    )


# ------------------- Задания по предметам -------------------

@dp.callback_query(F.data == "menu_tasks")
async def menu_tasks(query: CallbackQuery) -> None:
    await query.answer()
    await query.message.edit_text(
        "📆 Выбери предмет для задания:",
        reply_markup=build_subjects_keyboard(),
    )


@dp.callback_query(F.data.startswith("task_"))
async def handle_subject_task(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    if not user:
        return
    display_name = user.full_name or user.username or f"user_{user.id}"
    state = await get_user_state(user.id, display_name=display_name)

    subject_key = query.data.replace("task_", "")
    tasks = SUBJECT_TASKS.get(subject_key)
    if not tasks:
        await query.message.edit_text(
            "Для этого предмета пока нет заданий. 😕",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    import random
    idx = random.randint(0, len(tasks) - 1)
    task = tasks[idx]

    state["quiz_subject"] = subject_key
    state["quiz_question_index"] = idx
    await save_user_state(state)

    options = task["options"]
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{i+1}. {opt}",
                callback_data=f"quiz_{subject_key}_{idx}_{i}",
            )
        ]
        for i, opt in enumerate(options)
    ]
    buttons.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_tasks")]
    )

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await query.message.edit_text(
        f"{SUBJECT_NAMES[subject_key]}\n\n{task['q']}",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("quiz_"))
async def handle_quiz_answer(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    if not user:
        return
    display_name = user.full_name or user.username or f"user_{user.id}"
    state = await get_user_state(user.id, display_name=display_name)

    data = query.data
    try:
        _, subject_key, q_idx_str, ans_idx_str = data.split("_", 3)
        q_idx = int(q_idx_str)
        ans_idx = int(ans_idx_str)
    except Exception:
        await query.message.edit_text(
            "Что-то пошло не так с разбором ответа. 😔",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    tasks = SUBJECT_TASKS.get(subject_key)
    if not tasks or not (0 <= q_idx < len(tasks)):
        await query.message.edit_text(
            "Вопрос не найден. Попробуй ещё раз. 🙂",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    task = tasks[q_idx]
    correct_idx = task["answer_index"]
    today = date.today()

    if ans_idx == correct_idx:
        if state.get("last_test_date") != today:
            state["last_test_date"] = today
            state["xp"] += TASK_XP_REWARD
            state["balance"] += TASK_BALANCE_REWARD
            text = (
                "✅ Верно! Ты получаешь награду за тест!\n\n"
                f"+{TASK_XP_REWARD} XP и +{TASK_BALANCE_REWARD} к балансу. 💰\n\n"
                f"Правильный ответ: {task['options'][correct_idx]}"
            )
        else:
            text = (
                "✅ Верно, но сегодня ты уже получал(а) награду за тест.\n"
                "Приходи завтра за новой!\n\n"
                f"Правильный ответ: {task['options'][correct_idx]}"
            )
    else:
        text = (
            "❌ Неверно.\n\n"
            f"Правильный ответ: {task['options'][correct_idx]}\n"
            "Попробуй ещё одно задание! 🙂"
        )

    state["quiz_subject"] = None
    state["quiz_question_index"] = None
    await save_user_state(state)

    await query.message.edit_text(
        text,
        reply_markup=build_main_menu_keyboard(),
    )


# ------------------- Экзамен (/exam) -------------------

@dp.message(Command("exam"))
async def cmd_exam(message: Message) -> None:
    await message.answer(
        "📝 Выбери предмет для мини‑экзамена (5 вопросов):",
        reply_markup=build_exam_subjects_keyboard(),
    )


EXAM_QUESTIONS_PER_SUBJECT = 5


@dp.callback_query(F.data.startswith("exam_"))
async def handle_exam_subject(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    if not user:
        return
    subject_key = query.data.replace("exam_", "")
    tasks = SUBJECT_TASKS.get(subject_key)
    if not tasks:
        await query.message.edit_text(
            "Для этого предмета пока нет заданий. 😕",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    import random
    total = min(EXAM_QUESTIONS_PER_SUBJECT, len(tasks))
    order = random.sample(range(len(tasks)), total)

    exam_state[user.id] = {
        "subject": subject_key,
        "order": order,
        "pos": 0,
        "correct": 0,
    }

    await send_exam_question(query.message, user.id)


async def send_exam_question(message: Message, user_id: int) -> None:
    state_exam = exam_state.get(user_id)
    if not state_exam:
        await message.edit_text(
            "Экзамен не найден. Начни заново командой /exam.",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    subject_key = state_exam["subject"]
    order = state_exam["order"]
    pos = state_exam["pos"]

    tasks = SUBJECT_TASKS.get(subject_key)
    if pos >= len(order):
        # экзамен закончен
        correct = state_exam["correct"]
        total = len(order)

        user_state_db = await get_user_state(user_id)
        # Можно наградить XP за экзамен:
        user_state_db["xp"] += correct * 2
        await save_user_state(user_state_db)

        if correct >= total - 1:
            text = (
                f"🎉 Экзамен по {SUBJECT_NAMES[subject_key]} завершён!\n\n"
                f"Ты ответил(а) правильно на {correct} из {total} вопросов.\n"
                "Отличный результат! 💪"
            )
        elif correct >= total // 2:
            text = (
                f"Экзамен по {SUBJECT_NAMES[subject_key]} завершён.\n\n"
                f"Правильных ответов: {correct} из {total}.\n"
                "Неплохо, но есть, что повторить. 🙂"
            )
        else:
            text = (
                f"Экзамен по {SUBJECT_NAMES[subject_key]} завершён.\n\n"
                f"Правильных ответов: {correct} из {total}.\n"
                "Ничего страшного, попробуй ещё раз после повторения темы. 🙂"
            )

        await message.edit_text(text, reply_markup=build_main_menu_keyboard())
        exam_state.pop(user_id, None)
        return

    idx = order[pos]
    task = SUBJECT_TASKS[subject_key][idx]
    options = task["options"]

    buttons = [
        [
            InlineKeyboardButton(
                text=f"{i+1}. {opt}",
                callback_data=f"examans_{subject_key}_{idx}_{i}",
            )
        ]
        for i, opt in enumerate(options)
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.edit_text(
        f"Экзамен по {SUBJECT_NAMES[subject_key]} ({pos+1}/{len(order)})\n\n{task['q']}",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("examans_"))
async def handle_exam_answer(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    if not user:
        return
    data = query.data
    try:
        _, subject_key, q_idx_str, ans_idx_str = data.split("_", 3)
        q_idx = int(q_idx_str)
        ans_idx = int(ans_idx_str)
    except Exception:
        await query.message.edit_text(
            "Ошибка разбора ответа. Попробуй снова /exam.",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    state_exam = exam_state.get(user.id)
    if not state_exam or state_exam["subject"] != subject_key:
        await query.message.edit_text(
            "Экзамен неактивен. Начни снова /exam.",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    tasks = SUBJECT_TASKS.get(subject_key)
    if not tasks or not (0 <= q_idx < len(tasks)):
        await query.message.edit_text(
            "Вопрос не найден. Попробуй ещё раз.",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    task = tasks[q_idx]
    correct_idx = task["answer_index"]

    if ans_idx == correct_idx:
        state_exam["correct"] += 1

    state_exam["pos"] += 1
    exam_state[user.id] = state_exam

    await send_exam_question(query.message, user.id)


# ------------------- /save, /list, /repeat -------------------

@dp.message(Command("save"))
async def cmd_save(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    last = last_answer.get(user.id)
    if not last:
        await message.answer("Пока нечего сохранять. Сначала задай вопрос и получи ответ. 🙂")
        return

    q, a = last
    items = saved_items.setdefault(user.id, [])
    new_id = len(items) + 1
    items.append((new_id, q, a))
    await message.answer(f"✅ Объяснение сохранено под номером {new_id}.")


@dp.message(Command("list"))
async def cmd_list(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    items = saved_items.get(user.id, [])
    if not items:
        await message.answer("У тебя пока нет сохранённых задач. Используй /save после ответа.")
        return

    text = "📚 Сохранённые задачи/объяснения:\n\n"
    for idx, (item_id, q, _) in enumerate(items, start=1):
        short_q = q
        if len(short_q) > 50:
            short_q = short_q[:47] + "..."
        text += f"{item_id}) {short_q}\n"
    text += "\nЧтобы повторить, напиши: /repeat НОМЕР (например, /repeat 1)."

    await message.answer(text)


@dp.message(Command("repeat"))
async def cmd_repeat(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Используй: /repeat НОМЕР (например, /repeat 1).")
        return
    num = int(parts[1])

    items = saved_items.get(user.id, [])
    for item_id, q, a in items:
        if item_id == num:
            await message.answer(f"❓ Вопрос:\n{q}\n\n💡 Объяснение:\n{a}")
            return

    await message.answer("Такого номера нет в сохранённых. Посмотри список через /list.")


# ------------------- /summary -------------------

@dp.message(Command("summary"))
async def cmd_summary(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    history = user_history.get(user.id)
    if not history:
        await message.answer("Пока нет вопросов для конспекта. Задай мне что‑нибудь. 🙂")
        return

    text = "📝 Краткий конспект твоих последних вопросов:\n\n"
    for i, q in enumerate(history, start=1):
        text += f"{i}) {q}\n"
    await message.answer(text)


# ------------------- Меню, профиль, режимы -------------------

@dp.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer("📱 Главное меню:", reply_markup=build_main_menu_keyboard())


@dp.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    display_name = user.full_name or user.username or f"user_{user.id}"
    state = await get_user_state(user.id, display_name=display_name)
    await message.answer(format_profile(state))


@dp.message(Command("top"))
async def cmd_top(message: Message) -> None:
    text = await format_leaderboard()
    await message.answer(text)


@dp.message(Command("mode"))
async def cmd_mode(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    await message.answer("Выбери режим объяснения:", reply_markup=build_mode_keyboard())


@dp.callback_query(F.data.startswith("mode_"))
async def handle_mode_callback(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    if not user:
        return
    display_name = user.full_name or user.username or f"user_{user.id}"
    state = await get_user_state(user.id, display_name=display_name)

    data = query.data
    if data == "mode_short":
        state["mode"] = "short"
    elif data == "mode_detailed":
        state["mode"] = "detailed"
    elif data == "mode_simple":
        state["mode"] = "simple"

    await save_user_state(state)

    await query.message.edit_text(
        f"Режим объяснения: {mode_label(state['mode'])}",
        reply_markup=build_main_menu_keyboard(),
    )


@dp.callback_query(F.data.startswith("menu_"))
async def handle_menu_callback(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    if not user:
        return
    display_name = user.full_name or user.username or f"user_{user.id}"
    state = await get_user_state(user.id, display_name=display_name)
    data = query.data

    if data == "menu_profile":
        await query.message.edit_text(
            format_profile(state),
            reply_markup=build_main_menu_keyboard(),
        )
    elif data == "menu_tasks":
        await query.message.edit_text(
            "📆 Выбери предмет для задания:",
            reply_markup=build_subjects_keyboard(),
        )
    elif data == "menu_top":
        text = await format_leaderboard()
        await query.message.edit_text(
            text,
            reply_markup=build_main_menu_keyboard(),
        )
    elif data == "menu_topup":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="50 ⭐️", callback_data="topup_50"
                    ),
                    InlineKeyboardButton(
                        text="100 ⭐️", callback_data="topup_100"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="200 ⭐️", callback_data="topup_200"
                    ),
                    InlineKeyboardButton(
                        text="500 ⭐️", callback_data="topup_500"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="💬 Другая сумма", callback_data="topup_custom"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад", callback_data="menu_home"
                    ),
                ],
            ]
        )
        await query.message.edit_text(
            "💰 Выберите сумму пополнения баланса:",
            reply_markup=kb,
        )
    elif data == "menu_mode":
        await query.message.edit_text(
            "Выбери режим объяснения:",
            reply_markup=build_mode_keyboard(),
        )
    elif data == "menu_exam":
        await query.message.edit_text(
            "📝 Выбери предмет для мини‑экзамена:",
            reply_markup=build_exam_subjects_keyboard(),
        )
    elif data == "menu_home":
        await query.message.edit_text(
            "📱 Главное меню:",
            reply_markup=build_main_menu_keyboard(),
        )


# ------------------- Q&A -------------------

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    display_name = user.full_name or user.username or f"user_{user.id}"
    await get_user_state(user.id, display_name=display_name)
    text = (
        f"Привет, {user.first_name or 'ученик'}! 👋\n\n"
        "Я бот‑репетитор с ИИ 🤖📚\n\n"
        "Я умею:\n"
        "• отвечать на текстовые вопросы 📝\n"
        "• понимать голосовые сообщения 🎤\n"
        "• смотреть на фото задач 📷\n\n"
        f"Каждый пользователь получает {MAX_FREE_PER_DAY} бесплатных вопросов в день 🎁\n"
        "После этого можно оформить подписку через Telegram Stars ⭐️.\n\n"
        "Команды:\n"
        "• /menu — главное меню 📱\n"
        "• /profile — профиль 📋\n"
        "• /top — лидерборд 🏆\n"
        "• /mode — режим объяснения 🎛\n"
        "• /summary — конспект 📝\n"
        "• /exam — мини‑экзамен 📝\n"
        "• /save, /list, /repeat — сохранить и повторять задачи 💾\n"
        "• /paysupport — поддержка платежей 🛟\n\n"
        "А ещё есть задания по предметам в /menu → «Задания по предметам» 📆\n"
        "Каждый день можно получить награду за тест! 🎁\n\n"
        "Просто задай вопрос текстом, голосом или пришли фото задания. 🙂"
    )
    await message.answer(text, reply_markup=build_main_menu_keyboard())


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "Как пользоваться ботом:\n"
        "• Напиши учебный вопрос — я объясню. 📝\n"
        "• Отправь голосовое — я распознаю и отвечу. 🎤\n"
        "• Отправь фото задачи — я посмотрю и помогу. 📷\n\n"
        f"У тебя есть {MAX_FREE_PER_DAY} бесплатных вопросов в день. "
        "Дальше можно оформить подписку через Telegram Stars. ⭐️\n\n"
        "Дополнительно:\n"
        "• /menu — главное меню 📱\n"
        "• /profile — профиль 📋\n"
        "• /top — лидерборд 🏆\n"
        "• /mode — режим объяснения 🎛\n"
        "• /summary — конспект 📝\n"
        "• /exam — мини‑экзамен 📝\n"
        "• /save, /list, /repeat — сохранить и повторять задачи 💾\n"
        "• /paysupport — поддержка платежей 🛟"
    )
    await message.answer(text)


@dp.message(F.text & ~F.via_bot & ~F.text.startswith("/"))
async def handle_text(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    display_name = user.full_name or user.username or f"user_{user.id}"
    state = await get_user_state(user.id, display_name=display_name)

    if state.get("mode") == "topup_input":
        txt = (message.text or "").strip()
        if not txt.isdigit():
            await message.answer("Пожалуйста, введи целое число. Например: 150 💰")
            return
        amount = int(txt)
        if amount <= 0:
            await message.answer("Сумма должна быть положительной. 🙂")
            return
        state["mode"] = "short"
        await save_user_state(state)
        if TEST_SUBSCRIPTION_MODE:
            state["balance"] += amount
            await save_user_state(state)
            await message.answer(
                f"Баланс пополнен на {amount} (ТЕСТОВЫЙ РЕЖИМ).\n"
                f"Текущий баланс: {state['balance']} 💰"
            )
        else:
            await send_topup_invoice(message, amount)
        return

    if not await ensure_access(message):
        return

    user_text = message.text or ""
    user_history[user.id].append(user_text)

    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        answer = await ask_ai_text_with_mode(state, user_text)
        last_answer[user.id] = (user_text, answer)
    except Exception as e:
        logger.exception("Ошибка при запросе к OpenAI (текст): %s", e)
        answer = "Что-то пошло не так с ИИ. Попробуй ещё раз позже. 😔"

    await message.answer(answer)


@dp.message(F.voice)
async def handle_voice(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    display_name = user.full_name or user.username or f"user_{user.id}"
    state = await get_user_state(user.id, display_name=display_name)

    if not await ensure_access(message):
        return

    voice = message.voice
    if not voice:
        return

    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        file = await bot.get_file(voice.file_id)
        file_data = await bot.download_file(file.file_path)
        file_bytes = file_data.read()

        recognized_text = await transcribe_audio(file_bytes, file_name="voice.ogg")
        logger.info("Распознанный текст из голосового: %s", recognized_text)
        user_history[user.id].append(f"[voice] {recognized_text}")

        answer = await ask_ai_text_with_mode(
            state,
            f"Ученик сказал голосом: «{recognized_text}». Ответь ему как репетитор.",
        )
        last_answer[user.id] = (recognized_text, answer)
        await message.answer(
            f"Я понял из голосового:\n\n«{recognized_text}»\n\nМой ответ:\n{answer}"
        )
    except Exception as e:
        logger.exception("Ошибка при обработке голосового: %s", e)
        await message.answer(
            "Не получилось обработать голосовое. Попробуй ещё раз или напиши текстом. 😔"
        )


@dp.message(F.photo)
async def handle_photo(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    display_name = user.full_name or user.username or f"user_{user.id}"
    state = await get_user_state(user.id, display_name=display_name)

    if not await ensure_access(message):
        return

    photos = message.photo
    if not photos:
        return

    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    photo = photos[-1]
    try:
        file = await bot.get_file(photo.file_id)
        file_data = await bot.download_file(file.file_path)
        file_bytes = file_data.read()

        caption = message.caption or ""
        question = caption.strip() or "Помоги разобрать это задание по фото."

        user_history[user.id].append(f"[photo] {question}")

        answer = await analyze_image_with_question(file_bytes, question)
        last_answer[user.id] = (question, answer)

        await message.answer(answer)
    except Exception as e:
        logger.exception("Ошибка при обработке фото: %s", e)
        await message.answer(
            "Не удалось обработать фото. Попробуй ещё раз или добавь подпись с вопросом. 😔"
        )


@dp.message()
async def fallback_unknown(message: Message) -> None:
    await message.answer(
        "Извини, я понимаю только команды /start, /help, /menu, /mode, /summary, /exam, /save, /list, /repeat, /paysupport, текст, голосовые и фото. 🙂"
    )


# ------------------- Точка входа -------------------

async def main():
    await init_db_pool()
    logger.info("Бот (aiogram 3 + Postgres) запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())