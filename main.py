import asyncio
import logging
import os
import sqlite3
import random
import math
from datetime import datetime, timedelta
from typing import Optional

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
ADMIN_USERNAME = "sardelka"
DEFAULT_ADMINS = {1780243203, 1780243210}

DB_FILE = "halloween_bot.db"
active_games = {}

# --- УТИЛИТЫ ---
def is_admin(user: types.User) -> bool:
    if user is None: return False
    if (user.username or "").lower() == ADMIN_USERNAME.lower(): return True
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM admins WHERE user_id = ?", (user.id,))
    row = cur.fetchone()
    conn.close()
    return bool(row)

def get_all_admins() -> list[int]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM admins")
    rows = cur.fetchall()
    conn.close()
    return [row[0] for row in rows]

async def notify_admins(text: str):
    admin_ids = set(get_all_admins()) | DEFAULT_ADMINS
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode="Markdown")
        except: pass

def get_all_users():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    rows = cur.fetchall()
    conn.close()
    return [row[0] for row in rows]

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 0)")
    cur.execute("CREATE TABLE IF NOT EXISTS quest_proofs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, quest_name TEXT, proof_text TEXT, status TEXT DEFAULT 'pending', created_at TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS user_purchases (user_id INTEGER, item_id TEXT, count INTEGER DEFAULT 0, PRIMARY KEY (user_id, item_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS user_quest_completions (user_id INTEGER, quest_id TEXT, completed_at TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS user_bonuses (user_id INTEGER PRIMARY KEY, last_claim TEXT)")
    
    for admin_id in DEFAULT_ADMINS:
        cur.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (admin_id,))
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
    cur.execute("INSERT INTO quest_proofs (user_id, quest_name, proof_text, created_at) VALUES (?, ?, ?, ?)", (user_id, quest_name, proof_text, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_top(limit: int = 10):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_all_balances():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, balance FROM users ORDER BY balance DESC")
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

def get_setting(key, default=None):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key, value):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?", (key, value, value))
    conn.commit()
    conn.close()

def find_user_id(identifier: str) -> Optional[int]:
    identifier = identifier.strip()
    if identifier.isdigit(): return int(identifier)
    if identifier.startswith("@"):
        uname = identifier[1:].lower()
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE LOWER(username) = ?", (uname,))
        row = cur.fetchone()
        conn.close()
        if row: return row[0]
    return None

def get_last_bonus(user_id: int) -> Optional[datetime]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT last_claim FROM user_bonuses WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row: return datetime.fromisoformat(row[0])
    return None

def set_bonus_claimed(user_id: int):
    now_str = datetime.now().isoformat()
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO user_bonuses (user_id, last_claim) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET last_claim = ?", (user_id, now_str, now_str))
    conn.commit()
    conn.close()

# --- ПУЛ КВЕСТОВ ---
QUEST_POOL = {
    "q1": {"name": "Раздай 5 подарков", "reward": 25, "desc": "Раздай 5 подарков друзьям или в чатах."},
    "q2": {"name": "Фото с тыквой", "reward": 15, "desc": "Сделай фото с тыквой и отправь его организатору."},
    "q3": {"name": "Купи 3 подарка себе", "reward": 10, "desc": "Купи 3 подарка себе в профиль Telegram."},
}

def check_and_rotate_quests():
    now = datetime.now()
    last_str = get_setting("last_rotation")
    active_str = get_setting("active_quests")
    
    if last_str and active_str:
        last_time = datetime.fromisoformat(last_str)
        if now - last_time < timedelta(hours=12):
            return active_str.split(",")
    
    keys = list(QUEST_POOL.keys())
    random.shuffle(keys)
    active = keys[:3]
    
    set_setting("last_rotation", now.isoformat())
    set_setting("active_quests", ",".join(active))
    return active

# --- СОСТОЯНИЯ (FSM) ---
class AdminAmount(StatesGroup):
    target_id = State()
    amount = State()

class GrantAdmin(StatesGroup):
    target_id = State()

class BuyStars(StatesGroup):
    custom_amount = State()

class TransferPumpkins(StatesGroup):
    target = State()
    amount = State()

class AdminViewBalance(StatesGroup):
    target = State()

class RocketGame(StatesGroup):
    bet = State()

class Broadcast(StatesGroup):
    message = State()

# --- МАГАЗИН ---
SHOP_ITEMS = {
    "item_3blacks": {"name": "3 блека", "price": 650, "limit": 2, "emoji": "🖤"},
    "item_nft_user": {"name": "Нфт юз", "price": 800, "limit": 1, "emoji": "🖼️"},
    "item_nft_num": {"name": "Номер Nft", "price": 900, "limit": 1, "emoji": "🔢🖼️"},
    "item_scam": {"name": "Scam метка", "price": 1300, "limit": 1, "emoji": "⚠️"},
}

# --- КЛАВИАТУРЫ ---
def main_menu_kb(user: types.User) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🕸️ Квесты", callback_data="quest_menu")
    b.button(text="💰 Связанное с балансом", callback_data="balance_menu")
    b.button(text="🎮 Мини-игры", callback_data="games_menu")
    b.button(text="🏆 Зал славы", callback_data="show_top")
    b.button(text="🎁 Бонус", callback_data="daily_bonus")
    b.button(text="🆔 Мой ID", callback_data="show_my_id")
    b.button(text="🆘 Поддержка", callback_data="support_menu")
    if is_admin(user):
        b.button(text="💀 Панель мастера", callback_data="admin_panel")
    b.adjust(2)
    return b.as_markup()

def balance_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎃 Мой баланс", callback_data="show_balance")
    b.button(text="🛒 Магазин", callback_data="shop_menu")
    b.button(text="💸 Перевод", callback_data="transfer_start")
    b.button(text="💎 Купить тыквы", callback_data="buy_stars_menu")
    b.button(text="🔙 Назад в меню", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def games_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚀 Ракета (x2, x3...)", callback_data="game_rocket")
    b.button(text="🍬 Сладость или Гадость", callback_data="game_trickortreat")
    b.button(text="🔙 Назад в меню", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def rocket_bet_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Ставка 10 🎃", callback_data="rocket_bet_10")
    b.button(text="Ставка 50 🎃", callback_data="rocket_bet_50")
    b.button(text="Ставка 100 🎃", callback_data="rocket_bet_100")
    b.button(text="✍️ Своя сумма", callback_data="rocket_bet_custom")
    b.button(text="🔙 Назад", callback_data="games_menu")
    b.adjust(1)
    return b.as_markup()

def rocket_cashout_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💰 ЗАБРАТЬ ТЫКВЫ", callback_data="rocket_cashout")
    b.adjust(1)
    return b.as_markup()

def support_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👻 @sardelka", url="https://t.me/sardelka")
    b.button(text="👾 @mehura", url="https://t.me/mehura")
    b.button(text="🔙 Назад", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def shop_kb(user_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for item_id, data in SHOP_ITEMS.items():
        bought = get_purchase_count(user_id, item_id)
        limit = data["limit"]
        
        if limit is not None and bought >= limit:
            b.button(text=f"❌ {data['emoji']} {data['name']} (Куплено)", callback_data="ignore")
        else:
            limit_text = f"[{bought}/{limit}]" if limit else ""
            b.button(text=f"{data['emoji']} {data['name']} - {data['price']}🎃 {limit_text}", callback_data=f"buy_{item_id}")
            
    b.button(text="🔙 Назад", callback_data="balance_menu")
    b.adjust(1)
    return b.as_markup()

def buy_stars_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="100 🎃 (1750 ⭐)", callback_data="buy_stars_100")
    b.button(text="250 🎃 (4375 ⭐)", callback_data="buy_stars_250")
    b.button(text="500 🎃 (8750 ⭐)", callback_data="buy_stars_500")
    b.button(text="✍️ Своя сумма", callback_data="buy_stars_custom")
    b.button(text="🔙 Назад", callback_data="balance_menu")
    b.adjust(1)
    return b.as_markup()

def quests_kb(user_id: int) -> InlineKeyboardMarkup:
    active_quests = check_and_rotate_quests()
    b = InlineKeyboardBuilder()
    last_rot_str = get_setting("last_rotation")
    
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT quest_id FROM user_quest_completions WHERE user_id = ? AND completed_at > ?", (user_id, last_rot_str))
    completed = [row[0] for row in cur.fetchall()]
    conn.close()

    for qid in active_quests:
        q = QUEST_POOL[qid]
        if qid in completed:
            b.button(text=f"✅ {q['name']} (Выполнено)", callback_data="ignore")
        else:
            b.button(text=f"🎁 {q['name']} (+{q['reward']} тыкв)", callback_data=f"quest_{qid}")
            
    b.button(text="📜 Доп. квесты", callback_data="extra_quests") # Новая кнопка
    b.button(text="🔙 Назад в меню", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def proof_kb(quest_key: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"📩 Написать @{ADMIN_USERNAME}", url=f"https://t.me/{ADMIN_USERNAME}")
    b.button(text="✅ Я отправил доказательство", callback_data=f"proof_sent_{quest_key}")
    b.button(text="🔙 Назад к квестам", callback_data="quest_menu")
    b.adjust(1)
    return b.as_markup()

def admin_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🪄 Выдать тыквы", callback_data="admin_give")
    b.button(text="🪓 Отобрать тыквы", callback_data="admin_take")
    b.button(text="👁 Баланс юзера", callback_data="admin_check_bal")
    b.button(text="📊 Балансы всех", callback_data="admin_all_bals")
    b.button(text="📜 Свитки (заявки)", callback_data="admin_pending")
    b.button(text="👑 Выдать админку", callback_data="admin_grant")
    b.button(text="📢 Сообщение всем", callback_data="admin_broadcast") # Новая кнопка
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
    b.button(text="❌ Отмена", callback_data="balance_menu")
    b.adjust(1)
    return b.as_markup()

def cancel_transfer_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="balance_menu")
    b.adjust(1)
    return b.as_markup()

def cancel_view_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="admin_panel")
    b.adjust(1)
    return b.as_markup()

def cancel_rocket_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="games_menu")
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
    await cb.answer("Это действие сейчас недоступно.", show_alert=True)

@dp.callback_query(F.data == "show_my_id")
async def show_my_id(cb: types.CallbackQuery):
    await cb.message.edit_text(
        f"🆔 Твой ID: `{cb.from_user.id}`\n\n📋 Скопируй его и отправь мастеру @{ADMIN_USERNAME}.",
        reply_markup=main_menu_kb(cb.from_user), parse_mode="Markdown",
    )
    await cb.answer()

@dp.callback_query(F.data == "support_menu")
async def show_support(cb: types.CallbackQuery):
    await cb.message.edit_text(
        "🆘 **Служба спасения от нечисти**\n\nЕсли у тебя возникли проблемы, пиши мастерам:\n\n👻 @sardelka\n👾 @mehura",
        reply_markup=support_kb(), parse_mode="Markdown"
    )
    await cb.answer()

@dp.callback_query(F.data == "balance_menu")
async def show_balance_menu(cb: types.CallbackQuery):
    await cb.message.edit_text("💰 **Всё, что связано с тыквами:**", reply_markup=balance_menu_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "show_balance")
async def show_balance(cb: types.CallbackQuery):
    balance = get_balance(cb.from_user.id)
    await cb.message.edit_text(
        f"🎃 В твоём котле сейчас: **{balance}** тыкв.\n\n🕷️ Береги их...",
        reply_markup=balance_menu_kb(), parse_mode="Markdown",
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

# --- ЕЖЕДНЕВНЫЙ БОНУС ---
@dp.callback_query(F.data == "daily_bonus")
async def claim_daily_bonus(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    now = datetime.now()
    last_claim = get_last_bonus(user_id)
    bonus_amount = 25
    
    if last_claim:
        time_diff = now - last_claim
        if time_diff < timedelta(hours=24):
            remaining = timedelta(hours=24) - time_diff
            hours, remainder = divmod(remaining.seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            await cb.answer(f"⏳ Следующую порцию тьмы можно получить через {hours} ч. {minutes} мин.", show_alert=True)
            return

    new_balance = update_balance(user_id, "", bonus_amount)
    set_bonus_claimed(user_id)
    
    await cb.message.edit_text(
        f"🎁 **Тёмный дар получен!**\n\n"
        f"Тьма благоволит тебе и дарует **{bonus_amount}** тыкв 🎃.\n\n"
        f"🎃 Твой котёл: **{new_balance}**.\n"
        f"Возвращайся через 24 часа за новой порцией!",
        reply_markup=main_menu_kb(cb.from_user), parse_mode="Markdown"
    )
    await cb.answer("Бонус получен!")

# --- МИНИ-ИГРЫ ---
@dp.callback_query(F.data == "games_menu")
async def show_games(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if cb.from_user.id in active_games:
        await cb.answer("Ты уже в игре! Заверши текущую ракету.", show_alert=True)
        return
    await cb.message.edit_text("🎮 **Игровой зал**\n\nВыбирай испытание. Тьма любит азартных...", reply_markup=games_menu_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "game_trickortreat")
async def trick_or_treat(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    balance = get_balance(user_id)
    
    if balance < 25:
        await cb.answer("❌ Для игры нужно минимум 25 тыкв! Тьма не любит нищих.", show_alert=True)
        return
        
    if random.random() < 0.5:
        new_balance = update_balance(user_id, "", 25)
        text = (
            f"🍬 **Сладость!**\n\n"
            f"Тьма оказалась благосклонна к тебе.\n"
            f"Ты получаешь **+25** тыкв 🎃!\n\n"
            f"🎃 Твой баланс: **{new_balance}**."
        )
    else:
        new_balance = update_balance(user_id, "", -25)
        text = (
            f"🧟 **Гадость!**\n\n"
            f"Древнее проклятие настигло тебя.\n"
            f"У тебя отняли **25** тыкв 🎃!\n\n"
            f"🎃 Твой баланс: **{new_balance}**."
        )
        
    await cb.message.edit_text(text, reply_markup=games_menu_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "game_rocket")
async def rocket_menu(cb: types.CallbackQuery):
    await cb.message.edit_text(
        "🚀 **Игра «Ракета»**\n\nПравила: ты делаешь ставку. Ракета летит, множитель растет.\nВ любой момент нажми **«Забрать»**, чтобы получить ставку × множитель.\n⚠️ Если ракета взорвется раньше — ставка сгорит!\n\nВыбери размер ставки:",
        reply_markup=rocket_bet_kb(), parse_mode="Markdown"
    )
    await cb.answer()

@dp.callback_query(F.data == "rocket_bet_custom")
async def rocket_bet_custom(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(RocketGame.bet)
    await cb.message.edit_text("✍️ Введи сумму ставки в тыквах (целое число):", reply_markup=cancel_rocket_kb())
    await cb.answer()

@dp.message(RocketGame.bet)
async def rocket_bet_custom_handler(message: types.Message, state: FSMContext):
    try:
        bet = int(message.text.strip())
        if bet <= 0: raise ValueError
    except ValueError:
        await message.answer("❌ Введи целое число больше 0.")
        return
    await state.clear()
    await start_rocket_game(message.from_user.id, bet, message)

@dp.callback_query(F.data.startswith("rocket_bet_") & ~F.data.startswith("rocket_bet_custom"))
async def rocket_bet_preset(cb: types.CallbackQuery):
    bet = int(cb.data.replace("rocket_bet_", ""))
    await start_rocket_game(cb.from_user.id, bet, cb.message, cb)

async def start_rocket_game(user_id: int, bet: int, message: types.Message, cb: types.CallbackQuery = None):
    if user_id in active_games:
        if cb: await cb.answer("Ты уже в игре!", show_alert=True)
        else: await message.answer("Ты уже в игре!")
        return

    balance = get_balance(user_id)
    if balance < bet:
        if cb: await cb.answer(f"Недостаточно тыкв! У тебя только {balance} 🎃.", show_alert=True)
        else: await message.answer(f"❌ Недостаточно тыкв! У тебя только {balance} 🎃.")
        return

    update_balance(user_id, "", -bet)
    crash_point = round(random.uniform(1.2, 5.0), 1)
    game_text = f"🚀 **Ракета взлетела!**\n\nМножитель: **x1.0**\nСтавка: {bet} 🎃\n\nУспей забрать!"
    
    if cb:
        await cb.message.edit_text(game_text, reply_markup=rocket_cashout_kb(), parse_mode="Markdown")
        msg = cb.message
    else:
        msg = await message.answer(game_text, reply_markup=rocket_cashout_kb(), parse_mode="Markdown")

    task = asyncio.create_task(rocket_loop(user_id, bet, crash_point, msg))
    active_games[user_id] = {"bet": bet, "multiplier": 1.0, "crash_point": crash_point, "message_id": msg.message_id, "task": task}

async def rocket_loop(user_id: int, bet: int, crash_point: float, msg: types.Message):
    multiplier = 1.0
    try:
        while True:
            await asyncio.sleep(1)
            if user_id not in active_games: return
            multiplier += 0.2
            
            if multiplier >= crash_point:
                if user_id in active_games:
                    del active_games[user_id]
                    await bot.edit_message_text(
                        chat_id=user_id, message_id=msg.message_id,
                        text=f"💥 **БУМ! Ракета взорвалась!**\n\nМножитель достиг **x{crash_point:.1f}**\nТы потерял **{bet}** 🎃.",
                        reply_markup=None
                    )
                return

            profit = int(bet * multiplier)
            await bot.edit_message_text(
                chat_id=user_id, message_id=msg.message_id,
                text=f"🚀 **Ракета летит!**\n\nМножитель: **x{multiplier:.1f}**\nСтавка: {bet} 🎃\nМожно забрать: **{profit}** 🎃\n\nУспей нажать!",
                reply_markup=rocket_cashout_kb(), parse_mode="Markdown"
            )
            active_games[user_id]["multiplier"] = multiplier
            
    except asyncio.CancelledError: pass 
    except Exception as e:
        logging.error(f"Ошибка в игре ракеты: {e}")
        if user_id in active_games: del active_games[user_id]

@dp.callback_query(F.data == "rocket_cashout")
async def rocket_cashout(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    if user_id not in active_games:
        await cb.answer("Игра уже окончена!", show_alert=True)
        return

    game = active_games[user_id]
    game["task"].cancel()
    del active_games[user_id]
    
    bet, multiplier = game["bet"], game["multiplier"]
    profit = int(bet * multiplier)
    new_balance = update_balance(user_id, "", profit)
    
    await cb.message.edit_text(
        f"💰 **Ты успел забрать!**\n\nМножитель: **x{multiplier:.1f}**\nСтавка: {bet} 🎃\nВыигрыш: **{profit}** 🎃\n\n🎃 Твой баланс: **{new_balance}**.",
        reply_markup=games_menu_kb(), parse_mode="Markdown"
    )
    await cb.answer("Тыквы у тебя в котле!")

# --- ПЕРЕВОД ТЫКВ ---
@dp.callback_query(F.data == "transfer_start")
async def transfer_start(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(TransferPumpkins.target)
    await cb.message.edit_text("💸 **Перевод тыкв**\n\nВведи **ID** или **@username** получателя:", reply_markup=cancel_transfer_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.message(TransferPumpkins.target)
async def transfer_target(message: types.Message, state: FSMContext):
    target_id = find_user_id(message.text)
    if not target_id: return await message.answer("❌ Пользователь не найден в базе бота.")
    if target_id ==
