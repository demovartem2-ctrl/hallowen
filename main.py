import asyncio
import logging
import os
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, 
    LabeledPrice, 
    PreCheckoutQuery, 
    SuccessfulPayment
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web

# --- КОНФИГУРАЦИЯ ---
TOKEN = os.environ.get("BOT_TOKEN", "")
API_BASE = os.environ.get("API_BASE", "https://misugram.cloud-ip.cc")
ADMIN_USERNAMES = ["sardelka", "mehura"]  # Список админов по юзернейму
ADMIN_IDS = {1780243203, 1780243210}      # Список админов по ID

DB_FILE = "halloween_bot.db"

def is_admin(user: types.User) -> bool:
    if user is None:
        return False
    username = (user.username or "").lower()
    return user.id in ADMIN_IDS or username in ADMIN_USERNAMES

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS quest_proofs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            quest_name TEXT,
            proof_text TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_purchases (
            user_id INTEGER,
            item_id TEXT,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, item_id)
        )
    """)
    conn.commit()
    conn.close()

def get_balance(user_id: int) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def update_balance(user_id: int, username: str, amount: int) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT balance, username FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row:
        new_balance = row[0] + amount
        uname = username or row[1]
        cur.execute("UPDATE users SET balance = ?, username = ? WHERE user_id = ?", (new_balance, uname, user_id))
    else:
        new_balance = amount
        cur.execute("INSERT INTO users (user_id, username, balance) VALUES (?, ?, ?)", (user_id, username, new_balance))
    conn.commit()
    conn.close()
    return new_balance

def save_proof(user_id: int, quest_name: str, proof_text: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO quest_proofs (user_id, quest_name, proof_text, created_at) VALUES (?, ?, ?, ?)",
        (user_id, quest_name, proof_text, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

def get_top(limit: int = 10):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_pending(limit: int = 20):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, quest_name, created_at FROM quest_proofs WHERE status='pending' ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_purchase_count(user_id: int, item_id: str) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT count FROM user_purchases WHERE user_id = ? AND item_id = ?", (user_id, item_id))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def increment_purchase(user_id: int, item_id: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO user_purchases (user_id, item_id, count) VALUES (?, ?, 1) ON CONFLICT(user_id, item_id) DO UPDATE SET count = count + 1", (user_id, item_id))
    conn.commit()
    conn.close()

# --- СОСТОЯНИЯ (FSM) ---
class AdminAmount(StatesGroup):
    target_id = State()
    amount = State()

class BuyStars(StatesGroup):
    custom_amount = State()

# --- МАГАЗИН ---
SHOP_ITEMS = {
    "item_3blacks": {"name": "3 блека", "price": 650, "limit": 2, "emoji": "🖤"},
    "item_10000": {"name": "10000", "price": 550, "limit": 5, "emoji": "🔢"},
    "item_100": {"name": "100", "price": 50, "limit": None, "emoji": "💯"},
    "item_nft_user": {"name": "Нфт юз", "price": 800, "limit": 1, "emoji": "🖼️"},
    "item_nft_num": {"name": "Номер Nft", "price": 900, "limit": 1, "emoji": "🔢🖼️"},
    "item_scam": {"name": "Scam метка", "price": 1300, "limit": 1, "emoji": "⚠️"},
}

# --- КВЕСТЫ И ДОП ЗАДАНИЯ ---
QUESTS = {
    "quest_gifts": {"name": "Раздай 5 подарков", "reward": 25, "desc": "Раздай 5 подарков друзьям или в чатах."},
    "quest_pumpkin": {"name": "Фото с тыквой", "reward": 15, "desc": "Сделай фото с тыквой и отправь его организатору."},
}

EXTRA_TASKS = {
    "extra_kanal": {"name": "Подписка на @kanal", "reward": 15, "link": "https://t.me/kanal", "desc": "Подпишись на канал @kanal."},
    "extra_misugram": {"name": "Подписка на @misugram", "reward": 20, "link": "https://t.me/misugram", "desc": "Подпишись на канал @misugram."},
    "extra_devlog": {"name": "Подписка на @devlog", "reward": 20, "link": "https://t.me/devlog", "desc": "Подпишись на канал @devlog."},
}

# --- КЛАВИАТУРЫ ---
def main_menu_kb(user: types.User) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🕸️ Квесты", callback_data="quest_menu")
    b.button(text="📜 Доп. задания", callback_data="extra_menu")
    b.button(text="🎃 Мой котёл", callback_data="show_balance")
    b.button(text="🏆 Зал славы", callback_data="show_top")
    b.button(text="🛒 Магазин", callback_data="shop_menu")
    b.button(text="💎 Купить тыквы", callback_data="buy_stars_menu")
    b.button(text="🆔 Мой ID", callback_data="show_my_id")
    if is_admin(user):
        b.button(text="💀 Панель мастера", callback_data="admin_panel")
    b.adjust(2)
    return b.as_markup()

def shop_kb(user_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for item_id, data in SHOP_ITEMS.items():
        bought = get_purchase_count(user_id, item_id)
        limit = data["limit"]
        if limit is not None and bought >= limit:
            text = f"❌ {data['emoji']} {data['name']} (Куплено)"
            b.button(text=text, callback_data="ignore")
        else:
            limit_text = f"[{bought}/{limit}]" if limit else ""
            text = f"{data['emoji']} {data['name']} - {data['price']}🎃 {limit_text}"
            b.button(text=text, callback_data=f"buy_{item_id}")
    b.button(text="🔙 Назад", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def buy_stars_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="50 ⭐ (100 🎃)", callback_data="buy_stars_50")
    b.button(text="150 ⭐ (300 🎃)", callback_data="buy_stars_150")
    b.button(text="500 ⭐ (1000 🎃)", callback_data="buy_stars_500")
    b.button(text="✍️ Своя сумма", callback_data="buy_stars_custom")
    b.button(text="🔙 Назад", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def quests_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎁 Раздай 5 подарков (+25 тыкв)", callback_data="quest_gifts")
    b.button(text="📸 Фото с тыквой (+15 тыкв)", callback_data="quest_pumpkin")
    b.button(text="🔙 Назад в склеп", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def extra_tasks_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for task_id, data in EXTRA_TASKS.items():
        b.button(text=f"{data['name']} (+{data['reward']} 🎃)", callback_data=task_id)
    b.button(text="🔙 Назад в склеп", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def proof_kb(task_key: str, prefix: str = "proof_sent_") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"📩 Написать @sardelka или @mehura", url="https://t.me/sardelka")
    b.button(text="✅ Я отправил доказательство", callback_data=f"{prefix}{task_key}")
    b.button(text="🔙 Назад", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def admin_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🪄 Выдать тыквы", callback_data="admin_give")
    b.button(text="🪓 Отобрать тыквы", callback_data="admin_take")
    b.button(text="📜 Свитки (заявки)", callback_data="admin_pending")
    b.button(text="🔙 В главное меню", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def cancel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="admin_cancel")
    b.adjust(1)
    return b.as_markup()

def cancel_stars_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="buy_stars_menu")
    b.adjust(1)
    return b.as_markup()

# --- БОТ ---
logging.basicConfig(level=logging.INFO)
session = AiohttpSession(api=TelegramAPIServer.from_base(API_BASE))
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())

# --- ХЭНДЛЕРЫ ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    update_balance(message.from_user.id, message.from_user.username or message.from_user.full_name, 0)
    await message.answer(
        f"🦇 Приветствую, {message.from_user.first_name}! 🕸️\n\n"
        f"Тьма сгущается, Хэллоуин уже близко... 🎃\n"
        f"Собирай проклятые тыквы, выполняя задания, и стань Королём этого праздника! 👑\n\n"
        f"Что выберешь, смертный?",
        reply_markup=main_menu_kb(message.from_user),
    )

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("🔮 Главное меню. Тьма ждёт твоего выбора...", reply_markup=main_menu_kb(cb.from_user))
    await cb.answer()

@dp.callback_query(F.data == "ignore")
async def ignore_cb(cb: types.CallbackQuery):
    await cb.answer("Лимит покупок исчерпан!", show_alert=True)

@dp.callback_query(F.data == "show_my_id")
async def show_my_id(cb: types.CallbackQuery):
    await cb.message.edit_text(
        f"🆔 Твой ID: `{cb.from_user.id}`\n\n"
        f"📋 Скопируй его и отправь мастеру, чтобы получить награду.",
        reply_markup=main_menu_kb(cb.from_user),
        parse_mode="Markdown",
    )
    await cb.answer()

@dp.callback_query(F.data == "show_balance")
async def show_balance(cb: types.CallbackQuery):
    balance = get_balance(cb.from_user.id)
    await cb.message.edit_text(
        f"🎃 В твоём котле сейчас: **{balance}** тыкв.\n\n🕷️ Береги их, они очень ценятся в эту ночь...",
        reply_markup=main_menu_kb(cb.from_user),
        parse_mode="Markdown",
    )
    await cb.answer()

@dp.callback_query(F.data == "show_top")
async def show_top(cb: types.CallbackQuery):
    rows = get_top(10)
    if not rows:
        text = "Зал славы пуст. Тьма ещё не выбрала героев... 🕸️"
    else:
        medals = ["🥇", "🥈", "🥉"] + ["💀"] * 20
        text = "🏆 **Зал славы Хэллоуина** 🦇\n\n"
        for i, (uid, uname, bal) in enumerate(rows):
            name = f"@{uname}" if uname else f"id {uid}"
            text += f"{medals[i]} {name} — {bal} 🎃\n"
    await cb.message.edit_text(text, reply_markup=main_menu_kb(cb.from_user), parse_mode="Markdown")
    await cb.answer()

# --- МАГАЗИН ---
@dp.callback_query(F.data == "shop_menu")
async def show_shop(cb: types.CallbackQuery):
    await cb.message.edit_text(
        "🛒 **Тёмный магазин**\n\nВыбирай товар, смертный. Но помни: у всего есть своя цена...",
        reply_markup=shop_kb(cb.from_user.id),
        parse_mode="Markdown",
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("buy_item_"))
async def process_buy_item(cb: types.CallbackQuery):
    item_id = cb.data.replace("buy_", "")
    item = SHOP_ITEMS.get(item_id)
    if not item:
        await cb.answer("Товар не найден.", show_alert=True)
        return
    bought = get_purchase_count(cb.from_user.id, item_id)
    if item["limit"] is not None and bought >= item["limit"]:
        await cb.answer("Лимит покупок исчерпан!", show_alert=True)
        return
    balance = get_balance(cb.from_user.id)
    if balance < item["price"]:
        await cb.answer(f"Недостаточно тыкв! Нужно: {item['price']} 🎃", show_alert=True)
        return
    update_balance(cb.from_user.id, "", -item["price"])
    increment_purchase(cb.from_user.id, item_id)
    await cb.message.edit_text(
        f"✅ Ты успешно приобрёл **{item['name']}** за {item['price']} 🎃!\n\n"
        f"Остаток в котле: {get_balance(cb.from_user.id)} 🎃\n"
        f"Свяжись с @sardelka или @mehura для получения товара.",
        reply_markup=main_menu_kb(cb.from_user),
        parse_mode="Markdown"
    )
    # Уведомление админам
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"🛒 **Новая покупка!**\nПользователь @{cb.from_user.username or cb.from_user.id} купил **{item['name']}**.")
        except:
            pass
    await cb.answer("Покупка успешна!")

# --- ПОКУПКА ЗВЁЗД ---
@dp.callback_query(F.data == "buy_stars_menu")
async def show_buy_stars(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        "💎 **Обмен звёзд на тыквы**\n\n"
        "Курс: 1 ⭐ = 2 🎃\n"
        "Минимальная покупка: 100 🎃 (50 ⭐)\n\n"
        "Выбери пакет:",
        reply_markup=buy_stars_kb(),
        parse_mode="Markdown",
    )
    await cb.answer()

@dp.callback_query(F.data == "buy_stars_custom")
async def buy_stars_custom(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(BuyStars.custom_amount)
    await cb.message.edit_text(
        "✍️ Напиши, сколько тыкв ты хочешь купить (число должно быть кратно 2 и не меньше 100):",
        reply_markup=cancel_stars_kb(),
    )
    await cb.answer()

@dp.message(BuyStars.custom_amount)
async def process_custom_amount(message: types.Message, state: FSMContext):
    try:
        pumpkins = int(message.text.strip())
        if pumpkins < 100 or pumpkins % 2 != 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Ошибка! Введи число не меньше 100 и кратное 2 (например, 100, 200, 300).")
        return
    await state.clear()
    stars = pumpkins // 2
    prices = [LabeledPrice(label="XTR", amount=stars)]
    await message.answer_invoice(
        title="Покупка тыкв 🎃",
        description=f"Покупка {pumpkins} тыкв за {stars} звёзд.",
        prices=prices,
        provider_token="",
        currency="XTR",
        payload=f"stars_{pumpkins}",
    )

@dp.callback_query(F.data.startswith("buy_stars_") & ~F.data.startswith("buy_stars_custom") & ~F.data.startswith("buy_stars_menu"))
async def process_preset_stars(cb: types.CallbackQuery):
    stars = int(cb.data.replace("buy_stars_", ""))
    pumpkins = stars * 2
    prices = [LabeledPrice(label="XTR", amount=stars)]
    await cb.message.answer_invoice(
        title="Покупка тыкв 🎃",
        description=f"Покупка {pumpkins} тыкв за {stars} звёзд.",
        prices=prices,
        provider_token="",
        currency="XTR",
        payload=f"stars_{pumpkins}",
    )
    await cb.answer()

@dp.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    payload = message.successful_payment.invoice_payload
    pumpkins = int(payload.split("_")[1])
    new_balance = update_balance(message.from_user.id, "", pumpkins)
    await message.answer(
        f"🎉 Успешная покупка!\n"
        f"Тебе начислено **{pumpkins}** 🎃.\n\n"
        f"Твой баланс: **{new_balance}** 🎃.",
        parse_mode="Markdown"
    )

# --- КВЕСТЫ И ДОП ЗАДАНИЯ ---
@dp.callback_query(F.data == "quest_menu")
async def show_quests(cb: types.CallbackQuery):
    await cb.message.edit_text("🕸️ **Карта испытаний**\n\nВыполни задание и забери награду!", reply_markup=quests_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data.startswith("quest_") & ~F.data.startswith("quest_menu"))
async def handle_quest(cb: types.CallbackQuery):
    quest = QUESTS.get(cb.data)
    if not quest:
        await cb.answer("Неизвестное испытание.", show_alert=True)
        return
    await cb.message.edit_text(
        f"🕷️ Испытание: **{quest['name']}**\n\n📜 {quest['desc']}\n\n💰 Награда: **{quest['reward']} тыкв** 🎃\n\n📩 Докажи, что выполнил, написав: @sardelka или @mehura\nПосле отправки нажми кнопку ниже.",
        reply_markup=proof_kb(cb.data, "proof_sent_"),
        parse_mode="Markdown",
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("proof_sent_"))
async def handle_proof(cb: types.CallbackQuery):
    quest_key = cb.data.replace("proof_sent_", "")
    quest = QUESTS.get(quest_key)
    if not quest:
        await cb.answer("Ошибка.", show_alert=True)
        return
    save_proof(cb.from_user.id, quest["name"], "Пользователь заявил о выполнении.")
    await cb.message.edit_text(f"✅ Свиток с доказательством **{quest['name']}** отправлен в тёмную канцелярию!\nМастера проверят его.", parse_mode="Markdown")
    await cb.answer("Заявка отправлена!")

@dp.callback_query(F.data == "extra_menu")
async def show_extra_tasks(cb: types.CallbackQuery):
    await cb.message.edit_text("📜 **Дополнительные задания**\n\nПодпишись и получи награду!", reply_markup=extra_tasks_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data.startswith("extra_") & ~F.data.startswith("extra_menu"))
async def handle_extra_task(cb: types.CallbackQuery):
    task = EXTRA_TASKS.get(cb.data)
    if not task:
        await cb.answer("Неизвестное задание.", show_alert=True)
        return
    
    b = InlineKeyboardBuilder()
    b.button(text="📢 Перейти в канал", url=task["link"])
    b.button(text="✅ Я подписался", callback_data=f"extra_proof_sent_{cb.data}")
    b.button(text="🔙 Назад", callback_data="extra_menu")
    b.adjust(1)
    
    await cb.message.edit_text(
        f"📜 Задание: **{task['name']}**\n\n"
        f"🔗 Ссылка: {task['link']}\n\n"
        f"💰 Награда: **{task['reward']} тыкв** 🎃\n\n"
        f"Подпишись на канал, а затем нажми кнопку ниже.",
        reply_markup=b.as_markup(),
        parse_mode="Markdown",
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("extra_proof_sent_"))
async def handle_extra_proof(cb: types.CallbackQuery):
    task_key = cb.data.replace("extra_proof_sent_", "")
    task = EXTRA_TASKS.get(task_key)
    if not task:
        await cb.answer("Ошибка.", show_alert=True)
        return
    save_proof(cb.from_user.id, task["name"], "Пользователь заявил о подписке.")
    await cb.message.edit_text(f"✅ Свиток с доказательством **{task['name']}** отправлен в тёмную канцелярию!\nМастера проверят его.", parse_mode="Markdown")
    await cb.answer("Заявка отправлена!")

# --- АДМИН-ПАНЕЛЬ ---
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: types.CallbackQuery):
    if not is_admin(cb.from_user):
        await cb.answer("Тьма не пускает тебя сюда.", show_alert=True)
        return
    await cb.message.edit_text("💀 **Панель Мастера**\n\nРаспоряжайся тыквами и проверяй свитки:", reply_markup=admin_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "admin_pending")
async def admin_pending(cb: types.CallbackQuery):
    if not is_admin(cb.from_user):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    rows = get_pending(20)
    if not rows:
        text = "📜 Свитков в ожидании нет. Тишина..."
    else:
        text = "📜 **Свитки на проверку:**\n\n"
        for pid, uid, qname, ts in rows:
            text += f"🔸 #{pid} · user_id={uid} · {qname}\n"
        text += "\nДля подтверждения: `/approve <id>`\nДля отклонения: `/reject <id>`"
    await cb.message.edit_text(text, reply_markup=admin_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "admin_give")
async def admin_give_start(cb: types.CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    await state.update_data(action="give")
    await state.set_state(AdminAmount.target_id)
    await cb.message.edit_text("🪄 **Выдача тыкв**\n\nВведи **ID жертвы**, которой нужно дать тыквы:", parse_mode="Markdown", reply_markup=cancel_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_take")
async def admin_take_start(cb: types.CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    await state.update_data(action="take")
    await state.set_state(AdminAmount.target_id)
    await cb.message.edit_text("🪓 **Изъятие тыкв**\n\nВведи **ID грешника**, у которого нужно отобрать тыквы:", parse_mode="Markdown", reply_markup=cancel_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_cancel")
async def admin_cancel(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("💀 Панель Мастера:", reply_markup=admin_kb())
    await cb.answer("Отменено")

@dp.message(AdminAmount.target_id)
async def handle_target(message: types.Message, state: FSMContext):
    try:
        target = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("Нужен числовой ID. Попробуй ещё раз.")
        return
    await state.update_data(target=target)
    data = await state.get_data()
    action = data.get("action")
    verb = "выдать" if action == "give" else "отобрать"
    await state.set_state(AdminAmount.amount)
    await message.answer(f"Сколько тыкв {verb} у пользователя `{target}`?", parse_mode="Markdown", reply_markup=cancel_kb())

@dp.message(AdminAmount.amount)
async def handle_amount(message: types.Message, state: FSMContext):
    try:
        amount = abs(int(message.text.strip()))
        if amount == 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Нужно целое число (не 0).")
        return
    data = await state.get_data()
    target = data["target"]
    action = data.get("action")
    await state.clear()
    final_amount = amount if action == "give" else -amount
    new_balance = update_balance(target, "", final_amount)
    action_text = "Выдано" if action == "give" else "Отобрано"
    await message.answer(f"✅ {action_text} **{amount}** 🎃 у `{target}`.\nТеперь у него: **{new_balance}** тыкв.", parse_mode="Markdown")
    try:
        if action == "give":
            user_text = f"🪄 Мастер смилостивился над тобой!\nТебе начислено **{amount}** тыкв 🎃.\n\n🎃 Твой баланс: **{new_balance}**."
        else:
            user_text = f"🪓 Тьма покарала тебя!\nУ тебя изъято **{amount}** тыкв 🎃.\n\n🎃 Твой баланс: **{new_balance}**."
        await bot.send_message(target, user_text, parse_mode="Markdown")
    except Exception as e:
        logging.warning(f"Не смог уведомить {target}: {e}")

@dp.message(F.text.startswith("/approve "))
async def cmd_approve(message: types.Message):
    if not is_admin(message.from_user):
        return
    try:
        pid = int(message.text.split()[1])
    except (IndexError, ValueError):
        await message.answer("Формат: /approve <id>")
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id, quest_name, status FROM quest_proofs WHERE id = ?", (pid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        await message.answer("Свиток не найден.")
        return
    uid, qname, status = row
    if status != "pending":
        conn.close()
        await message.answer(f"Этот свиток уже {status}.")
        return
    
    # Ищем награду и в квестах, и в доп. заданиях
    all_tasks = list(QUESTS.values()) + list(EXTRA_TASKS.values())
    reward = next((q["reward"] for q in all_tasks if q["name"] == qname), 0)
    
    cur.execute("UPDATE quest_proofs SET status='approved' WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    new_balance = update_balance(uid, "", reward)
    try:
        await bot.send_message(uid, f"🎉 Твоё доказательство для задания **{qname}** принято!\nТебе начислено **{reward}** тыкв 🎃.\n🎃 Твой котёл: {new_balance}.", parse_mode="Markdown")
    except Exception:
        pass
    await message.answer(f"✅ Свиток #{pid} подтверждён, начислено {reward} тыкв.")

@dp.message(F.text.startswith("/reject "))
async def cmd_reject(message: types.Message):
    if not is_admin(message.from_user):
        return
    try:
        pid = int(message.text.split()[1])
    except (IndexError, ValueError):
        await message.answer("Формат: /reject <id>")
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id, quest_name, status FROM quest_proofs WHERE id = ?", (pid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        await message.answer("Свиток не найден.")
        return
    uid, qname, status = row
    if status != "pending":
        conn.close()
        await message.answer(f"Этот свиток уже {status}.")
        return
    cur.execute("UPDATE quest_proofs SET status='rejected' WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    try:
        await bot.send_message(uid, f"❌ Твоё доказательство для задания **{qname}** отклонено.\nЕсли это ошибка, напиши мастерам: @sardelka или @mehura.", parse_mode="Markdown")
    except Exception:
        pass
    await message.answer(f"Свиток #{pid} отклонён.")

# --- HEALTHCHECK ---
async def healthcheck(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/healthcheck", healthcheck)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
    await site.start()
    logging.info("Healthcheck server started.")

async def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в переменных окружения")
    init_db()
    asyncio.create_task(start_web_server())
    logging.info(f"Запускаю бота через API {API_BASE}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
