import asyncio
import logging
import os
from datetime import datetime, timedelta, date, timezone
from io import BytesIO

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
    SuccessfulPayment
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

# Тестовый режим подписок (теперь с реальной оплатой)
TEST_SUBSCRIPTION_MODE = True  # Оставьте True для тестирования, False для продакшена

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

# Ежедневное задание
DAILY_QUEST_REQUIRED = 3
DAILY_QUEST_XP_REWARD = 10
DAILY_QUEST_BALANCE_REWARD = 10

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()


# ------------------- Вспомогательные для ИИ -------------------

async def ask_ai_text(prompt: str, system_prompt: str | None = None) -> str:
    """Текстовый запрос к ИИ (репетитор)."""
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
    """Распознавание речи."""
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
    """Анализ изображения + вопрос к нему (фото задачи)."""
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
            "last_daily_date": None,
            "today_questions": 0,
            "today_photos": 0,
            "today_voices": 0,
            "display_name": display_name or f"user_{user_id}",
            "mode": None,
        }
        user_state[user_id] = state
        return state

    # Обновление даты и сброс дневных счётчиков
    if state["last_date"] != today:
        state["free_used_today"] = 0
        state["last_date"] = today
        state["today_questions"] = 0
        state["today_photos"] = 0
        state["today_voices"] = 0

    if display_name:
        state["display_name"] = display_name

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
                    text="📆 Ежедневные задания", callback_data="menu_daily"
                ),
                InlineKeyboardButton(
                    text="🏆 Лидерборд", callback_data="menu_top"
                ),
            ],
        ]
    )


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


# ------------------- Ежедневные задания и XP -------------------

def increment_activity(state: dict, kind: str) -> None:
    state["today_questions"] += 1
    if kind == "photo":
        state["today_photos"] += 1
    elif kind == "voice":
        state["today_voices"] += 1


async def maybe_complete_daily(message: Message, state: dict) -> None:
    today = date.today()
    if state["last_daily_date"] == today:
        return  # уже получал награду сегодня

    if state["today_questions"] >= DAILY_QUEST_REQUIRED:
        state["last_daily_date"] = today
        state["xp"] += DAILY_QUEST_XP_REWARD
        state["balance"] += DAILY_QUEST_BALANCE_REWARD
        await message.answer(
            "🎉 Ежедневное задание выполнено!\n"
            f"Ты получил {DAILY_QUEST_XP_REWARD} опыта и "
            f"{DAILY_QUEST_BALANCE_REWARD} к балансу. 💰"
        )


def format_daily_status(state: dict) -> str:
    today_count = state["today_questions"]
    need = DAILY_QUEST_REQUIRED
    done = state["last_daily_date"] == date.today()
    status = "✅ Выполнено" if done else "⏳ В процессе"
    return (
        "📆 Ежедневное задание:\n"
        f"• Задать {need} любых вопроса боту (текст/голос/фото)\n"
        f"• Прогресс: {today_count}/{need}\n"
        f"• Статус: {status}\n\n"
        "Награда за выполнение: "
        f"{DAILY_QUEST_XP_REWARD} XP и {DAILY_QUEST_BALANCE_REWARD} к балансу. 💎"
    )


def format_profile(state: dict) -> str:
    today_free_left = max(0, MAX_FREE_PER_DAY - state["free_used_today"])
    sub_active = has_active_subscription(state)
    sub_text = "активна ✅" if sub_active else "нет ❌"
    return (
        f"📋 <b>Профиль</b>\n\n"
        f"Имя: {state['display_name']}\n"
        f"Баланс: <b>{state['balance']}</b> 💰\n"
        f"Опыт: <b>{state['xp']}</b> ⭐️\n"
        f"Бесплатных вопросов сегодня осталось: <b>{today_free_left}</b> 🎁\n"
        f"Подписка: {sub_text}\n"
    )


def format_leaderboard() -> str:
    if not user_state:
        return "🏆 Пока нет данных для лидерборда."

    # сортируем по XP
    sorted_users = sorted(
        user_state.items(), key=lambda kv: kv[1].get("xp", 0), reverse=True
    )
    lines = ["🏆 <b>Лидерборд по опыту</b>\n"]
    for idx, (uid, state) in enumerate(sorted_users[:10], start=1):
        name = state.get("display_name", f"user_{uid}")
        xp = state.get("xp", 0)
        balance = state.get("balance", 0)
        lines.append(f"{idx}) {name} — {xp} XP, баланс {balance} 💰")
    return "\n".join(lines)


# =================== ОПЛАТА TELEGRAM STARS ===================

async def send_subscription_invoice(message: Message, plan_key: str):
    """
    Отправляет инвойс для оплаты подписки Stars
    """
    plan = SUB_PLANS[plan_key]
    
    # Для Stars платежей:
    # - provider_token = "" (пустая строка)
    # - currency = "XTR"
    # - prices = [LabeledPrice(label="XTR", amount=цена)] [citation:8]
    prices = [LabeledPrice(label="XTR", amount=plan["stars"])]
    
    # Создаем клавиатуру с кнопкой оплаты
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Оплатить {plan['stars']} ⭐️", pay=False)]
        ]
    )
    
    await message.answer_invoice(
        title=plan["title"],
        description=f"Оформление подписки на {plan['title'].lower()}",
        prices=prices,
        provider_token="",  # Обязательно пустая строка для Stars [citation:8]
        payload=f"subscription_{plan_key}",  # Уникальный payload для идентификации
        currency="XTR",  # Обязательно XTR для Stars [citation:8]
        reply_markup=keyboard,
    )


@dp.callback_query(F.data.startswith("sub_"))
async def handle_subscription_callback(query: CallbackQuery) -> None:
    """
    Обработчик нажатий на кнопки подписки.
    Теперь с реальной оплатой через Stars.
    """
    await query.answer()
    data = query.data
    user = query.from_user
    if not user or not data:
        return

    if data not in ("sub_day", "sub_month", "sub_year"):
        await query.message.edit_text("Неизвестный тип подписки. ❌")
        return

    plan_key = data.split("_", 1)[1]  # day / month / year
    
    if TEST_SUBSCRIPTION_MODE:
        # Тестовый режим - без реальной оплаты
        new_exp = add_subscription(user.id, plan_key)
        await query.message.edit_text(
            f"{SUB_PLANS[plan_key]['title']} активирована ✅ (ТЕСТОВЫЙ РЕЖИМ).\n"
            f"Подписка действует до: {new_exp.strftime('%Y-%m-%d %H:%M UTC')} 🕒"
        )
    else:
        # Реальная оплата - отправляем инвойс
        await send_subscription_invoice(query.message, plan_key)


@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery) -> None:
    """
    Обязательный обработчик для подтверждения платежа.
    Должен ответить в течение 10 секунд. [citation:2][citation:8]
    """
    logger.info(f"Pre-checkout query received: {pre_checkout_query.invoice_payload}")
    # Всегда отвечаем ok=True для принятия платежа
    await pre_checkout_query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message) -> None:
    """
    Обработчик успешного платежа.
    Активирует подписку после оплаты.
    """
    user = message.from_user
    if not user:
        return
    
    payment: SuccessfulPayment = message.successful_payment
    payload = payment.invoice_payload
    
    logger.info(f"Successful payment from user {user.id}, payload: {payload}")
    
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
            await message.answer("✅ Оплата прошла успешно, но возникла ошибка активации. Обратитесь в поддержку.")
    else:
        await message.answer("✅ Оплата прошла успешно! Спасибо!")


@dp.message(Command("paysupport"))
async def pay_support_handler(message: Message) -> None:
    """
    Обязательная команда для модерации Telegram.
    Информация о возвратах и поддержке. [citation:8]
    """
    await message.answer(
        "🛟 Поддержка платежей\n\n"
        "По вопросам возврата средств и проблем с оплатой обращайтесь:\n"
        "• Напишите @your_support_username\n"
        "• Или используйте команду /support\n\n"
        "Возврат средств возможен в течение 7 дней после покупки при наличии технических проблем."
    )


# ------------------- Меню (профиль, пополнение, дейлики, топ) -------------------

@dp.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer("📱 Главное меню:", reply_markup=build_main_menu_keyboard())


@dp.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    user = message.from_user
    display_name = user.full_name or user.username or f"user_{user.id}"
    state = get_user_state(user.id, display_name=display_name)
    await message.answer(format_profile(state))


@dp.message(Command("daily"))
async def cmd_daily(message: Message) -> None:
    user = message.from_user
    display_name = user.full_name or user.username or f"user_{user.id}"
    state = get_user_state(user.id, display_name=display_name)
    await message.answer(format_daily_status(state))


@dp.message(Command("top"))
async def cmd_top(message: Message) -> None:
    await message.answer(format_leaderboard())


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
    elif data == "menu_daily":
        await query.message.edit_text(
            format_daily_status(state),
            reply_markup=build_main_menu_keyboard(),
        )
    elif data == "menu_top":
        await query.message.edit_text(
            format_leaderboard(),
            reply_markup=build_main_menu_keyboard(),
        )
    elif data == "menu_topup":
        state["mode"] = "topup"
        await query.message.edit_text(
            "💰 Пополнение баланса (тестовый режим).\n\n"
            "Введи число, на сколько пополнить баланс.\n"
            "Например: <b>100</b>",
            reply_markup=None,
        )


# ------------------- Хендлеры Telegram -------------------

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = message.from_user
    display_name = user.full_name or user.username or f"user_{user.id}"
    get_user_state(user.id, display_name=display_name)

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
        "• /daily — ежедневные задания 📆\n"
        "• /top — лидерборд 🏆\n"
        "• /paysupport — поддержка платежей 🛟\n\n"
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
        "• /daily — ежедневные задания 📆\n"
        "• /top — лидерборд 🏆\n"
        "• /paysupport — поддержка платежей 🛟"
    )
    await message.answer(text)


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
        state["mode"] = None
        await message.answer(
            f"Баланс пополнен на {amount} 💰\n"
            f"Текущий баланс: {state['balance']} 💰"
        )
        return

    # Обычный вопрос → проверяем доступ
    if not await ensure_access(message):
        return

    increment_activity(state, kind="text")
    await maybe_complete_daily(message, state)

    user_text = message.text or ""

    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        answer = await ask_ai_text(user_text)
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

    increment_activity(state, kind="voice")
    await maybe_complete_daily(message, state)

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

        answer = await ask_ai_text(
            f"Ученик сказал голосом: «{recognized_text}». Ответь ему как репетитор."
        )

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
    state = get_user_state(user.id, display_name=display_name)

    if not await ensure_access(message):
        return

    increment_activity(state, kind="photo")
    await maybe_complete_daily(message, state)

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

        answer = await analyze_image_with_question(file_bytes, question)
        await message.answer(answer)
    except Exception as e:
        logger.exception("Ошибка при обработке фото: %s", e)
        await message.answer(
            "Не удалось обработать фото. Попробуй ещё раз или добавь подпись с вопросом. 😔"
        )


@dp.message()
async def fallback_unknown(message: Message) -> None:
    await message.answer(
        "Извини, я понимаю только команды /start, /help, /menu, /paysupport, текст, голосовые и фото. 🙂"
    )


# ------------------- Точка входа -------------------

async def main():
    logger.info("Бот (aiogram 3) запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())