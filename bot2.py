import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import aiosqlite
import os
from dotenv import load_dotenv

load_dotenv()

# ============ КОНФИГ ============
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))
ADMIN_USER_IDS = [
    int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()
]
DB_PATH = "requests.db"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ============ БАЗА ДАННЫХ ============
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                currency_from TEXT,
                currency_to TEXT,
                amount REAL,
                address TEXT,
                contact TEXT,
                status TEXT DEFAULT 'active',
                admin_msg_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                result TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.commit()


async def create_request(user_id, user_name, c_from, c_to, amount, address, contact, result):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO requests 
               (user_id, user_name, currency_from, currency_to, amount, address, contact, result)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, user_name, c_from, c_to, amount, address, contact, result)
        )
        await db.commit()
        return cur.lastrowid


async def set_admin_msg_id(req_id, msg_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE requests SET admin_msg_id=? WHERE id=?", (msg_id, req_id))
        await db.commit()


async def get_user_active_requests(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, currency_from, currency_to, amount, address, result FROM requests "
            "WHERE user_id=? AND status='active' ORDER BY id DESC",
            (user_id,)
        )
        return await cur.fetchall()


async def get_request(req_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM requests WHERE id=?", (req_id,))
        return await cur.fetchone()


async def update_request_status(req_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE requests SET status=? WHERE id=?", (status, req_id))
        await db.commit()

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        await db.commit()


async def get_setting(key: str, default: str = None) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default

# ============ АДМИНЫ ============
async def is_admin(user_id: int) -> bool:
    """Проверяет, есть ли пользователь в списке админов (БД или .env)."""
    if user_id in ADMIN_USER_IDS:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,))
        return await cur.fetchone() is not None


async def add_admin(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
        await db.commit()


async def remove_admin(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        await db.commit()


async def list_admins():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM admins ORDER BY added_at")
        return await cur.fetchall()
    
# ============ КУРСЫ ============
async def build_kurs_text():
    rub = float(await get_setting("rub_to_vnd", default="0.85"))
    usd = float(await get_setting("usd_to_vnd", default="25500"))
    usdt = float(await get_setting("usdt_to_vnd", default="25500"))

    rub_rev = (1 / rub) * 10000
    usd_rev = (1 / usd) * 10000
    usdt_rev = (1 / usdt) * 10000

    text = (
        "💱 <b>Курс к вьетнамскому донгу (VND)</b>\n\n"
        f"🇷🇺 1 RUB = <b>{rub:,.2f}</b> VND\n"
        f"🇺🇸 1 USD = <b>{usd:,.2f}</b> VND\n"
        f"🪙 1 USDT ≈ <b>{usdt:,.2f}</b> VND\n\n"
        "🔄 <b>Обратный курс:</b>\n\n"
        f"10000 VND = <b>{rub_rev:,.2f}</b> RUB\n"
        f"10000 VND = <b>{usd_rev:,.2f}</b> USD\n"
        f"10000 VND = <b>{usdt_rev:,.2f}</b> USDT\n\n"
        "<i>Курс устанавливается администратором.</i>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="start")]
    ])
    return text, keyboard

async def calculate_result(currency_from: str, currency_to: str, amount: float) -> str:
    """Считает, сколько клиент получит на руки. Возвращает строку для отображения."""
    # Курсы: сколько VND за 1 единицу валюты
    rates_to_vnd = {
        "RUB": float(await get_setting("rub_to_vnd", default="320")),
        "USD": float(await get_setting("usd_to_vnd", default="25500")),
        "USDT": float(await get_setting("usdt_to_vnd", default="25500")),
        "VND": 1.0,  # 1 VND = 1 VND
    }

    c_from = currency_from.upper()
    c_to = currency_to.upper()

    if c_from == c_to:
        return f"{amount:,.2f} {c_to}"

    # Считаем через VND как промежуточную валюту
    if c_from == "VND":
        # VND → другая валюта: делим на курс целевой
        result = amount / rates_to_vnd[c_to]
    elif c_to == "VND":
        # Другая → VND: умножаем на курс исходной
        result = amount * rates_to_vnd[c_from]
    else:
        # Кросс-курс (например, USD → RUB): сначала в VND, потом в целевую
        amount_in_vnd = amount * rates_to_vnd[c_from]
        result = amount_in_vnd / rates_to_vnd[c_to]

    return f"{result:,.2f} {c_to}"

# ============ КЛАВИАТУРЫ ============
CURRENCY_EMOJI = {"RUB": "🇷🇺 Рубли", "USD": "🇺🇸 Доллары", "USDT": "🪙 USDT", "VND": "🇻🇳 Донги"}


def start_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💱 Курсы валют", callback_data="kurs")],
        [InlineKeyboardButton(text="📝 Оставить заявку", callback_data="req")],
        [InlineKeyboardButton(text="📋 Мои заявки", callback_data="my_reqs")],
    ])


def start_text(name):
    return (
        f"👋 Привет, {name}!\n\n"
        f"Здесь вы можете узнать наш курс на обмен Донгов и оставить заявку.\n"
        f"Для просмотра курса напишите /kurs или нажмите на кнопку.\n\n"
        f"⚠️ ВАЖНО: мы работаем только в Нячанге."
    )


def back_cancel_keyboard(back_cb):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=back_cb),
         InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel")]
    ])


# ============ FSM ============
class ReqForm(StatesGroup):
    currency_from = State()
    currency_to = State()
    amount = State()
    address = State()
    contact = State()
    confirm = State()
    
class AdminForm(StatesGroup):
    set_rub = State()    # ждём ввод курса RUB
    set_usd = State()    # ждём ввод курса USD
    set_usdt = State()   # ждём ввод курса USDT

class SupportForm(StatesGroup):
    waiting_message = State()

# ============ FSM-ХЕНДЛЕРЫ ЗАЯВКИ ============
@dp.callback_query(lambda c: c.data == "req")
async def req_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Рубли", callback_data="cf_RUB")],
        [InlineKeyboardButton(text="🇺🇸 Доллары", callback_data="cf_USD")],
        [InlineKeyboardButton(text="🪙 USDT", callback_data="cf_USDT")],
        [InlineKeyboardButton(text="🇻🇳 Донги", callback_data="cf_VND")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel")],
    ])
    await callback.message.edit_text(
        "📝 <b>Шаг 1 из 5</b>\n\nКакую валюту будете менять?",
        reply_markup=keyboard, parse_mode="HTML"
    )
    await state.set_state(ReqForm.currency_from)
    await callback.answer()


@dp.callback_query(ReqForm.currency_from, F.data.startswith("cf_"))
async def req_currency_from(callback: types.CallbackQuery, state: FSMContext):
    c_from = callback.data.split("_")[1]
    await state.update_data(currency_from=c_from)

    if c_from == "VND":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🇷🇺 Рубли", callback_data="ct_RUB")],
            [InlineKeyboardButton(text="🇺🇸 Доллары", callback_data="ct_USD")],
            [InlineKeyboardButton(text="🪙 USDT", callback_data="ct_USDT")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="req"),
             InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel")],
        ])
        await callback.message.edit_text(
            "📝 <b>Шаг 2 из 5</b>\n\nНа какую валюту будете менять донги?",
            reply_markup=keyboard, parse_mode="HTML"
        )
        await state.set_state(ReqForm.currency_to)
    else:
        await state.update_data(currency_to="VND")
        await callback.message.edit_text(
            f"📝 <b>Шаг 2 из 5</b>\n\n"
            f"Направление: {CURRENCY_EMOJI[c_from]} → 🇻🇳 Донги\n\n"
            f"Введите сумму:",
            reply_markup=back_cancel_keyboard("req"), parse_mode="HTML"
        )
        await state.set_state(ReqForm.amount)
    await callback.answer()


@dp.callback_query(ReqForm.currency_to, F.data.startswith("ct_"))
async def req_currency_to(callback: types.CallbackQuery, state: FSMContext):
    c_to = callback.data.split("_")[1]
    await state.update_data(currency_to=c_to)
    data = await state.get_data()
    c_from = data["currency_from"]

    await callback.message.edit_text(
        f"📝 <b>Шаг 2 из 5</b>\n\n"
        f"Направление: {CURRENCY_EMOJI[c_from]} → {CURRENCY_EMOJI[c_to]}\n\n"
        f"Введите сумму:",
        reply_markup=back_cancel_keyboard("req"), parse_mode="HTML"
    )
    await state.set_state(ReqForm.amount)
    await callback.answer()


@dp.message(ReqForm.amount)
async def req_amount(message: types.Message, state: FSMContext):
    raw = message.text.replace(" ", "").replace(",", ".")
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введите корректную сумму (положительное число):")
        return

    await state.update_data(amount=amount)
    await message.answer(
        "📝 <b>Шаг 3 из 5</b>\n\nВведите адрес или ближайший отель, куда приедет обменщик:",
        reply_markup=back_cancel_keyboard("req"), parse_mode="HTML"
    )
    await state.set_state(ReqForm.address)


@dp.message(ReqForm.address)
async def req_address(message: types.Message, state: FSMContext):
    await state.update_data(address=message.text)
    await message.answer(
        "📝 <b>Шаг 4 из 5</b>\n\nВведите контакт для связи (@телеграм или номер телефона):",
        reply_markup=back_cancel_keyboard("req"), parse_mode="HTML"
    )
    await state.set_state(ReqForm.contact)


@dp.message(ReqForm.contact)
async def req_contact(message: types.Message, state: FSMContext):
    await state.update_data(contact=message.text)
    data = await state.get_data()

    result_str = await calculate_result(
        data["currency_from"], data["currency_to"], data["amount"]
    )

    text = (
        "📝 <b>Шаг 5 из 5 — проверьте заявку</b>\n\n"
        f"💱 {CURRENCY_EMOJI[data['currency_from']]} → {CURRENCY_EMOJI[data['currency_to']]}\n"
        f"💰 Отдаёте: <b>{data['amount']:,.2f}</b>\n"
        f"💵 Получаете: <b>{result_str}</b>\n"
        f"📍 Адрес: {data['address']}\n"
        f"📞 Контакт: {data['contact']}\n\n"
        "Всё верно?"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="req_confirm")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="req_back_contact"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel")],
    ])
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(ReqForm.confirm)



@dp.callback_query(lambda c: c.data == "req_back_contact")
async def req_back_contact(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📝 <b>Шаг 4 из 5</b>\n\nВведите контакт для связи (@телеграм или номер телефона):",
        parse_mode="HTML"
    )
    await state.set_state(ReqForm.contact)
    await callback.answer()


@dp.callback_query(lambda c: c.data == "req_cancel")
async def req_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "❌ Заявка отменена.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 В меню", callback_data="start")]
        ])
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "req_confirm")
async def req_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()

    user = callback.from_user
    user_name = f"{user.full_name} (@{user.username})" if user.username else user.full_name

    result_str = await calculate_result(
        data["currency_from"], data["currency_to"], data["amount"]
    )

    req_id = await create_request(
        user.id, user_name,
        data["currency_from"], data["currency_to"],
        data["amount"], data["address"], data["contact"],
        result_str
    )
    admin_text = (
        f"🆕 <b>Заявка #{req_id}</b>\n\n"
        f"👤 {user_name}\n"
        f"🆔 <code>{user.id}</code>\n\n"
        f"💱 {CURRENCY_EMOJI[data['currency_from']]} → {CURRENCY_EMOJI[data['currency_to']]}\n"
        f"💰 Отдаёт: <b>{data['amount']:,.2f}</b>\n"
        f"💵 Получает: <b>{result_str}</b>\n"
        f"📍 Адрес: {data['address']}\n"
        f"📞 Контакт: {data['contact']}\n\n"
        f"Статус: 🟡 Активна"
    )
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Выполнено", callback_data=f"adm_done_{req_id}"),
         InlineKeyboardButton(text="❌ Отказ", callback_data=f"adm_cancel_{req_id}")]
    ])
    try:
        admin_msg = await bot.send_message(ADMIN_CHAT_ID, admin_text, reply_markup=admin_kb, parse_mode="HTML")
        await set_admin_msg_id(req_id, admin_msg.message_id)
    except Exception as e:
        logging.error("Не удалось отправить заявку в админ-чат: %s", e)

    await callback.message.edit_text(
        f"✅ <b>Заявка #{req_id} принята!</b>\n\n"
        f"Мы свяжемся с вами в ближайшее время.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 В меню", callback_data="start")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.message(Command("msg"))
async def cmd_msg(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "✉️ <b>Связь с поддержкой</b>\n\n"
        "Напишите ваше сообщение — мы передадим его администратору.\n\n"
        "Для отмены напишите /cancel.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="support_cancel")]
        ]),
        parse_mode="HTML"
    )
    await state.set_state(SupportForm.waiting_message)


@dp.message(SupportForm.waiting_message)
async def support_message(message: types.Message, state: FSMContext):
    await state.clear()
    
    user = message.from_user
    user_name = f"{user.full_name} (@{user.username})" if user.username else user.full_name
    
    admin_text = (
        f"✉️ <b>Сообщение в поддержку</b>\n\n"
        f"👤 {user_name}\n"
        f"🆔 <code>{user.id}</code>\n\n"
        f"💬 {message.text}"
    )
    try:
        await bot.send_message(
            ADMIN_CHAT_ID, admin_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="↩️ Ответить пользователю",
                    callback_data=f"support_reply_{user.id}"
                )]
            ])
        )
    except Exception as e:
        logging.error("Не удалось отправить сообщение поддержки: %s", e)
        await message.answer("😔 Не удалось отправить. Попробуйте позже.")
        return
    
    await message.answer(
        "✅ Сообщение отправлено! Мы ответим в ближайшее время.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 В меню", callback_data="start")]
        ])
    )


@dp.callback_query(lambda c: c.data == "support_cancel")
async def support_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "❌ Отправка сообщения отменена.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 В меню", callback_data="start")]
        ])
    )
    await callback.answer()

@dp.message(F.chat.id == ADMIN_CHAT_ID, F.reply_to_message)
async def admin_reply_to_user(message: types.Message):
    # Ищем user_id в тексте сообщения, на которое ответили
    reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
    # Ищем ID в <code>...</code>
    import re
    match = re.search(r"🆔 (\d+)", reply_text)
    if not match:
        return  # это не сообщение поддержки, игнорируем
    
    user_id = int(match.group(1))
    try:
        await bot.send_message(
            user_id,
            f"📬 <b>Ответ поддержки:</b>\n\n{message.text}",
            parse_mode="HTML"
        )
        await message.reply("✅ Отправлено пользователю")
    except Exception as e:
        logging.error("Не удалось ответить пользователю %s: %s", user_id, e)
        await message.reply("❌ Не удалось отправить. Возможно, пользователь заблокировал бота.")


# ============ АДМИН: СТАТУСЫ ЗАЯВОК ============
@dp.callback_query(F.data.startswith("adm_"))
async def admin_status(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    action, req_id = parts[1], int(parts[2])

    req = await get_request(req_id)
    if not req:
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    if req[8] != "active":
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    if action == "done":
        new_status = "done"
        label = "✅ Выполнено"
    else:
        new_status = "cancelled"
        label = "❌ Отказ"

    await update_request_status(req_id, new_status)

    # Редактируем сообщение в админ-чате
    user_id = req[1]
    user_name = req[2]
    c_from, c_to = req[3], req[4]
    amount, address, contact = req[5], req[6], req[7]
    result_str = req[11] or "—"

    new_text = (
        f"🆕 <b>Заявка #{req_id}</b>\n\n"
        f"👤 {user_name}\n"
        f"🆔 <code>{user_id}</code>\n\n"
        f"💱 {CURRENCY_EMOJI[c_from]} → {CURRENCY_EMOJI[c_to]}\n"
        f"💰 Отдаёт: <b>{amount:,.2f}</b>\n"
        f"💵 Получает: <b>{result_str}</b>\n"
        f"📍 Адрес: {address}\n"
        f"📞 Контакт: {contact}\n\n"
        f"Статус: {label}"
    )
    try:
        await callback.message.edit_text(new_text, parse_mode="HTML")
    except Exception as e:
        logging.error("Не удалось отредактировать сообщение: %s", e)

    # Уведомляем пользователя
    user_text = (
        f"{label}\n\n"
        f"Ваша заявка #{req_id} "
        + ("выполнена. Спасибо, что выбрали нас!" if new_status == "done" else "отклонена.")
    )
    try:
        await bot.send_message(user_id, user_text)
    except Exception as e:
        logging.error("Не удалось уведомить пользователя %s: %s", user_id, e)

    await callback.answer("Статус обновлён")


# ============ МОИ ЗАЯВКИ ============
@dp.callback_query(lambda c: c.data == "my_reqs")
async def my_requests(callback: types.CallbackQuery):
    reqs = await get_user_active_requests(callback.from_user.id)
    if not reqs:
        await callback.message.edit_text(
            "📋 У вас нет активных заявок.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 В меню", callback_data="start")]
            ])
        )
        await callback.answer()
        return

    text = "📋 <b>Ваши активные заявки:</b>\n\n"
    buttons = []
    for r in reqs:
        req_id, c_from, c_to, amount, address, result_str = r
        result_str = result_str or "—"
        text += (
            f"#{req_id}: {CURRENCY_EMOJI[c_from]} → {CURRENCY_EMOJI[c_to]}\n"
            f"   💰 {amount:,.2f} → {result_str} | 📍 {address}\n\n"
        )
        buttons.append([InlineKeyboardButton(
            text=f"❌ Отменить #{req_id}",
            callback_data=f"user_cancel_{req_id}"
        )])
    buttons.append([InlineKeyboardButton(text="🔙 В меню", callback_data="start")])

    await callback.message.edit_text(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("user_cancel_"))
async def user_cancel_request(callback: types.CallbackQuery):
    req_id = int(callback.data.split("_")[2])
    req = await get_request(req_id)
    if not req or req[1] != callback.from_user.id:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    if req[8] != "active":
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    await update_request_status(req_id, "cancelled")

    # Обновляем сообщение в админ-чате
    admin_msg_id = req[9]
    if admin_msg_id:
        c_from, c_to = req[3], req[4]
        amount, address, contact = req[5], req[6], req[7]
        result_str = req[11] or "—"

        new_text = (
            f"🆕 <b>Заявка #{req_id}</b>\n\n"
            f"👤 {req[2]}\n"
            f"🆔 <code>{req[1]}</code>\n\n"
            f"💱 {CURRENCY_EMOJI[c_from]} → {CURRENCY_EMOJI[c_to]}\n"
            f"💰 Отдаёт: <b>{amount:,.2f}</b>\n"
            f"💵 Получает: <b>{result_str}</b>\n"
            f"📍 Адрес: {address}\n"
            f"📞 Контакт: {contact}\n\n"
            f"Статус: ❌ Отменена пользователем"
        )
        try:
            await bot.edit_message_text(
                new_text, chat_id=ADMIN_CHAT_ID, message_id=admin_msg_id,
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error("Не удалось обновить сообщение в админ-чате: %s", e)

    await callback.answer("Заявка отменена")
    await my_requests(callback)


# ============ СТАРТ И КУРС ============
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        start_text(message.from_user.first_name),
        reply_markup=start_keyboard()
    )


@dp.message(Command("kurs"))
async def cmd_kurs(message: types.Message):
    try:
        text, keyboard = await build_kurs_text()
    except Exception as e:
        await message.answer("😔 Не удалось получить курс. Попробуйте позже.")
        logging.error("Ошибка при получении курса: %s", e)
        return
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@dp.callback_query(lambda c: c.data == "start")
async def back_to_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        start_text(callback.from_user.first_name),
        reply_markup=start_keyboard()
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "kurs")
async def process_kurs(callback: types.CallbackQuery):
    try:
        text, keyboard = await build_kurs_text()
    except Exception as e:
        await callback.message.edit_text("😔 Не удалось получить курс.")
        logging.error("Ошибка при получении курса: %s", e)
        await callback.answer()
        return
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    # В группах /cancel игнорируем, чтобы не создавать пустые сообщения
    if message.chat.type != "private":
        return

    current = await state.get_state()
    if current is None:
        await message.answer("Нечего отменять — вы не в процессе создания заявки.")
        return
    await state.clear()
    await message.answer(
        "❌ Действие отменено.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 В меню", callback_data="start")]
        ])
    )

# ============ АДМИН: УСТАНОВКА КУРСА ============
@dp.message(Command("setkurs"))
async def cmd_setkurs(message: types.Message, state: FSMContext):
    # Проверка прав
    if not await is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return

    parts = message.text.split()

    # --- Вариант 1: с аргументами (работает в любом чате) ---
    if len(parts) == 3:
        currency = parts[1].lower()
        if currency not in ("rub", "usd", "usdt"):
            await message.answer("⚠️ Валюта должна быть <code>rub</code>, <code>usd</code> или <code>usdt</code>.", parse_mode="HTML")
            return
        try:
            rate = float(parts[2].replace(",", "."))
            if rate <= 0:
                raise ValueError
        except ValueError:
            await message.answer("⚠️ Курс должен быть положительным числом.")
            return

        key = f"{currency}_to_vnd"
        await set_setting(key, str(rate))
        await message.answer(f"✅ Курс <b>1 {currency.upper()} = {rate:,.2f} VND</b> сохранён.", parse_mode="HTML")
        return

    # --- Вариант 2: без аргументов (только личка, пошаговый диалог) ---
    if message.chat.type != "private":
        await message.answer(
            "ℹ️ Пошаговый ввод работает только в личке со мной.\n\n"
            "Здесь, в группе, используйте команду с аргументом:\n"
            "<code>/setkurs rub 305</code>\n"
            "<code>/setkurs usd 25500</code>\n"
            "<code>/setkurs usdt 25500</code>",
            parse_mode="HTML"
        )
        return

    rub = await get_setting("rub_to_vnd", default="320")
    usd = await get_setting("usd_to_vnd", default="25500")
    usdt = await get_setting("usdt_to_vnd", default="25500")

    await message.answer(
        f"Текущие курсы:\n"
        f"🇷🇺 1 RUB = {rub} VND\n"
        f"🇺🇸 1 USD = {usd} VND\n"
        f"🪙 1 USDT = {usdt} VND\n\n"
        f"Введите новый курс <b>1 RUB = ? VND</b>\n"
        f"(или <code>-</code>, чтобы оставить без изменений):",
        parse_mode="HTML"
    )
    await state.set_state(AdminForm.set_rub)


@dp.message(AdminForm.set_rub)
async def admin_set_rub(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    if raw != "-":
        try:
            rate = float(raw.replace(",", "."))
            if rate <= 0:
                raise ValueError
            await set_setting("rub_to_vnd", str(rate))
        except ValueError:
            await message.answer("⚠️ Введите положительное число или <code>-</code>.", parse_mode="HTML")
            return

    await message.answer(
        "Теперь курс <b>1 USD = ? VND</b> (или <code>-</code>):",
        parse_mode="HTML"
    )
    await state.set_state(AdminForm.set_usd)


@dp.message(AdminForm.set_usd)
async def admin_set_usd(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    if raw != "-":
        try:
            rate = float(raw.replace(",", "."))
            if rate <= 0:
                raise ValueError
            await set_setting("usd_to_vnd", str(rate))
        except ValueError:
            await message.answer("⚠️ Введите положительное число или <code>-</code>.", parse_mode="HTML")
            return

    await message.answer(
        "Теперь курс <b>1 USDT = ? VND</b> (или <code>-</code>):",
        parse_mode="HTML"
    )
    await state.set_state(AdminForm.set_usdt)


@dp.message(AdminForm.set_usdt)
async def admin_set_usdt(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    if raw != "-":
        try:
            rate = float(raw.replace(",", "."))
            if rate <= 0:
                raise ValueError
            await set_setting("usdt_to_vnd", str(rate))
        except ValueError:
            await message.answer("⚠️ Введите положительное число или <code>-</code>.", parse_mode="HTML")
            return

    await state.clear()

    rub = await get_setting("rub_to_vnd", default="320")
    usd = await get_setting("usd_to_vnd", default="25500")
    usdt = await get_setting("usdt_to_vnd", default="25500")

    await message.answer(
        f"✅ Курсы сохранены:\n\n"
        f"🇷🇺 1 RUB = {rub} VND\n"
        f"🇺🇸 1 USD = {usd} VND\n"
        f"🪙 1 USDT = {usdt} VND",
        parse_mode="HTML"
    )

# ============ АДМИН: УПРАВЛЕНИЕ ДОСТУПОМ ============
@dp.message(Command("addadmin"))
async def cmd_addadmin(message: types.Message):
    if not await is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await message.answer(
            "Использование: <code>/addadmin 123456789</code>",
            parse_mode="HTML"
        )
        return
    new_id = int(parts[1])
    await add_admin(new_id)
    await message.answer(
        f"✅ Пользователь <code>{new_id}</code> добавлен в админы.",
        parse_mode="HTML"
    )


@dp.message(Command("deladmin"))
async def cmd_deladmin(message: types.Message):
    if not await is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await message.answer(
            "Использование: <code>/deladmin 123456789</code>",
            parse_mode="HTML"
        )
        return
    target_id = int(parts[1])

    if target_id in ADMIN_USER_IDS:
        await message.answer(
            "⚠️ Этот пользователь задан в <code>.env</code> как супер-админ. "
            "Удалить его через команду нельзя — правьте переменные на BotHost.",
            parse_mode="HTML"
        )
        return

    await remove_admin(target_id)
    await message.answer(
        f"🗑 Пользователь <code>{target_id}</code> удалён из админов.",
        parse_mode="HTML"
    )


@dp.message(Command("admins"))
async def cmd_admins(message: types.Message):
    if not await is_admin(message.from_user.id):
        return

    rows = await list_admins()
    env_ids = ADMIN_USER_IDS

    text = "👥 <b>Админы:</b>\n\n"

    if env_ids:
        text += "<b>Супер-админы (.env):</b>\n"
        for uid in env_ids:
            text += f"• <code>{uid}</code>\n"
        text += "\n"

    if rows:
        text += "<b>Добавленные через бота:</b>\n"
        for r in rows:
            text += f"• <code>{r[0]}</code>\n"
    else:
        text += "<i>Через бота пока никто не добавлен.</i>"

    await message.answer(text, parse_mode="HTML")
   
# ============ ОШИБКИ И ЗАПУСК ============
@dp.errors()
async def errors_handler(event: types.ErrorEvent):
    logging.exception("Ошибка в хендлере: %s", event.exception)


async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
