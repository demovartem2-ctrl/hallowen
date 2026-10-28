import asyncio
import logging
import os
import random
import math
from datetime import datetime, timedelta
from typing import Optional

import asyncpg
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, 
    LabeledPrice, 
    PreCheckoutQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web

# --- КОНФИГУРАЦИЯ ---
TOKEN = os.environ.get("BOT_TOKEN", "")
API_BASE = os.environ.get("API_BASE", "https://misugram.cloud-ip.cc")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL: raise RuntimeError("DATABASE_URL не задан!")

ADMIN_USERNAME = "sardelka"
DEFAULT_ADMINS = {1780243203, 1780243210}
GIFT_IDS = ["9000000000000159", "9000000000000158", "9000000000000153", "9000000000000152", "9000000000000148"]

db_pool: asyncpg.Pool = None
active_games = {}

# --- БАЗА ДАННЫХ ---
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, ssl='require')
    async with db_pool.acquire() as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT, balance BIGINT DEFAULT 0, spent BIGINT DEFAULT 0, registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        await conn.execute("CREATE TABLE IF NOT EXISTS quest_proofs (id SERIAL PRIMARY KEY, user_id BIGINT, quest_name TEXT, proof_text TEXT, status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        await conn.execute("CREATE TABLE IF NOT EXISTS user_purchases (user_id BIGINT, item_id TEXT, count INTEGER DEFAULT 0, PRIMARY KEY (user_id, item_id))")
        await conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        await conn.execute("CREATE TABLE IF NOT EXISTS user_quest_completions (user_id BIGINT, quest_id TEXT, completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        await conn.execute("CREATE TABLE IF NOT EXISTS admins (user_id BIGINT PRIMARY KEY)")
        await conn.execute("CREATE TABLE IF NOT EXISTS user_bonuses (user_id BIGINT PRIMARY KEY, last_claim TIMESTAMP, streak INTEGER DEFAULT 0)")
        await conn.execute("CREATE TABLE IF NOT EXISTS user_trickortreat (user_id BIGINT PRIMARY KEY, last_played TIMESTAMP)")
        await conn.execute("CREATE TABLE IF NOT EXISTS user_wheel (user_id BIGINT PRIMARY KEY, last_played TIMESTAMP)")
        await conn.execute("CREATE TABLE IF NOT EXISTS user_case_cd (user_id BIGINT PRIMARY KEY, last_time TIMESTAMP)")
        
        for admin_id in DEFAULT_ADMINS:
            await conn.execute("INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING", admin_id)

async def get_user_info(user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT username, balance, spent, registered_at FROM users WHERE user_id = $1", user_id)

async def update_balance(user_id: int, username: str, amount: int, is_spending: bool = False) -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance, spent FROM users WHERE user_id = $1", user_id)
        if row:
            current_bal = row['balance']
            raw_new_bal = current_bal + amount
            new_bal = max(0, raw_new_bal)
            actual_change = new_bal - current_bal
            new_spent = row['spent'] + (abs(actual_change) if is_spending and actual_change < 0 else 0)
            await conn.execute("UPDATE users SET balance = $1, spent = $2, username = COALESCE($3, username) WHERE user_id = $4", new_bal, new_spent, username, user_id)
            return new_bal
        else:
            new_bal = max(0, amount)
            new_spent = abs(new_bal) if is_spending and new_bal < 0 else 0
            await conn.execute("INSERT INTO users (user_id, username, balance, spent) VALUES ($1, $2, $3, $4)", user_id, username, new_bal, new_spent)
            return new_bal

def is_admin_check(user_id: int) -> bool:
    return user_id in DEFAULT_ADMINS

async def is_admin_db(user_id: int) -> bool:
    if is_admin_check(user_id): return True
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM admins WHERE user_id = $1", user_id)
    return bool(row)

async def notify_admins(text: str):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM admins")
    admin_ids = set([r['user_id'] for r in rows]) | DEFAULT_ADMINS
    for aid in admin_ids:
        try: await bot.send_message(aid, text, parse_mode="Markdown")
        except: pass

async def get_all_users():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users")
    return [r['user_id'] for r in rows]

async def get_total_users_count() -> int:
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM users")
    return count or 0

async def find_user_id(identifier: str) -> Optional[int]:
    identifier = identifier.strip()
    if identifier.isdigit(): return int(identifier)
    if identifier.startswith("@"):
        uname = identifier[1:].lower()
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT user_id FROM users WHERE LOWER(username) = $1", uname)
        if row: return row['user_id']
    return None

async def get_top(limit: int = 10):
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT user_id, username, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT $1", limit)

async def get_pending(limit: int = 20):
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT id, user_id, quest_name, created_at FROM quest_proofs WHERE status='pending' ORDER BY id DESC LIMIT $1", limit)

async def save_proof(user_id: int, quest_name: str):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO quest_proofs (user_id, quest_name) VALUES ($1, $2)", user_id, quest_name)

# --- КВЕСТЫ ---
QUEST_POOL = {
    "q1": {"name": "Раздай 5 подарков", "reward": 25, "desc": "Раздай 5 подарков."},
    "q2": {"name": "Фото с тыквой", "reward": 15, "desc": "Сделай фото с тыквой."},
    "q3": {"name": "Купи 3 подарка", "reward": 10, "desc": "Купи 3 подарка себе."},
}

async def check_and_rotate_quests():
    async with db_pool.acquire() as conn:
        last_str = await conn.fetchval("SELECT value FROM settings WHERE key = 'last_rotation'")
        active_str = await conn.fetchval("SELECT value FROM settings WHERE key = 'active_quests'")
        
        if last_str and active_str:
            last_time = datetime.fromisoformat(last_str)
            if datetime.now() - last_time < timedelta(hours=12):
                return active_str.split(",")
        
        keys = list(QUEST_POOL.keys())
        random.shuffle(keys)
        active = keys[:3]
        
        now = datetime.now().isoformat()
        await conn.execute("INSERT INTO settings (key, value) VALUES ('last_rotation', $1) ON CONFLICT (key) DO UPDATE SET value = $1", now)
        await conn.execute("INSERT INTO settings (key, value) VALUES ('active_quests', $1) ON CONFLICT (key) DO UPDATE SET value = $1", ",".join(active))
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
class AdminGiveAll(StatesGroup):
    amount = State()
class AdminTakeAll(StatesGroup):
    amount = State()
class Upgrader(StatesGroup):
    amount = State()

# --- МАГАЗИН ---
SHOP_ITEMS = {
    "item_3blacks": {"name": "3 блека", "price": 650, "limit": 2, "emoji": "🖤"},
    "item_nft_user": {"name": "Нфт юз", "price": 800, "limit": 1, "emoji": "🖼️"},
    "item_nft_num": {"name": "Номер Nft", "price": 900, "limit": 1, "emoji": "🔢🖼️"},
    "item_scam": {"name": "Scam метка", "price": 1300, "limit": 1, "emoji": "⚠️"},
}

# --- КЛАВИАТУРЫ ---
def main_menu_kb(is_admin: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🕸️ Квесты", callback_data="quest_menu")
    b.button(text="💰 Связанное с балансом", callback_data="balance_menu")
    b.button(text="🎮 Мини-игры", callback_data="games_menu")
    b.button(text="📊 Профиль", callback_data="show_profile")
    b.button(text="🏆 Зал славы", callback_data="show_top")
    b.button(text="🎁 Бонус", callback_data="daily_bonus")
    b.button(text="🆔 Мой ID", callback_data="show_my_id")
    b.button(text="🆘 Поддержка", callback_data="support_menu")
    if is_admin: b.button(text="💀 Панель мастера", callback_data="admin_panel")
    b.adjust(2)
    return b.as_markup()

def balance_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎃 Мой баланс", callback_data="show_balance")
    b.button(text="🛒 Магазин", callback_data="shop_menu")
    b.button(text="💸 Перевод", callback_data="transfer_start")
    b.button(text="💎 Купить тыквы", callback_data="buy_stars_menu")
    b.button(text="🔙 Назад", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def games_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚀 Ракета", callback_data="game_rocket")
    b.button(text="🍬 Сладость/Гадость", callback_data="game_trickortreat")
    b.button(text="📈 Апгрейдер", callback_data="game_upgrader")
    b.button(text="🎡 Колесо (20🎃)", callback_data="game_wheel")
    b.button(text="🎁 Кейсы", callback_data="case_menu")
    b.button(text="🔙 Назад", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def cases_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🌭 Сосиска (333 🎃) [до 800]", callback_data="buy_case_sosiska")
    b.button(text="🕸️ Паутина (150 🎃) [до 400]", callback_data="buy_case_pautina")
    b.button(text="🎃 Тыква (666 🎃) [до 1500]", callback_data="buy_case_tykva")
    b.button(text="👻 Страшный мисс (777 🎃) [до 1900]", callback_data="buy_case_strashny") # НОВЫЙ КЕЙС
    b.button(text="🔙 Назад", callback_data="games_menu")
    b.adjust(1)
    return b.as_markup()

def rocket_bet_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="10 🎃", callback_data="rocket_bet_10")
    b.button(text="50 🎃", callback_data="rocket_bet_50")
    b.button(text="100 🎃", callback_data="rocket_bet_100")
    b.button(text="✍️ Своя", callback_data="rocket_bet_custom")
    b.button(text="🔙 Назад", callback_data="games_menu")
    b.adjust(2)
    return b.as_markup()

def rocket_cashout_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💰 ЗАБРАТЬ", callback_data="rocket_cashout")
    b.adjust(1)
    return b.as_markup()

def upgrader_choice_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⚡ Быстро (35%, x2)", callback_data="upgrade_fast")
    b.button(text="🐢 Медленно (60%, x1.5)", callback_data="upgrade_slow")
    b.button(text="🔙 Назад", callback_data="games_menu")
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
        b.button(text=f"{data['emoji']} {data['name']} - {data['price']}🎃", callback_data=f"buy_{item_id}")
    b.button(text="🔙 Назад", callback_data="balance_menu")
    b.adjust(1)
    return b.as_markup()

def buy_stars_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="100 🎃 (1000 ⭐)", callback_data="buy_stars_100")
    b.button(text="250 🎃 (2500 ⭐)", callback_data="buy_stars_250")
    b.button(text="500 🎃 (5000 ⭐)", callback_data="buy_stars_500")
    b.button(text="✍️ Своя сумма", callback_data="buy_stars_custom")
    b.button(text="🔙 Назад", callback_data="balance_menu")
    b.adjust(1)
    return b.as_markup()

def quests_kb(user_id: int, active_quests: list, completed: list) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for qid in active_quests:
        q = QUEST_POOL[qid]
        if qid in completed: b.button(text=f"✅ {q['name']}", callback_data="ignore")
        else: b.button(text=f"🎁 {q['name']} (+{q['reward']})", callback_data=f"quest_{qid}")
    b.button(text="🔙 Назад", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def proof_kb(quest_key: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"📩 Написать @{ADMIN_USERNAME}", url=f"https://t.me/{ADMIN_USERNAME}")
    b.button(text="✅ Я отправил", callback_data=f"proof_sent_{quest_key}")
    b.button(text="🔙 Назад", callback_data="quest_menu")
    b.adjust(1)
    return b.as_markup()

def admin_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🪄 Выдать", callback_data="admin_give")
    b.button(text="🪓 Отобрать", callback_data="admin_take")
    b.button(text="🪄 Начислить всем", callback_data="admin_give_all")
    b.button(text="🪓 Отобрать у всех", callback_data="admin_take_all")
    b.button(text="🎁 Подарки", callback_data="admin_gift_all")
    b.button(text="👥 Всего юзеров", callback_data="admin_users_count")
    b.button(text="👁 Баланс юзера", callback_data="admin_check_bal")
    b.button(text="📊 Балансы всех", callback_data="admin_all_bals")
    b.button(text="📜 Заявки", callback_data="admin_pending")
    b.button(text="👑 Выдать админку", callback_data="admin_grant")
    b.button(text="🔙 В меню", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def admin_gift_choice_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for gid in GIFT_IDS: b.button(text=f"🎁 Подарок {gid}", callback_data=f"send_gift_{gid}")
    b.button(text="🔙 Назад", callback_data="admin_panel")
    b.adjust(1)
    return b.as_markup()

def cancel_kb(target: str = "admin_panel") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=target)
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
    await update_balance(message.from_user.id, message.from_user.username or message.from_user.full_name, 0)
    admin = await is_admin_db(message.from_user.id)
    await message.answer(
        f"🦇 Приветствую, {message.from_user.first_name}! 🕸️\n\nТьма сгущается... 🎃\nЧто выберешь?",
        reply_markup=main_menu_kb(admin)
    )

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    admin = await is_admin_db(cb.from_user.id)
    await cb.message.edit_text("🔮 Главное меню:", reply_markup=main_menu_kb(admin))
    await cb.answer()

@dp.callback_query(F.data == "ignore")
async def ignore_cb(cb: types.CallbackQuery): await cb.answer("Недоступно.", show_alert=True)

@dp.callback_query(F.data == "show_my_id")
async def show_my_id(cb: types.CallbackQuery):
    admin = await is_admin_db(cb.from_user.id)
    await cb.message.edit_text(f"🆔 ID: `{cb.from_user.id}`", reply_markup=main_menu_kb(admin), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "support_menu")
async def show_support(cb: types.CallbackQuery):
    await cb.message.edit_text("🆘 Поддержка:\n👻 @sardelka\n👾 @mehura", reply_markup=support_kb())
    await cb.answer()

@dp.callback_query(F.data == "balance_menu")
async def show_balance_menu(cb: types.CallbackQuery):
    await cb.message.edit_text("💰 **Всё о тыквах:**", reply_markup=balance_menu_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "show_balance")
async def show_balance(cb: types.CallbackQuery):
    info = await get_user_info(cb.from_user.id)
    bal = info['balance'] if info else 0
    await cb.message.edit_text(f"🎃 Твой котёл: **{bal}** тыкв.", reply_markup=balance_menu_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "show_profile")
async def show_profile(cb: types.CallbackQuery):
    info = await get_user_info(cb.from_user.id)
    if not info: return await cb.answer("Ошибка")
    uname, bal, spent, reg = info['username'], info['balance'], info['spent'], info['registered_at']
    level = (spent // 500) + 1
    title = ["🥚 Новичок", "🎃 Любитель", "🦇 Охотник", "💀 Мастер", "👑 Король Тьмы"][min(level-1, 4)]
    reg_str = reg.strftime("%d.%m.%Y") if reg else "Неизвестно"
    await cb.message.edit_text(
        f"📊 **Профиль**\n\n👤 @{uname or 'скрыт'}\n🆔 `{cb.from_user.id}`\n📅 С нами с: {reg_str}\n\n"
        f"🎃 Баланс: **{bal}**\n💸 Потрачено: **{spent}**\n📈 Уровень: **{level}** ({title})",
        reply_markup=main_menu_kb(await is_admin_db(cb.from_user.id)), parse_mode="Markdown"
    )
    await cb.answer()

@dp.callback_query(F.data == "show_top")
async def show_top(cb: types.CallbackQuery):
    rows = await get_top(10)
    text = "🏆 **Зал славы**\n\n"
    medals = ["🥇", "🥈", "🥉"] + ["💀"] * 10
    for i, row in enumerate(rows):
        name = f"@{row['username']}" if row['username'] else f"id {row['user_id']}"
        text += f"{medals[i]} {name} — {row['balance']} 🎃\n"
    await cb.message.edit_text(text, reply_markup=main_menu_kb(await is_admin_db(cb.from_user.id)), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "daily_bonus")
async def claim_daily_bonus(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    now = datetime.now()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT last_claim, streak FROM user_bonuses WHERE user_id = $1", user_id)
    
    streak = 0
    if row:
        last_claim = row['last_claim']
        streak = row['streak'] or 0
        time_diff = now - last_claim
        if time_diff < timedelta(hours=24):
            rem = timedelta(hours=24) - time_diff
            h, r = divmod(rem.seconds, 3600)
            m, _ = divmod(r, 60)
            return await cb.answer(f"⏳ Жди {h} ч. {m} мин.", show_alert=True)
        if time_diff > timedelta(hours=48): streak = 0
            
    streak += 1
    reward = 25 if streak == 1 else 30 if streak == 2 else 40 if streak == 3 else 50 if streak == 4 else 75 if streak == 5 else 100
    new_bal = await update_balance(user_id, "", reward)
    
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO user_bonuses (user_id, last_claim, streak) VALUES ($1, $2, $3) ON CONFLICT (user_id) DO UPDATE SET last_claim = $2, streak = $3", user_id, now, streak)
        
    await cb.message.edit_text(f"🎁 **Дар!**\n🔥 Серия: **{streak}** дней!\n💰 Награда: **{reward}** 🎃\n\nБаланс: **{new_bal}**.", reply_markup=main_menu_kb(await is_admin_db(user_id)), parse_mode="Markdown")
    await cb.answer("Получено!")

@dp.callback_query(F.data == "games_menu")
async def show_games(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if cb.from_user.id in active_games: return await cb.answer("Ты уже в игре!", show_alert=True)
    await cb.message.edit_text("🎮 **Игровой зал**", reply_markup=games_menu_kb())
    await cb.answer()

# --- КЕЙСЫ ЗА ТЫКВЫ ---
@dp.callback_query(F.data == "case_menu")
async def show_cases(cb: types.CallbackQuery):
    await cb.message.edit_text(
        "🎁 **Кейсы за тыквы**\n\n"
        "Выбери свой трофей. Лимит: **10 кейсов** каждого типа.\n"
        "Кулдаун между покупками: **5 секунд**.",
        reply_markup=cases_menu_kb(),
        parse_mode="Markdown"
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("buy_case_"))
async def buy_case(cb: types.CallbackQuery):
    case_id = cb.data.replace("buy_case_", "")
    
    # 1. Проверка кулдауна (5 сек)
    now = datetime.now()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT last_time FROM user_case_cd WHERE user_id = $1", cb.from_user.id)
    if row and (now - row['last_time']).total_seconds() < 5:
        rem = 5 - int((now - row['last_time']).total_seconds())
        return await cb.answer(f"⏳ Подожди {rem} сек.", show_alert=True)
        
    # 2. Проверка лимита (10 шт)
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT count FROM user_purchases WHERE user_id = $1 AND item_id = $2", cb.from_user.id, case_id)
    if (count or 0) >= 10:
        return await cb.answer("❌ Лимит 10 кейсов исчерпан!", show_alert=True)
        
    # 3. Данные кейсов
    cases = {
        "pautina": {"price": 150, "name": "Паутина", "max": 400},
        "sosiska": {"price": 333, "name": "Сосиска", "max": 800},
        "tykva": {"price": 666, "name": "Тыква", "max": 1500},
        "strashny": {"price": 777, "name": "Страшный мисс", "max": 1900}
    }
    c_data = cases[case_id]
    
    # 4. Проверка баланса
    info = await get_user_info(cb.from_user.id)
    if info['balance'] < c_data["price"]:
        return await cb.answer(f"❌ Недостаточно тыкв! Нужно {c_data['price']} 🎃.", show_alert=True)
        
    # 5. Списание стоимости и обновление БД (сразу, чтобы не было дыр)
    await update_balance(cb.from_user.id, "", -c_data["price"], is_spending=True)
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO user_case_cd (user_id, last_time) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET last_time = $2", cb.from_user.id, now)
        await conn.execute("INSERT INTO user_purchases (user_id, item_id, count) VALUES ($1, $2, 1) ON CONFLICT (user_id, item_id) DO UPDATE SET count = count + 1", cb.from_user.id, case_id)
        
    # 6. Анимация открытия (уведомление)
    await cb.answer(f"🎰 Крутим барабаны...", show_alert=False)
    try:
        await cb.message.edit_text(
            f"🎁 **Кейс «{c_data['name']}» открывается...**\n\n"
            f"💰 Списано: {c_data['price']} 🎃\n"
            f"🎰 Ждём результат...",
            reply_markup=None
        )
    except: pass
    
    await asyncio.sleep(1.5) # Ждем 1.5 секунды для эффекта
    
    # 7. Генерация награды
    r = random.random()
    if case_id == "pautina":
        if r < 0.5: reward = random.randint(10, 50)
        elif r < 0.8: reward = random.randint(51, 150)
        elif r < 0.95: reward = random.randint(151, 250)
        elif r < 0.995: reward = random.randint(251, 350)
        else: reward = random.randint(351, 400)
    elif case_id == "sosiska":
        if r < 0.5: reward = random.randint(50, 150)
        elif r < 0.8: reward = random.randint(151, 300)
        elif r < 0.95: reward = random.randint(301, 500)
        elif r < 0.995: reward = random.randint(501, 650)
        else: reward = random.randint(651, 800)
    elif case_id == "tykva":
        if r < 0.5: reward = random.randint(100, 300)
        elif r < 0.8: reward = random.randint(301, 600)
        elif r < 0.95: reward = random.randint(601, 900)
        elif r < 0.995: reward = random.randint(901, 1200)
        else: reward = random.randint(1201, 1500)
    elif case_id == "strashny":
        if r < 0.5: reward = random.randint(150, 400)
        elif r < 0.8: reward = random.randint(401, 800)
        elif r < 0.95: reward = random.randint(801, 1200)
        elif r < 0.995: reward = random.randint(1201, 1500)
        else: reward = random.randint(1501, 1900)
            
    # 8. Начисление награды
    new_bal = await update_balance(cb.from_user.id, "", reward)
    
    # 9. Финальный ответ (новым сообщением, чтобы избежать ошибок edit_text)
    try:
        await cb.message.edit_text(
            f"🎁 **Кейс «{c_data['name']}» открыт!**\n\n"
            f"Тебе выпало: **{reward}** 🎃!\n\n"
            f"🎃 Твой баланс: **{new_bal}**.\n"
            f"Куплено кейсов: `{(count or 0) + 1}/10`",
            reply_markup=games_menu_kb(), 
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Ошибка при отправке результата кейса: {e}")
        await bot.send_message(cb.from_user.id, f"🎁 **Кейс открыт!**\nВыпало: **{reward}** 🎃\nБаланс: **{new_bal}**.", reply_markup=games_menu_kb())

# --- ОСТАЛЬНЫЕ ИГРЫ ---
@dp.callback_query(F.data == "game_wheel")
async def wheel_of_fortune(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    now = datetime.now()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT last_played FROM user_wheel WHERE user_id = $1", user_id)
        
    if row:
        time_diff = now - row['last_played']
        if time_diff < timedelta(hours=6):
            rem = timedelta(hours=6) - time_diff
            h, r = divmod(rem.seconds, 3600)
            m, _ = divmod(r, 60)
            return await cb.answer(f"🎡 Колесо остывает. Жди {h} ч. {m} мин.", show_alert=True)
            
    info = await get_user_info(user_id)
    if info['balance'] < 20: return await cb.answer("❌ Нужно 20 тыкв!", show_alert=True)
        
    await update_balance(user_id, "", -20, is_spending=True)
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO user_wheel (user_id, last_played) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET last_played = $2", user_id, now)
        
    rand = random.random()
    prize = 100 if rand < 0.05 else 50 if rand < 0.20 else 20 if rand < 0.50 else 0
    if prize > 0:
        new_bal = await update_balance(user_id, "", prize)
        text = f"🎡 **Крутится...**\n\n🎉 Выпало: **{prize}** 🎃!\nБаланс: **{new_bal}**."
    else:
        info = await get_user_info(user_id)
        text = f"🎡 **Крутится...**\n\n💀 Проигрыш!\nБаланс: **{info['balance']}**."
        
    await cb.message.edit_text(text, reply_markup=games_menu_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "game_trickortreat")
async def trick_or_treat(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    now = datetime.now()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT last_played FROM user_trickortreat WHERE user_id = $1", user_id)
        
    if row:
        time_diff = now - row['last_played']
        if time_diff < timedelta(hours=24):
            rem = timedelta(hours=24) - time_diff
            h, r = divmod(rem.seconds, 3600)
            m, _ = divmod(r, 60)
            return await cb.answer(f"🎃 Тьма отдыхает. Жди {h} ч. {m} мин.", show_alert=True)
            
    info = await get_user_info(user_id)
    if info['balance'] < 25: return await cb.answer("❌ Нужно 25 тыкв!", show_alert=True)
        
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO user_trickortreat (user_id, last_played) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET last_played = $2", user_id, now)
        
    if random.random() < 0.5:
        new_bal = await update_balance(user_id, "", 25)
        text = f"🍬 **Сладость!**\n+25 🎃\nБаланс: **{new_bal}**."
    else:
        new_bal = await update_balance(user_id, "", -25, is_spending=True)
        text = f"🧟 **Гадость!**\n-25 🎃\nБаланс: **{new_bal}**."
    await cb.message.edit_text(text, reply_markup=games_menu_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "game_rocket")
async def rocket_menu(cb: types.CallbackQuery):
    await cb.message.edit_text("🚀 **Ракета**\nВыбери ставку:", reply_markup=rocket_bet_kb())
    await cb.answer()

@dp.callback_query(F.data == "rocket_bet_custom")
async def rocket_bet_custom(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(RocketGame.bet)
    await cb.message.edit_text("✍️ Введи сумму:", reply_markup=cancel_kb("games_menu"))
    await cb.answer()

@dp.message(RocketGame.bet)
async def rocket_bet_input(message: types.Message, state: FSMContext):
    try:
        bet = int(message.text.strip())
        if bet <= 0: raise ValueError
    except:
        return await message.answer("❌ Введи число > 0.")
    await state.clear()
    await start_rocket(message.from_user.id, bet, message)

@dp.callback_query(F.data.startswith("rocket_bet_") & ~F.data.startswith("rocket_bet_custom"))
async def rocket_bet_preset(cb: types.CallbackQuery):
    bet = int(cb.data.replace("rocket_bet_", ""))
    await start_rocket(cb.from_user.id, bet, cb.message, cb)

async def start_rocket(user_id: int, bet: int, message: types.Message, cb: types.CallbackQuery = None):
    if user_id in active_games: return await (cb.answer("Уже в игре!", show_alert=True) if cb else message.answer("Уже в игре!"))
    info = await get_user_info(user_id)
    if info['balance'] < bet: return await (cb.answer("Мало тыкв!", show_alert=True) if cb else message.answer("Мало тыкв!"))
    
    await update_balance(user_id, "", -bet, is_spending=True)
    crash = round(random.uniform(1.2, 5.0), 1)
    text = f"🚀 **Взлет!**\nМножитель: **x1.0**\nСтавка: {bet} 🎃"
    
    if cb:
        await cb.message.edit_text(text, reply_markup=rocket_cashout_kb(), parse_mode="Markdown")
        msg = cb.message
    else:
        msg = await message.answer(text, reply_markup=rocket_cashout_kb(), parse_mode="Markdown")
        
    task = asyncio.create_task(rocket_loop(user_id, bet, crash, msg))
    active_games[user_id] = {"bet": bet, "mult": 1.0, "crash": crash, "task": task, "msg": msg}

async def rocket_loop(user_id: int, bet: int, crash: float, msg: types.Message):
    mult = 1.0
    try:
        while True:
            await asyncio.sleep(1)
            if user_id not in active_games: return
            mult += 0.2
            if mult >= crash:
                del active_games[user_id]
                await bot.edit_message_text(chat_id=user_id, message_id=msg.message_id, text=f"💥 **БУМ!**\nМножитель x{crash:.1f}\nПотеряно: {bet} 🎃", reply_markup=None)
                return
            
            active_games[user_id]["mult"] = mult
            await bot.edit_message_text(
                chat_id=user_id, message_id=msg.message_id,
                text=f"🚀 **Летит!**\nМножитель: **x{mult:.1f}**\nЗабрать: **{int(bet*mult)}** 🎃",
                reply_markup=rocket_cashout_kb(), parse_mode="Markdown"
            )
    except asyncio.CancelledError: pass
    except Exception: 
        if user_id in active_games: del active_games[user_id]

@dp.callback_query(F.data == "rocket_cashout")
async def rocket_cashout(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    if user_id not in active_games: return await cb.answer("Игра окончена!", show_alert=True)
    
    game = active_games.pop(user_id)
    game["task"].cancel()
    win = int(game["bet"] * game["mult"])
    new_bal = await update_balance(user_id, "", win)
    await cb.message.edit_text(f"💰 **Забрано!**\nМножитель: x{game['mult']:.1f}\nВыигрыш: **{win}** 🎃\nБаланс: **{new_bal}**.", reply_markup=games_menu_kb(), parse_mode="Markdown")
    await cb.answer("Успел!")

@dp.callback_query(F.data == "game_upgrader")
async def upgrader_start(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(Upgrader.amount)
    await cb.message.edit_text("📈 **Апгрейдер**\nВведи сумму:", reply_markup=cancel_kb("games_menu"))
    await cb.answer()

@dp.message(Upgrader.amount)
async def upgrader_input(message: types.Message, state: FSMContext):
    try:
        bet = int(message.text.strip())
        if bet <= 0: raise ValueError
    except:
        return await message.answer("❌ Введи число > 0.")
    
    info = await get_user_info(message.from_user.id)
    if info['balance'] < bet: return await message.answer("❌ Мало тыкв!")
    
    await state.update_data(bet=bet)
    await message.answer(f"📈 Ставка: {bet} 🎃\nВыбери режим:", reply_markup=upgrader_choice_kb())

@dp.callback_query(F.data == "upgrade_fast")
async def upgrade_fast(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    bet = data.get("bet")
    if not bet: return await cb.answer("Устарело.", show_alert=True)
    await state.clear()
    
    await update_balance(cb.from_user.id, "", -bet, is_spending=True)
    win = bet * 2 if random.random() < 0.35 else 0
    new_bal = await update_balance(cb.from_user.id, "", win)
    
    text = f"🎉 Выигрыш: **+{win}** 🎃 (x2)\nБаланс: **{new_bal}**." if win else f"💀 Проигрыш: **-{bet}** 🎃\nБаланс: **{new_bal}**."
    b = InlineKeyboardBuilder().button(text="🔄 Ещё", callback_data="game_upgrader").button(text="🔙 В игры", callback_data="games_menu").adjust(1)
    await cb.message.edit_text(text, reply_markup=b.as_markup(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "upgrade_slow")
async def upgrade_slow(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    bet = data.get("bet")
    if not bet: return await cb.answer("Устарело.", show_alert=True)
    await state.clear()
    
    await update_balance(cb.from_user.id, "", -bet, is_spending=True)
    win = int(bet * 1.5) if random.random() < 0.6 else 0
    new_bal = await update_balance(cb.from_user.id, "", win)
    
    text = f"🎉 Выигрыш: **+{win}** 🎃 (x1.5)\nБаланс: **{new_bal}**." if win else f"💀 Проигрыш: **-{bet}** 🎃\nБаланс: **{new_bal}**."
    b = InlineKeyboardBuilder().button(text="🔄 Ещё", callback_data="game_upgrader").button(text="🔙 В игры", callback_data="games_menu").adjust(1)
    await cb.message.edit_text(text, reply_markup=b.as_markup(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "transfer_start")
async def transfer_start(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(TransferPumpkins.target)
    await cb.message.edit_text("💸 Введи **ID** или **@username** получателя:", reply_markup=cancel_kb("balance_menu"), parse_mode="Markdown")
    await cb.answer()

@dp.message(TransferPumpkins.target)
async def transfer_target(message: types.Message, state: FSMContext):
    target = await find_user_id(message.text)
    if not target: return await message.answer("❌ Не найден.")
    if target == message.from_user.id: return await message.answer("❌ Себе нельзя.")
    await state.update_data(target=target)
    await state.set_state(TransferPumpkins.amount)
    await message.answer(f"Сколько перевести `{target}`?", reply_markup=cancel_kb("balance_menu"), parse_mode="Markdown")

@dp.message(TransferPumpkins.amount)
async def transfer_amount(message: types.Message, state: FSMContext):
    try: amt = int(message.text.strip())
    except: return await message.answer("Число нужно.")
    if amt <= 0: return await message.answer("Больше 0.")
    
    data = await state.get_data()
    target = data["target"]
    await state.clear()
    
    info = await get_user_info(message.from_user.id)
    if info['balance'] < amt: return await message.answer("Мало тыкв!")
    
    await update_balance(message.from_user.id, "", -amt, is_spending=True)
    new_bal = await update_balance(target, "", amt)
    
    admin = await is_admin_db(message.from_user.id)
    await message.answer(f"✅ Переведено **{amt}** 🎃 пользователю `{target}`.", reply_markup=main_menu_kb(admin), parse_mode="Markdown")
    try: await bot.send_message(target, f"🎁 Тебе перевели **{amt}** 🎃!")
    except: pass

@dp.callback_query(F.data == "shop_menu")
async def show_shop(cb: types.CallbackQuery):
    await cb.message.edit_text("🛒 **Магазин**", reply_markup=shop_kb(cb.from_user.id))
    await cb.answer()

@dp.callback_query(F.data.startswith("buy_item_"))
async def buy_item(cb: types.CallbackQuery):
    item_id = cb.data.replace("buy_", "")
    item = SHOP_ITEMS.get(item_id)
    if not item: return await cb.answer("Нет товара.", show_alert=True)
        
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT count FROM user_purchases WHERE user_id = $1 AND item_id = $2", cb.from_user.id, item_id)
    bought = row['count'] if row else 0
    if bought >= item["limit"]: return await cb.answer("Лимит исчерпан!", show_alert=True)
        
    info = await get_user_info(cb.from_user.id)
    if info['balance'] < item["price"]: return await cb.answer("Мало тыкв!", show_alert=True)
        
    await update_balance(cb.from_user.id, "", -item["price"], is_spending=True)
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO user_purchases (user_id, item_id, count) VALUES ($1, $2, 1) ON CONFLICT (user_id, item_id) DO UPDATE SET count = count + 1", cb.from_user.id, item_id)
        
    admin = await is_admin_db(cb.from_user.id)
    await cb.message.edit_text(f"✅ Куплено **{item['name']}**!", reply_markup=main_menu_kb(admin), parse_mode="Markdown")
    await notify_admins(f"🛒 `{cb.from_user.id}` купил **{item['name']}**.")

@dp.callback_query(F.data == "buy_stars_menu")
async def buy_stars_menu(cb: types.CallbackQuery):
    await cb.message.edit_text("💎 **Звезды** (1🎃=10⭐)\nМин: 100🎃", reply_markup=buy_stars_kb())
    await cb.answer()

@dp.callback_query(F.data == "buy_stars_custom")
async def buy_stars_custom(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(BuyStars.custom_amount)
    await cb.message.edit_text("✍️ Введи кол-во тыкв (>100):", reply_markup=cancel_kb("balance_menu"))
    await cb.answer()

@dp.message(BuyStars.custom_amount)
async def buy_stars_amt(message: types.Message, state: FSMContext):
    try: p = int(message.text.strip())
    except: return await message.answer("Число.")
    if p < 100: return await message.answer("Мин 100.")
    await state.clear()
    await message.answer_invoice(title="Тыквы", description=f"{p} тыкв", prices=[LabeledPrice(label="XTR", amount=p*10)], provider_token="", currency="XTR", payload=f"stars_{p}")

@dp.callback_query(F.data.startswith("buy_stars_") & ~F.data.startswith("buy_stars_custom") & ~F.data.startswith("buy_stars_menu"))
async def buy_stars_preset(cb: types.CallbackQuery):
    p = int(cb.data.replace("buy_stars_", ""))
    await cb.message.answer_invoice(title="Тыквы", description=f"{p} тыкв", prices=[LabeledPrice(label="XTR", amount=p*10)], provider_token="", currency="XTR", payload=f"stars_{p}")
    await cb.answer()

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery): await q.answer(ok=True)

@dp.message(F.successful_payment)
async def success_pay(message: types.Message):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("stars_"):
        p = int(payload.split("_")[1])
        await update_balance(message.from_user.id, "", p)
        admin = await is_admin_db(message.from_user.id)
        await message.answer(f"🎉 +{p} 🎃", reply_markup=main_menu_kb(admin))

@dp.callback_query(F.data == "quest_menu")
async def show_quests(cb: types.CallbackQuery):
    active = await check_and_rotate_quests()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT quest_id FROM user_quest_completions WHERE user_id = $1 AND completed_at > (SELECT value::timestamp FROM settings WHERE key = 'last_rotation')", cb.from_user.id)
    completed = [r['quest_id'] for r in rows]
    await cb.message.edit_text("🕸️ **Квесты** (обнов. каждые 12ч)", reply_markup=quests_kb(cb.from_user.id, active, completed))
    await cb.answer()

@dp.callback_query(F.data.startswith("quest_") & ~F.data.startswith("quest_menu"))
async def handle_quest(cb: types.CallbackQuery):
    qk = cb.data.replace("quest_", "")
    q = QUEST_POOL.get(qk)
    if not q: return await cb.answer("Устарел.", show_alert=True)
    await cb.message.edit_text(f"🕷️ **{q['name']}**\n{q['desc']}\n💰 {q['reward']} 🎃", reply_markup=proof_kb(qk))
    await cb.answer()

@dp.callback_query(F.data.startswith("proof_sent_"))
async def proof_sent(cb: types.CallbackQuery):
    qk = cb.data.replace("proof_sent_", "")
    q = QUEST_POOL.get(qk)
    await save_proof(cb.from_user.id, q["name"])
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO user_quest_completions (user_id, quest_id) VALUES ($1, $2)", cb.from_user.id, qk)
    await cb.message.edit_text("✅ Заявка отправлена на проверку!")
    await cb.answer()

@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    await cb.message.edit_text("💀 **Панель Мастера**", reply_markup=admin_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_users_count")
async def admin_users_count(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет доступа.", show_alert=True)
    count = await get_total_users_count()
    await cb.message.edit_text(
        f"👥 **Статистика бота**\n\nВсего зарегистрировано пользователей: **{count}**", 
        reply_markup=admin_kb(), 
        parse_mode="Markdown"
    )
    await cb.answer()

@dp.callback_query(F.data == "admin_pending")
async def admin_pending(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    rows = await get_pending(20)
    text = "📜 **Заявки:**\n\n" + "\n".join([f"#{r['id']} · user={r['user_id']} · {r['quest_name']}" for r in rows]) if rows else "Нет заявок."
    await cb.message.edit_text(text, reply_markup=admin_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_all_bals")
async def admin_all_bals(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, username, balance FROM users ORDER BY balance DESC LIMIT 50")
    text = "📊 **Балансы:**\n\n" + "\n".join([f"• @{r['username'] or r['user_id']}: **{r['balance']}** 🎃" for r in rows])
    await cb.message.edit_text(text, reply_markup=admin_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "admin_check_bal")
async def admin_check_bal(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    await state.set_state(AdminViewBalance.target)
    await cb.message.edit_text("👁 Введи ID/username:", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(AdminViewBalance.target)
async def admin_check_bal_input(message: types.Message, state: FSMContext):
    t = await find_user_id(message.text)
    if not t: return await message.answer("Не найден.")
    await state.clear()
    info = await get_user_info(t)
    await message.answer(f"👁 Баланс `{t}`: **{info['balance'] if info else 0}** 🎃", reply_markup=admin_kb(), parse_mode="Markdown")

@dp.callback_query(F.data == "admin_grant")
async def admin_grant(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    await state.set_state(GrantAdmin.target_id)
    await cb.message.edit_text("👑 ID/username нового админа:", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(GrantAdmin.target_id)
async def admin_grant_input(message: types.Message, state: FSMContext):
    t = await find_user_id(message.text)
    if not t: return await message.answer("Не найден.")
    await state.clear()
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING", t)
    await message.answer(f"✅ `{t}` теперь Мастер!", reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_give")
async def admin_give(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    await state.update_data(action="give")
    await state.set_state(AdminAmount.target_id)
    await cb.message.edit_text("🪄 Кому (ID/username):", reply_markup=cancel_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_take")
async def admin_take(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    await state.update_data(action="take")
    await state.set_state(AdminAmount.target_id)
    await cb.message.edit_text("🪓 У кого (ID/username):", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(AdminAmount.target_id)
async def admin_target_input(message: types.Message, state: FSMContext):
    t = await find_user_id(message.text)
    if not t: return await message.answer("Не найден.")
    await state.update_data(target=t)
    await state.set_state(AdminAmount.amount)
    await message.answer("Сколько тыкв?", reply_markup=cancel_kb())

@dp.message(AdminAmount.amount)
async def admin_amt_input(message: types.Message, state: FSMContext):
    try: a = abs(int(message.text.strip()))
    except: return await message.answer("Число.")
    d = await state.get_data()
    t, act = d["target"], d["action"]
    await state.clear()
    final = a if act == "give" else -a
    new = await update_balance(t, "", final)
    await message.answer(f"✅ Готово. Баланс: {new} 🎃", reply_markup=admin_kb())
    try: await bot.send_message(t, f"Баланс изменен на {final} 🎃.")
    except: pass

@dp.callback_query(F.data == "admin_give_all")
async def admin_give_all(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    await state.set_state(AdminGiveAll.amount)
    await cb.message.edit_text("🪄 Сколько начислить ВСЕМ?", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(AdminGiveAll.amount)
async def admin_give_all_input(message: types.Message, state: FSMContext):
    try: a = int(message.text.strip())
    except: return await message.answer("Число.")
    if a <= 0: return await message.answer(">0.")
    await state.clear()
    users = await get_all_users()
    await message.answer(f"🚀 Начисляю {a} тыкв для {len(users)}...")
    for u in users:
        await update_balance(u, "", a)
        try: await bot.send_message(u, f"🎁 Дар! +{a} 🎃")
        except: pass
        await asyncio.sleep(0.05)
    await message.answer("✅ Готово!", reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_take_all")
async def admin_take_all(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    await state.set_state(AdminTakeAll.amount)
    await cb.message.edit_text("🪓 Сколько отобрать У ВСЕХ?", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(AdminTakeAll.amount)
async def admin_take_all_input(message: types.Message, state: FSMContext):
    try: a = abs(int(message.text.strip()))
    except: return await message.answer("Число.")
    await state.clear()
    users = await get_all_users()
    await message.answer(f"🚀 Списываю {a} тыкв у {len(users)}...")
    for u in users:
        await update_balance(u, "", -a, is_spending=True)
        try: await bot.send_message(u, f"🪓 Гнев Мастера! -{a} 🎃")
        except: pass
        await asyncio.sleep(0.05)
    await message.answer("✅ Готово!", reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_gift_all")
async def admin_gift_all(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    await cb.message.edit_text("🎁 Выбери подарок:", reply_markup=admin_gift_choice_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("send_gift_"))
async def send_gift(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    gid = cb.data.replace("send_gift_", "")
    users = await get_all_users()
    await cb.message.edit_text(f"🚀 Рассылка подарка `{gid}` для {len(users)}...")
    s, f = 0, 0
    for u in users:
        try:
            await bot.send_gift(user_id=u, gift_id=gid)
            s += 1
        except: f += 1
        await asyncio.sleep(0.5)
    await cb.message.answer(f"✅ Отправлено: {s}\nОшибок: {f}", reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_cancel")
async def admin_cancel(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("💀 Панель:", reply_markup=admin_kb())
    await cb.answer("Отменено")

@dp.message(F.text.startswith("/approve "))
async def cmd_approve(message: types.Message):
    if not await is_admin_db(message.from_user.id): return
    pid = int(message.text.split()[1])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id, quest_name FROM quest_proofs WHERE id = $1", pid)
        if not row: return await message.answer("Не найдено.")
        reward = next((q["reward"] for q in QUEST_POOL.values() if q["name"] == row['quest_name']), 0)
        await conn.execute("UPDATE quest_proofs SET status='approved' WHERE id=$1", pid)
    await update_balance(row['user_id'], "", reward)
    await message.answer(f"✅ Начислено {reward}.")
    try: await bot.send_message(row['user_id'], f"Квест {row['quest_name']} подтвержден! +{reward} 🎃.")
    except: pass

@dp.message(F.text.startswith("/reject "))
async def cmd_reject(message: types.Message):
    if not await is_admin_db(message.from_user.id): return
    pid = int(message.text.split()[1])
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE quest_proofs SET status='rejected' WHERE id=$1", pid)
    await message.answer("Заявка отклонена.")

# --- СЕРВЕР ---
async def healthcheck(request): return web.Response(text="OK")

async def main():
    if not TOKEN: raise RuntimeError("BOT_TOKEN не задан")
    await init_db()
    
    app = web.Application()
    app.router.add_get("/healthcheck", healthcheck)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
    await site.start()
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
