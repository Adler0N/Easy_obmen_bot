import asyncio
import logging
import aiohttp
import ssl
import certifi
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
DB_PATH = "requests.db"
MODIFIER = 0.81

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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def create_request(user_id, user_name, c_from, c_to, amount, address, contact):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO requests 
               (user_id, user_name, currency_from, currency_to, amount, address, contact)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, user_name, c_from, c_to, amount, address, contact)
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
            "SELECT id, currency_from, currency_to, amount, address FROM requests "
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


# ============ КУРСЫ ============
async def fetch_vnd_rates():
    url = "https://open.er-api.com/v6/latest/RUB"
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(url, timeout=10) as resp:
            data = await resp.json()
    if data.get("result") != "success":
        raise ValueError(f"API error: {data.get('error-type', 'unknown')}")
    rates = data["rates"]
    rub_to_vnd = rates["VND"]
    usd_to_vnd = rates["VND"] / rates["USD"]
    return {"RUB": rub_to_vnd, "USD": usd_to_vnd, "USDT": usd_to_vnd}


async def build_kurs_text():
    rates = await fetch_vnd_rates()
    rub_final = rates["RUB"] * MODIFIER
    usd_final = rates["USD"] * MODIFIER
    usdt_final = rates["USDT"] * MODIFIER

    rub_reverse = (1 / rates["RUB"]) * MODIFIER * 10000
    usd_reverse = (1 / rates["USD"]) * MODIFIER * 10000
    usdt_reverse = (1 / rates["USDT"]) * MODIFIER * 10000
    
    text = (
        "💱 <b>Курс к вьетнамскому донгу (VND)</b>\n\n"
        f"🇷🇺 1 RUB = <b>{rub_final:,.2f}</b> VND\n"
        f"🇺🇸 1 USD = <b>{usd_final:,.2f}</b> VND\n"
        f"🪙 1 USDT ≈ <b>{usdt_final:,.2f}</b> VND\n\n"
          "🔄 <b>Обратный курс:</b>\n\n"
    f"10000 VND = <b>{rub_reverse:,.2f}</b> RUB\n"
    f"10000 VND = <b>{usd_reverse:,.2f}</b> USD\n"
    f"10000 VND = <b>{usdt_reverse:,.2f}</b> USDT\n\n"
        f"<i>Курс рассчитан с коэффициентом {MODIFIER}</i>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="start")]
    ])
    return text, keyboard


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


def cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel")]
    ])


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

    text = (
        "📝 <b>Шаг 5 из 5 — проверьте заявку</b>\n\n"
        f"💱 {CURRENCY_EMOJI[data['currency_from']]} → {CURRENCY_EMOJI[data['currency_to']]}\n"
        f"💰 Сумма: <b>{data['amount']:,.2f}</b>\n"
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

    req_id = await create_request(
        user.id, user_name,
        data["currency_from"], data["currency_to"],
        data["amount"], data["address"], data["contact"]
    )

    admin_text = (
        f"🆕 <b>Заявка #{req_id}</b>\n\n"
        f"👤 {user_name}\n"
        f"🆔 <code>{user.id}</code>\n\n"
        f"💱 {CURRENCY_EMOJI[data['currency_from']]} → {CURRENCY_EMOJI[data['currency_to']]}\n"
        f"💰 Сумма: <b>{data['amount']:,.2f}</b>\n"
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

    new_text = (
        f"🆕 <b>Заявка #{req_id}</b>\n\n"
        f"👤 {user_name}\n"
        f"🆔 <code>{user_id}</code>\n\n"
        f"💱 {CURRENCY_EMOJI[c_from]} → {CURRENCY_EMOJI[c_to]}\n"
        f"💰 Сумма: <b>{amount:,.2f}</b>\n"
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
        req_id, c_from, c_to, amount, address = r
        text += (
            f"#{req_id}: {CURRENCY_EMOJI[c_from]} → {CURRENCY_EMOJI[c_to]}\n"
            f"   💰 {amount:,.2f} | 📍 {address}\n\n"
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
        new_text = (
            f"🆕 <b>Заявка #{req_id}</b>\n\n"
            f"👤 {req[2]}\n"
            f"🆔 <code>{req[1]}</code>\n\n"
            f"💱 {CURRENCY_EMOJI[c_from]} → {CURRENCY_EMOJI[c_to]}\n"
            f"💰 Сумма: <b>{amount:,.2f}</b>\n"
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
        logging.error("Ошибка API курса: %s", e)
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
        logging.error("Ошибка API курса: %s", e)
        await callback.answer()
        return
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
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
