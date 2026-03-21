import asyncio
import logging
import os
from datetime import datetime, timedelta, date, timezone
from io import BytesIO
from collections import deque

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

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN не задан (в .env или переменных окружения)."
    )

if not OPENAI_API_KEY:
    logging.warning("OPENAI_API_KEY не задан. ИИ-ответы работать не будут.")
    client = None
else:
    client = OpenAI(api_key=OPENAI_API_KEY)

# твой ID — без ограничений
FREE_USER_IDS = {5418608670}

# Режим подписок:
# True — тестовый (без реальной оплаты, сразу активируется подписка)
# False — реальный (через Stars-инвойс)
TEST_SUBSCRIPTION_MODE = False

# Хранилище состояния пользователей в памяти.
user_state: dict[int, dict] = {}

# Настройки подписок
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

# Награда за задания (тесты по предметам)
TASK_XP_REWARD = 5
TASK_BALANCE_REWARD = 5

# История вопросов для /summary
MAX_HISTORY_PER_USER = 20

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(name)

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()

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
            "options": [
                "не рад",
                "неправда",
                "не был",
                "не готов",
            ],
            "answer_index": 1,
        },
        {
            "q": "Русский: укажи слово с безударной гласной в корне:",
            "options": [
                "гора",
                "лес",
                "трава",
                "дуб",
            ],
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

# ------------------- Ранги и режимы объяснения -------------------


def get_rank(xp: int) -> str:
    if xp < 50:
        return "Новичок"
    if xp < 200:
        return "Стажёр учёного"
    if xp < 500:
        return "Юный академик"
    return "Профессор"


def build_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚡️ Коротко", callback_data="mode_short"
                ),
                InlineKeyboardButton(
                    text="📚 Подробно", callback_data="mode_detailed"
                ),
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


# ------------------- Состояние пользователя -------------------


def get_user_state(user_id: int, display_name: str | None = None) -> dict:
    today = date.today()
    state = user_state.get(user_id)
    if not state:
        state = {
            "free_used_today": 0,
            "last_date": today,
            "subscription_expires_at": None,
            "balance": 0,
            "xp": 0,
            "display_name": display_name or f"user_{user_id}",
            "mode": "short",  # short / detailed / simple
            "quiz_subject": None,
            "quiz_question_index": None,
            "history": deque(maxlen=MAX_HISTORY_PER_USER),  # список строк вопросов
        }
        user_state[user_id] = state
        return state

    if state["last_date"] != today:
        state["free_used_today"] = 0
        state["last_date"] = today

    if display_name:
        state["display_name"] = display_name

    if "history" not in state:
        state["history"] = deque(maxlen=MAX_HISTORY_PER_USER)

    return state


def has_active_subscription(state: dict) -> bool:
    exp = state.get("subscription_expires_at")
    if not exp:
        return False
    now = datetime.now(timezone.utc)
    return exp > now


def add_subscription(user_id: int, plan_key: str) -> datetime:
    """Активировать/продлить подписку пользователю."""
    state = get_user_state(user_id)
    now = datetime.now(timezone.utc)
    plan = SUB_PLANS[plan_key]

    current_exp = state.get("subscription_expires_at")
    if current_exp and current_exp > now:
        new_exp = current_exp + plan["delta"]
    else:
        new_exp = now + plan["delta"]

    state["subscription_expires_at"] = new_exp
    logger.info(
        "Подписка %s активирована для %s до %s",
        plan_key,
        user_id,
        new_exp.isoformat(),
    )
    return new_exp


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


async def send_paywall(message: Message) -> None:
    keyboard = build_subscription_keyboard()
    await message.answer(
        "Ты использовал 5 бесплатных вопросов на сегодня. ⏳\n\n"
        "Чтобы продолжить пользоваться ботом‑репетитором, "
        "оформи одну из подписок:",
        reply_markup=keyboard,
    )


async def ensure_access(message: Message) -> bool:
    """
    Проверяет доступ к ответу (лимит + подписка).
    True -> можно продолжать обработку.
    False -> отправлен paywall.
    """
    user = message.from_user
    if not user:
        return False
    display_name = user.full_name or user.username or f"user_{user.id}"
    state = get_user_state(user.id, display_name=display_name)

    user_id = user.id

    # Белый список (ты)
    if user_id in FREE_USER_IDS:
        return True

    # Есть активная подписка
    if has_active_subscription(state):
        return True

    # Бесплатные вопросы
    if state["free_used_today"] < MAX_FREE_PER_DAY:
        state["free_used_today"] += 1
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

    # Лимит исчерпан — показываем paywall
    await send_paywall(message)
    return False


# ------------------- Профиль, ранги и лидерборд -------------------


def format_profile(state: dict) -> str:
    today_free_left = max(0, MAX_FREE_PER_DAY - state["free_used_today"])
    sub_active = has_active_subscription(state)
    sub_text = "активна ✅" if sub_active else "нет ❌"
    rank = get_rank(state["xp"])
    return (
        f"📋 <b>Профиль</b>\n\n"
        f"Имя: {state['display_name']}\n"
        f"Ранг: <b>{rank}</b>\n"
        f"Баланс: <b>{state['balance']}</b> 💰\n"
        f"Опыт: <b>{state['xp']}</b> ⭐️\n"
        f"Бесплатных вопросов сегодня осталось: <b>{today_free_left}</b> 🎁\n"
        f"Подписка: {sub_text}\n"
        f"Режим объяснения: {mode_label(state.get('mode', 'short'))}\n"
    )


def mode_label(mode: str) -> str:
    if mode == "detailed":
        return "📚 Подробно"
    if mode == "simple":
        return "🙂 Простым языком"
    return "⚡️ Коротко"
    def format_leaderboard() -> str:
    if not user_state:
        return "🏆 Пока нет данных для лидерборда."

    sorted_users = sorted(
        user_state.items(), key=lambda kv: kv[1].get("xp", 0), reverse=True
    )
    lines = ["🏆 <b>Лидерборд по опыту</b>\n"]
    for idx, (uid, state) in enumerate(sorted_users[:10], start=1):
        name = state.get("display_name", f"user_{uid}")
        xp = state.get("xp", 0)
        balance = state.get("balance", 0)
        rank = get_rank(xp)
        lines.append(f"{idx}) {name} — {xp} XP ({rank}), баланс {balance} 💰")
    return "\n".join(lines)


# =================== ОПЛАТА TELEGRAM STARS ===================


async def send_subscription_invoice(message: Message, plan_key: str):
    """
    Отправляет инвойс для оплаты подписки Stars.
    Для Stars:
    - provider_token = ""
    - currency = "XTR"
    - prices = [LabeledPrice(label="XTR", amount=цена)]
    """
    plan = SUB_PLANS[plan_key]
    prices = [LabeledPrice(label="XTR", amount=plan["stars"])]

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Оплатить {plan['stars']} ⭐️", pay=True
                )
            ]
        ]
    )

    await message.answer_invoice(
        title=plan["title"],
        description=f"Оформление подписки: {plan['title'].lower()}",
        prices=prices,
        provider_token="",  # пустая строка для Stars
        payload=f"subscription_{plan_key}",
        currency="XTR",
        reply_markup=keyboard,
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
        new_exp = add_subscription(user.id, plan_key)
        await query.message.edit_text(
            f"{SUB_PLANS[plan_key]['title']} активирована ✅ (ТЕСТОВЫЙ РЕЖИМ).\n"
            f"Подписка действует до: {new_exp.strftime('%Y-%m-%d %H:%M UTC')} 🕒"
        )
    else:
        await send_subscription_invoice(query.message, plan_key)


@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery) -> None:
    logger.info(f"Pre-checkout query: {pre_checkout_query.invoice_payload}")
    await pre_checkout_query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message) -> None:
    user = message.from_user
    if not user:
        return

    payment: SuccessfulPayment = message.successful_payment
    payload = payment.invoice_payload

    logger.info(f"Successful payment from {user.id}, payload: {payload}")

    if payload.startswith("subscription_"):
        plan_key = payload.replace("subscription_", "")
        if plan_key in SUB_PLANS:
            new_exp = add_subscription(user.id, plan_key)
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
    state = get_user_state(user.id, display_name=display_name)

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

    state["mode"] = state.get("mode", "short")  # не меняем
    state["quiz_subject"] = subject_key
    state["quiz_question_index"] = idx

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
    state = get_user_state(user.id, display_name=display_name)

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

    if ans_idx == correct_idx:
        state["xp"] += TASK_XP_REWARD
        state["balance"] += TASK_BALANCE_REWARD
        text = (
            "✅ Верно!\n\n"
            f"+{TASK_XP_REWARD} XP и +{TASK_BALANCE_REWARD} к балансу. 💰\n\n"
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

    await query.message.edit_text(
        text,
        reply_markup=build_main_menu_keyboard(),
    )


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
    state = get_user_state(user.id, display_name=display_name)
    await message.answer(format_profile(state))


@dp.message(Command("top"))
async def cmd_top(message: Message) -> None:
    await message.answer(format_leaderboard())
@dp.message(Command("mode"))
async def cmd_mode(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    await message.answer(
        "Выбери режим объяснения:", reply_markup=build_mode_keyboard()
    )


@dp.callback_query(F.data.startswith("mode_"))
async def handle_mode_callback(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    if not user:
        return
    display_name = user.full_name or user.username or f"user_{user.id}"
    state = get_user_state(user.id, display_name=display_name)

    data = query.data
    if data == "mode_short":
        state["mode"] = "short"
    elif data == "mode_detailed":
        state["mode"] = "detailed"
    elif data == "mode_simple":
        state["mode"] = "simple"

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
    state = get_user_state(user.id, display_name=display_name)
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
        await query.message.edit_text(
            format_leaderboard(),
            reply_markup=build_main_menu_keyboard(),
        )
    elif data == "menu_topup":
        state["mode"] = "topup"  # временно для ввода суммы
        await query.message.edit_text(
            "💰 Пополнение баланса (тестовый режим).\n\n"
            "Введи число, на сколько пополнить баланс.\n"
            "Например: <b>100</b>",
            reply_markup=build_main_menu_keyboard(),
        )
    elif data == "menu_mode":
        await query.message.edit_text(
            "Выбери режим объяснения:",
            reply_markup=build_mode_keyboard(),
        )
    elif data == "menu_home":
        await query.message.edit_text(
            "📱 Главное меню:",
            reply_markup=build_main_menu_keyboard(),
        )


# ------------------- Режим /summary -------------------


@dp.message(Command("summary"))
async def cmd_summary(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    state = get_user_state(user.id, display_name=(user.full_name or user.username))
    history = state.get("history")
    if not history or len(history) == 0:
        await message.answer("Пока нет вопросов для конспекта. Задай мне что‑нибудь. 🙂")
        return

    text = "📝 Краткий конспект твоих последних вопросов:\n\n"
    for i, q in enumerate(history, start=1):
        text += f"{i}) {q}\n"

    await message.answer(text)


# ------------------- Хендлеры Q&A -------------------


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    display_name = user.full_name or user.username or f"user_{user.id}"
    state = get_user_state(user.id, display_name=display_name)
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
        "• /summary — конспект последних вопросов 📝\n"
        "• /paysupport — поддержка платежей 🛟\n\n"
        "А ещё есть задания по предметам в /menu → «Задания по предметам» 📆\n\n"
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
        "• /paysupport — поддержка платежей 🛟"
    )
    await message.answer(text)


def build_prompt_for_mode(state: dict, user_text: str) -> list[dict]:
    """
    Собираем messages для OpenAI в зависимости от режима.
    """
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
    else:  # short
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


async def transcribe_audio(file_bytes: bytes, file_name: str = "voice.ogg") -> str:
    """Функция для распознавания голосового сообщения"""
    if not client:
        return "Голосовое распознавание временно недоступно."
    
    try:
        from io import BytesIO
        audio_file = BytesIO(file_bytes)
        audio_file.name = file_name
        
        transcription = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="ru"
        )
        return transcription.text
    except Exception as e:
        logger.exception("Ошибка при распознавании голоса: %s", e)
        return "Не удалось распознать голосовое сообщение."
        async def analyze_image_with_question(photo_bytes: bytes, question: str) -> str:
    """Функция для анализа изображения с вопросом"""
    if not client:
        return "Анализ изображений временно недоступен."
    
    try:
        import base64
        from io import BytesIO
        
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
        logger.exception("Ошибка при анализе изображения: %s", e)
        return "Не удалось проанализировать изображение."


@dp.message(F.text & ~F.via_bot & ~F.text.startswith("/"))
async def handle_text(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    display_name = user.full_name or user.username or f"user_{user.id}"
    state = get_user_state(user.id, display_name=display_name)

    # Режим пополнения баланса
    if state.get("mode") == "topup":
        txt = (message.text or "").strip()
        if not txt.isdigit():
            await message.answer("Пожалуйста, введи целое число. Например: 100 💰")
            return
        amount = int(txt)
        if amount <= 0:
            await message.answer("Сумма должна быть положительной. 🙂")
            return
        state["balance"] += amount
        state["mode"] = "short"
        await message.answer(
            f"Баланс пополнен на {amount} 💰\n"
            f"Текущий баланс: {state['balance']} 💰"
        )
        return

    # Обычный вопрос → проверяем доступ
    if not await ensure_access(message):
        return

    user_text = message.text or ""

    # Сохраняем вопрос в историю для /summary
    state["history"].append(user_text)

    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        answer = await ask_ai_text_with_mode(state, user_text)
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
    state = get_user_state(user.id, display_name=display_name)

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

        # Добавляем в историю распознанный текст
        state["history"].append(f"[voice] {recognized_text}")

        answer = await ask_ai_text_with_mode(
            state,
            f"Ученик сказал голосом: «{recognized_text}». Ответь ему как репетитор.",
        )

        await message.answer
        