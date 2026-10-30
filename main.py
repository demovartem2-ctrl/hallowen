import asyncio
import logging
import os
import random
from datetime import datetime, timedelta
from typing import Optional

import asyncpg
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, LabeledPrice, PreCheckoutQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web

TOKEN = os.environ.get("BOT_TOKEN", "")
API_BASE = os.environ.get("API_BASE", "https://misugram.cloud-ip.cc")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL: raise RuntimeError("DATABASE_URL не задан!")

ADMIN_USERNAME = "sardelka"
DEFAULT_ADMINS = {1780243203, 1780243210}
GIFT_IDS = ["9000000000000159", "9000000000000158", "9000000000000153", "9000000000000152", "9000000000000148"]

db_pool: asyncpg.Pool = None
active_games = {}

# --- БАЗА ---
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
        await conn.execute("CREATE TABLE IF NOT EXISTS promocodes (code TEXT PRIMARY KEY, reward BIGINT, limit_uses INTEGER, current_uses INTEGER DEFAULT 0)")
        await conn.execute("CREATE TABLE IF NOT EXISTS user_promo_uses (user_id BIGINT, code TEXT, PRIMARY KEY (user_id, code))")
        for aid in DEFAULT_ADMINS:
            await conn.execute("INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING", aid)

async def wipe_database():
    async with db_pool.acquire() as conn:
        await conn.execute("TRUNCATE TABLE users, quest_proofs, user_purchases, settings, user_quest_completions, user_bonuses, user_trickortreat, user_wheel, promocodes, user_promo_uses RESTART IDENTITY CASCADE")
        await conn.execute("TRUNCATE TABLE admins CASCADE")
        for aid in DEFAULT_ADMINS:
            await conn.execute("INSERT INTO admins (user_id) VALUES ($1)", aid)

async def get_user_info(uid: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT username, balance, spent, registered_at FROM users WHERE user_id = $1", uid)

async def update_balance(uid: int, uname: str, amt: int, is_spend: bool = False) -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance, spent FROM users WHERE user_id = $1", uid)
        if row:
            nb = max(0, row['balance'] + amt)
            diff = nb - row['balance']
            ns = max(0, row['spent'] + (abs(diff) if is_spend and diff < 0 else 0))
            await conn.execute("UPDATE users SET balance = $1, spent = $2, username = COALESCE($3, username) WHERE user_id = $4", nb, ns, uname, uid)
            return nb
        nb = max(0, amt)
        ns = abs(nb) if is_spend and nb < 0 else 0
        await conn.execute("INSERT INTO users (user_id, username, balance, spent) VALUES ($1, $2, $3, $4)", uid, uname, nb, ns)
        return nb

async def admin_take_level(uid: int, amt: int) -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT spent FROM users WHERE user_id = $1", uid)
        if not row: return 0
        ns = max(0, row['spent'] - amt)
        await conn.execute("UPDATE users SET spent = $1 WHERE user_id = $2", ns, uid)
        return ns

def get_level_info(spent: int) -> tuple[int, str]:
    spent = spent or 0
    level = (spent // 500) + 1
    titles = ["🥚 Новичок", "🎃 Любитель", "🦇 Охотник", "💀 Мастер", "👑 Король Тьмы"]
    if level >= 50: title = "👑 Тёмный Лорд"
    elif level <= len(titles): title = titles[level - 1]
    else: title = "💀 Мастер"
    return level, title

async def is_admin_db(uid: int) -> bool:
    if uid in DEFAULT_ADMINS: return True
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM admins WHERE user_id = $1", uid)
    return bool(row)

async def notify_admins(text: str):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM admins")
    for aid in set([r['user_id'] for r in rows]) | DEFAULT_ADMINS:
        try: await bot.send_message(aid, text, parse_mode="Markdown")
        except: pass

async def get_all_users():
    async with db_pool.acquire() as conn:
        return [r['user_id'] for r in await conn.fetch("SELECT user_id FROM users")]

async def get_total_users_count() -> int:
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM users") or 0

async def find_user_id(identifier: str) -> Optional[int]:
    identifier = identifier.strip()
    if identifier.isdigit(): return int(identifier)
    if identifier.startswith("@"):
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT user_id FROM users WHERE LOWER(username) = $1", identifier[1:].lower())
        if row: return row['user_id']
    return None

async def get_top(limit: int = 10):
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT user_id, username, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT $1", limit)

async def get_pending(limit: int = 10):
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT id, user_id, quest_name, created_at FROM quest_proofs WHERE status='pending' ORDER BY id DESC LIMIT $1", limit)

async def save_proof(uid: int, qname: str):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO quest_proofs (user_id, quest_name) VALUES ($1, $2)", uid, qname)

QUEST_POOL = {
    "q1": {"name": "Раздай 5 подарков", "reward": 15, "desc": "Раздай 5 подарков."},
    "q2": {"name": "Фото с тыквой", "reward": 10, "desc": "Сделай фото с тыквой."},
    "q3": {"name": "Купи 3 подарка", "reward": 5, "desc": "Купи 3 подарка себе."},
}

async def check_and_rotate_quests():
    async with db_pool.acquire() as conn:
        last_str = await conn.fetchval("SELECT value FROM settings WHERE key = 'last_rotation'")
        active_str = await conn.fetchval("SELECT value FROM settings WHERE key = 'active_quests'")
        if last_str and active_str and datetime.now() - datetime.fromisoformat(last_str) < timedelta(hours=12):
            return active_str.split(",")
        keys = list(QUEST_POOL.keys())
        random.shuffle(keys)
        active = keys[:3]
        now = datetime.now().isoformat()
        await conn.execute("INSERT INTO settings (key, value) VALUES ('last_rotation', $1) ON CONFLICT (key) DO UPDATE SET value = $1", now)
        await conn.execute("INSERT INTO settings (key, value) VALUES ('active_quests', $1) ON CONFLICT (key) DO UPDATE SET value = $1", ",".join(active))
        return active

# --- СТЕЙТЫ ---
class AdminAmount(StatesGroup): target_id = State(); amount = State()
class AdminTakeLevel(StatesGroup): target_id = State(); amount = State()
class AdminTakeLevelAll(StatesGroup): amount = State()
class GrantAdmin(StatesGroup): target_id = State()
class BuyStars(StatesGroup): custom_amount = State()
class TransferPumpkins(StatesGroup): target = State(); amount = State()
class AdminViewBalance(StatesGroup): target = State()
class AdminViewProfile(StatesGroup): target = State()
class RocketGame(StatesGroup): bet = State()
class MinesGame(StatesGroup): bet = State()
class AdminGiveAll(StatesGroup): amount = State()
class AdminTakeAll(StatesGroup): amount = State()
class Upgrader(StatesGroup): amount = State()
class PromoCreate(StatesGroup): name = State(); reward = State(); limit = State()
class PromoEnter(StatesGroup): code = State()
class Broadcast(StatesGroup): text = State(); confirm = State()

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
    b.button(text="🎟️ Ввести промокод", callback_data="enter_promo")
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
    b.button(text="💣 Мины", callback_data="mines_menu")
    b.button(text="🔙 Назад", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def mines_difficulty_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🟢 1 мина", callback_data="mines_diff_1")
    b.button(text="🟡 3 мины", callback_data="mines_diff_3")
    b.button(text="🟠 5 мин", callback_data="mines_diff_5")
    b.button(text="🔴 10 мин", callback_data="mines_diff_10")
    b.button(text="💀 24 мины", callback_data="mines_diff_24")
    b.button(text="🔙 Назад", callback_data="games_menu")
    b.adjust(1)
    return b.as_markup()

def mines_board_kb(opened: list, multiplier: float) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i in range(25):
        if i in opened: b.button(text="🟩", callback_data="ignore")
        else: b.button(text="⬜️", callback_data=f"mines_open_{i}")
    b.button(text=f"💰 ЗАБРАТЬ (x{multiplier:.2f})", callback_data="mines_cashout")
    b.adjust(5, 5, 5, 5, 5, 1)
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
    b.button(text="🪓 Отобрать лвл", callback_data="admin_take_level")
    b.button(text="🪓 Отобрать лвл у всех", callback_data="admin_take_level_all")
    b.button(text="🪄 Начислить всем", callback_data="admin_give_all")
    b.button(text="🪓 Отобрать у всех", callback_data="admin_take_all")
    b.button(text="✉️ Рассылка всем", callback_data="admin_broadcast")
    b.button(text="🎁 Подарки", callback_data="admin_gift_all")
    b.button(text="🎟️ Создать промокод", callback_data="admin_create_promo")
    b.button(text="👥 Всего юзеров", callback_data="admin_users_count")
    b.button(text="👤 Профиль игрока", callback_data="admin_check_profile")
    b.button(text="👁 Баланс юзера", callback_data="admin_check_bal")
    b.button(text="📜 Заявки", callback_data="admin_pending")
    b.button(text="👑 Выдать админку", callback_data="admin_grant")
    b.button(text="💣 Снести БД", callback_data="admin_wipe_db")
    b.button(text="🔙 В меню", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def admin_gift_choice_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for gid in GIFT_IDS: b.button(text=f"🎁 Подарок {gid}", callback_data=f"send_gift_{gid}")
    b.button(text="🔙 Назад", callback_data="admin_panel")
    b.adjust(1)
    return b.as_markup()

def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Отправить всем", callback_data="broadcast_confirm_yes")
    b.button(text="❌ Отмена", callback_data="admin_cancel")
    b.adjust(1)
    return b.as_markup()

def cancel_kb(target: str = "admin_panel") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=target)
    b.adjust(1)
    return b.as_markup()

logging.basicConfig(level=logging.INFO)
session = AiohttpSession(api=TelegramAPIServer.from_base(API_BASE))
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())
# --- СТАРТ И НАВИГАЦИЯ ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await update_balance(message.from_user.id, message.from_user.username or message.from_user.full_name, 0)
    admin = await is_admin_db(message.from_user.id)
    await message.answer(f"🦇 Приветствую, {message.from_user.first_name}! 🕸️\n\nТьма сгущается... 🎃\nЧто выберешь?", reply_markup=main_menu_kb(admin))

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("🔮 Главное меню:", reply_markup=main_menu_kb(await is_admin_db(cb.from_user.id)))
    await cb.answer()

@dp.callback_query(F.data == "ignore")
async def ignore_cb(cb: types.CallbackQuery): await cb.answer("Недоступно.", show_alert=True)

@dp.callback_query(F.data == "show_my_id")
async def show_my_id(cb: types.CallbackQuery):
    await cb.message.edit_text(f"🆔 ID: `{cb.from_user.id}`", reply_markup=main_menu_kb(await is_admin_db(cb.from_user.id)), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "support_menu")
async def show_support(cb: types.CallbackQuery):
    await cb.message.edit_text("🆘 Поддержка:\n👻 @sardelka\n👾 @mehura", reply_markup=support_kb())
    await cb.answer()

# --- ПРОМОКОДЫ ---
@dp.message(Command("promo"))
async def cmd_promo(message: types.Message, state: FSMContext):
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return await message.answer("❌ Использование: `/promo КОД`", parse_mode="Markdown")
    await process_promo_code(message.from_user.id, args[1].strip().upper(), message, state)

@dp.callback_query(F.data == "enter_promo")
async def enter_promo_cb(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(PromoEnter.code)
    await cb.message.edit_text("🎟️ Введи промокод:", reply_markup=cancel_kb("back_to_main"))
    await cb.answer()

@dp.message(PromoEnter.code)
async def enter_promo_input(message: types.Message, state: FSMContext):
    await process_promo_code(message.from_user.id, message.text.strip().upper(), message, state)

async def process_promo_code(user_id: int, code: str, message: types.Message, state: FSMContext):
    async with db_pool.acquire() as conn:
        promo = await conn.fetchrow("SELECT reward, limit_uses, current_uses FROM promocodes WHERE code = $1", code)
        if not promo:
            await state.clear(); return await message.answer("❌ Такого промокода не существует.")
        if promo['current_uses'] >= promo['limit_uses']:
            await state.clear(); return await message.answer("❌ Этот промокод уже исчерпан.")
        used = await conn.fetchrow("SELECT 1 FROM user_promo_uses WHERE user_id = $1 AND code = $2", user_id, code)
        if used:
            await state.clear(); return await message.answer("❌ Ты уже использовал этот промокод.")
        reward = promo['reward']
        await conn.execute("UPDATE promocodes SET current_uses = current_uses + 1 WHERE code = $1", code)
        await conn.execute("INSERT INTO user_promo_uses (user_id, code) VALUES ($1, $2)", user_id, code)
    new_bal = await update_balance(user_id, "", reward)
    await state.clear()
    await message.answer(f"🎉 Промокод активирован!\n+{reward} 🎃\nТвой баланс: **{new_bal}**.", reply_markup=main_menu_kb(await is_admin_db(user_id)), parse_mode="Markdown")

# --- БАЛАНС / ПРОФИЛЬ / ТОП ---
@dp.callback_query(F.data == "balance_menu")
async def show_balance_menu(cb: types.CallbackQuery):
    await cb.message.edit_text("💰 **Всё о тыквах:**", reply_markup=balance_menu_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "show_balance")
async def show_balance(cb: types.CallbackQuery):
    info = await get_user_info(cb.from_user.id)
    await cb.message.edit_text(f"🎃 Твой котёл: **{info['balance'] if info else 0}** тыкв.", reply_markup=balance_menu_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "show_profile")
async def show_profile(cb: types.CallbackQuery):
    info = await get_user_info(cb.from_user.id)
    if not info: return await cb.answer("Ошибка")
    level, title = get_level_info(info['spent'])
    reg_str = info['registered_at'].strftime("%d.%m.%Y") if info['registered_at'] else "Неизвестно"
    await cb.message.edit_text(
        f"📊 **Профиль**\n\n👤 @{info['username'] or 'скрыт'}\n🆔 `{cb.from_user.id}`\n📅 С нами с: {reg_str}\n\n"
        f"🎃 Баланс: **{info['balance']}**\n💸 Потрачено: **{info['spent']}**\n📈 Уровень: **{level}** ({title})",
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

# --- БОНУС ---
@dp.callback_query(F.data == "daily_bonus")
async def claim_daily_bonus(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    now = datetime.now()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT last_claim, streak FROM user_bonuses WHERE user_id = $1", user_id)
    streak = 0
    if row:
        time_diff = now - row['last_claim']
        if time_diff < timedelta(hours=24):
            rem = timedelta(hours=24) - time_diff
            h, r = divmod(rem.seconds, 3600)
            m, _ = divmod(r, 60)
            return await cb.answer(f"⏳ Жди {h} ч. {m} мин.", show_alert=True)
        streak = row['streak'] or 0
        if time_diff > timedelta(hours=48): streak = 0
    streak += 1
    reward = 15 if streak == 1 else 20 if streak == 2 else 30 if streak == 3 else 40 if streak == 4 else 50 if streak == 5 else 75
    new_bal = await update_balance(user_id, "", reward)
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO user_bonuses (user_id, last_claim, streak) VALUES ($1, $2, $3) ON CONFLICT (user_id) DO UPDATE SET last_claim = $2, streak = $3", user_id, now, streak)
    await cb.message.edit_text(f"🎁 **Дар!**\n🔥 Серия: **{streak}** дней!\n💰 Награда: **{reward}** 🎃\n\nБаланс: **{new_bal}**.", reply_markup=main_menu_kb(await is_admin_db(user_id)), parse_mode="Markdown")
    await cb.answer("Получено!")

# --- ИГРОВОЙ ЗАЛ ---
@dp.callback_query(F.data == "games_menu")
async def show_games(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if cb.from_user.id in active_games: return await cb.answer("Ты уже в игре!", show_alert=True)
    await cb.message.edit_text("🎮 **Игровой зал**", reply_markup=games_menu_kb(), parse_mode="Markdown")
    await cb.answer()

# --- МИНЫ ---
@dp.callback_query(F.data == "mines_menu")
async def mines_menu(cb: types.CallbackQuery):
    await cb.message.edit_text("💣 **Игра «Мины»**\n\nВыбери сложность. Чем больше мин, тем выше множитель.", reply_markup=mines_difficulty_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data.startswith("mines_diff_"))
async def mines_difficulty(cb: types.CallbackQuery, state: FSMContext):
    difficulty = int(cb.data.replace("mines_diff_", ""))
    await state.update_data(difficulty=difficulty)
    await state.set_state(MinesGame.bet)
    await cb.message.edit_text(f"💣 Режим: **{difficulty} мин**\n\nВведи ставку (от 10 до 5000):", reply_markup=cancel_kb("mines_menu"), parse_mode="Markdown")
    await cb.answer()

@dp.message(MinesGame.bet)
async def mines_bet_input(message: types.Message, state: FSMContext):
    try:
        bet = int(message.text.strip())
        if bet < 10 or bet > 5000: raise ValueError
    except: return await message.answer("❌ Введи сумму от 10 до 5000.")
    info = await get_user_info(message.from_user.id)
    if info['balance'] < bet: return await message.answer("❌ Недостаточно тыкв!")
    difficulty = (await state.get_data()).get("difficulty", 3)
    await update_balance(message.from_user.id, "", -bet, is_spending=True)
    active_games[message.from_user.id] = {"type": "mines", "bet": bet, "mines": random.sample(range(25), difficulty), "opened": [], "difficulty": difficulty}
    await message.answer(f"💣 **Поле 5х5** (мин: {difficulty})\n💰 Ставка: {bet} 🎃\n📈 Множитель: **x1.00**\n\nОткрывай клетки:", reply_markup=mines_board_kb([], 1.0), parse_mode="Markdown")
    await state.clear()

@dp.callback_query(F.data.startswith("mines_open_"))
async def mines_open_cell(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    if user_id not in active_games or active_games[user_id]["type"] != "mines": return await cb.answer("Игра не найдена.", show_alert=True)
    game = active_games[user_id]
    idx = int(cb.data.replace("mines_open_", ""))
    if idx in game["opened"]: return await cb.answer("Уже открыто.", show_alert=True)
    if idx in game["mines"]:
        del active_games[user_id]
        b = InlineKeyboardBuilder()
        for i in range(25):
            if i in game["mines"]: b.button(text="💥", callback_data="ignore")
            elif i in game["opened"]: b.button(text="🟩", callback_data="ignore")
            else: b.button(text="⬜️", callback_data="ignore")
        b.button(text="🔙 В меню", callback_data="games_menu")
        b.adjust(5, 5, 5, 5, 5, 1)
        await cb.message.edit_text(f"💥 **БАБАХ!**\n\nСтавка: {game['bet']} 🎃 сгорела.\n\n🎃 Баланс: **{(await get_user_info(user_id))['balance']}**.", reply_markup=b.as_markup(), parse_mode="Markdown")
        return await cb.answer("Взрыв!")
    game["opened"].append(idx)
    mult = 1.0
    for i in range(len(game["opened"])):
        mult *= (25 - i) / (25 - game["difficulty"] - i)
    mult = max(1.0, mult * 0.95)
    if len(game["opened"]) == 25 - game["difficulty"]:
        win = int(game["bet"] * mult)
        new_bal = await update_balance(user_id, "", win)
        del active_games[user_id]
        await cb.message.edit_text(f"🎉 **ПОБЕДА!**\n\nВыигрыш: **{win}** 🎃\nБаланс: **{new_bal}**.", reply_markup=games_menu_kb(), parse_mode="Markdown")
        return await cb.answer("Максимум!")
    await cb.message.edit_text(f"💣 **Поле 5х5** (мин: {game['difficulty']})\n💰 Ставка: {game['bet']} 🎃\n📈 Множитель: **x{mult:.2f}**\n\nЗабрать сейчас: {int(game['bet'] * mult)} 🎃\nПродолжай или забирай:", reply_markup=mines_board_kb(game["opened"], mult), parse_mode="Markdown")
    await cb.answer("Безопасно!")

@dp.callback_query(F.data == "mines_cashout")
async def mines_cashout(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    if user_id not in active_games or active_games[user_id]["type"] != "mines": return await cb.answer("Игра не найдена.", show_alert=True)
    game = active_games[user_id]
    if not game["opened"]: return await cb.answer("Открой хотя бы одну клетку!", show_alert=True)
    del active_games[user_id]
    mult = 1.0
    for i in range(len(game["opened"])):
        mult *= (25 - i) / (25 - game["difficulty"] - i)
    mult = max(1.0, mult * 0.95)
    win = int(game["bet"] * mult)
    new_bal = await update_balance(user_id, "", win)
    await cb.message.edit_text(f"💰 **Забрано!**\n\nМножитель: **x{mult:.2f}**\nВыигрыш: **{win}** 🎃\nБаланс: **{new_bal}**.", reply_markup=games_menu_kb(), parse_mode="Markdown")
    await cb.answer("Успел!")

# --- КОЛЕСО / СЛАДОСТЬ / РАКЕТА / АПГРЕЙДЕР ---
@dp.callback_query(F.data == "game_wheel")
async def wheel(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    now = datetime.now()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT last_played FROM user_wheel WHERE user_id = $1", user_id)
    if row and (now - row['last_played']) < timedelta(hours=6):
        rem = timedelta(hours=6) - (now - row['last_played'])
        h, r = divmod(rem.seconds, 3600); m, _ = divmod(r, 60)
        return await cb.answer(f"🎡 Жди {h} ч. {m} мин.", show_alert=True)
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
async def trick(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    now = datetime.now()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT last_played FROM user_trickortreat WHERE user_id = $1", user_id)
    if row and (now - row['last_played']) < timedelta(hours=24):
        rem = timedelta(hours=24) - (now - row['last_played'])
        h, r = divmod(rem.seconds, 3600); m, _ = divmod(r, 60)
        return await cb.answer(f"🎃 Жди {h} ч. {m} мин.", show_alert=True)
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
    await cb.message.edit_text("🚀 **Ракета**\nВыбери ставку:", reply_markup=rocket_bet_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "rocket_bet_custom")
async def rocket_custom(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(RocketGame.bet)
    await cb.message.edit_text("✍️ Введи сумму:", reply_markup=cancel_kb("games_menu"))
    await cb.answer()

@dp.message(RocketGame.bet)
async def rocket_input(message: types.Message, state: FSMContext):
    try:
        bet = int(message.text.strip())
        if bet <= 0: raise ValueError
    except: return await message.answer("❌ Введи число > 0.")
    info = await get_user_info(message.from_user.id)
    if info['balance'] < bet: return await message.answer("❌ Недостаточно тыкв на балансе!")
    await state.clear()
    await start_rocket(message.from_user.id, bet, message)

@dp.callback_query(F.data.startswith("rocket_bet_") & ~F.data.startswith("rocket_bet_custom"))
async def rocket_preset(cb: types.CallbackQuery):
    bet = int(cb.data.replace("rocket_bet_", ""))
    await start_rocket(cb.from_user.id, bet, cb.message, cb)

async def start_rocket(user_id: int, bet: int, message: types.Message, cb: types.CallbackQuery = None):
    if user_id in active_games: return await (cb.answer("Уже в игре!", show_alert=True) if cb else message.answer("Уже в игре!"))
    info = await get_user_info(user_id)
    if info['balance'] < bet: return await (cb.answer("Мало тыкв!", show_alert=True) if cb else message.answer("Мало тыкв!"))
    await update_balance(user_id, "", -bet, is_spending=True)
    crash = round(random.uniform(1.05, 2.5), 2)
    text = f"🚀 **Взлет!**\nМножитель: **x1.00**\nСтавка: {bet} 🎃"
    if cb:
        await cb.message.edit_text(text, reply_markup=rocket_cashout_kb(), parse_mode="Markdown")
        msg = cb.message
    else:
        msg = await message.answer(text, reply_markup=rocket_cashout_kb(), parse_mode="Markdown")
    task = asyncio.create_task(rocket_loop(user_id, bet, crash, msg))
    active_games[user_id] = {"bet": bet, "mult": 1.0, "crash": crash, "task": task, "msg": msg, "type": "rocket"}

async def rocket_loop(user_id: int, bet: int, crash: float, msg: types.Message):
    mult = 1.0
    try:
        while True:
            await asyncio.sleep(1)
            if user_id not in active_games or active_games[user_id].get("type") != "rocket": return
            if active_games[user_id].get("crashed"): return
            mult += 0.1
            if mult >= crash:
                active_games[user_id]["crashed"] = True
                await asyncio.sleep(0.5)
                try: await bot.edit_message_text(chat_id=user_id, message_id=msg.message_id, text=f"💥 **БУМ!**\nМножитель x{crash:.2f}\nПотеряно: {bet} 🎃", reply_markup=None)
                except: pass
                if user_id in active_games: del active_games[user_id]
                return
            active_games[user_id]["mult"] = mult
            try: await bot.edit_message_text(chat_id=user_id, message_id=msg.message_id, text=f"🚀 **Летит!**\nМножитель: **x{mult:.2f}**\nЗабрать: **{int(bet*mult)}** 🎃", reply_markup=rocket_cashout_kb(), parse_mode="Markdown")
            except: pass
    except asyncio.CancelledError: pass
    except Exception as e:
        logging.error(f"Rocket: {e}")
        if user_id in active_games: del active_games[user_id]

@dp.callback_query(F.data == "rocket_cashout")
async def rocket_cashout(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    if user_id not in active_games or active_games[user_id].get("type") != "rocket": return await cb.answer("Игра окончена!", show_alert=True)
    game = active_games.pop(user_id)
    game["task"].cancel()
    win = int(game["bet"] * game["mult"])
    new_bal = await update_balance(user_id, "", win)
    await cb.message.edit_text(f"💰 **Забрано!**\nМножитель: x{game['mult']:.2f}\nВыигрыш: **{win}** 🎃\nБаланс: **{new_bal}**.", reply_markup=games_menu_kb(), parse_mode="Markdown")
    await cb.answer("Успел!")

@dp.callback_query(F.data == "game_upgrader")
async def upgr_start(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(Upgrader.amount)
    await cb.message.edit_text("📈 **Апгрейдер**\nВведи сумму:", reply_markup=cancel_kb("games_menu"), parse_mode="Markdown")
    await cb.answer()

@dp.message(Upgrader.amount)
async def upgr_input(message: types.Message, state: FSMContext):
    try:
        bet = int(message.text.strip())
        if bet <= 0: raise ValueError
    except: return await message.answer("❌ Введи число > 0.")
    info = await get_user_info(message.from_user.id)
    if info['balance'] < bet: return await message.answer("❌ Мало тыкв!")
    await state.update_data(bet=bet)
    await message.answer(f"📈 Ставка: {bet} 🎃\nВыбери режим:", reply_markup=upgrader_choice_kb())

@dp.callback_query(F.data == "upgrade_fast")
async def upgr_fast(cb: types.CallbackQuery, state: FSMContext):
    bet = (await state.get_data()).get("bet")
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
async def upgr_slow(cb: types.CallbackQuery, state: FSMContext):
    bet = (await state.get_data()).get("bet")
    if not bet: return await cb.answer("Устарело.", show_alert=True)
    await state.clear()
    await update_balance(cb.from_user.id, "", -bet, is_spending=True)
    win = int(bet * 1.5) if random.random() < 0.60 else 0
    new_bal = await update_balance(cb.from_user.id, "", win)
    text = f"🎉 Выигрыш: **+{win}** 🎃 (x1.5)\nБаланс: **{new_bal}**." if win else f"💀 Проигрыш: **-{bet}** 🎃\nБаланс: **{new_bal}**."
    b = InlineKeyboardBuilder().button(text="🔄 Ещё", callback_data="game_upgrader").button(text="🔙 В игры", callback_data="games_menu").adjust(1)
    await cb.message.edit_text(text, reply_markup=b.as_markup(), parse_mode="Markdown")
    await cb.answer()
    # --- ПЕРЕВОДЫ ---
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
    target = (await state.get_data()).get("target")
    await state.clear()
    info = await get_user_info(message.from_user.id)
    if info['balance'] < amt: return await message.answer("Мало тыкв!")
    await update_balance(message.from_user.id, "", -amt, is_spending=True)
    await update_balance(target, "", amt)
    await message.answer(f"✅ Переведено **{amt}** 🎃 пользователю `{target}`.", reply_markup=main_menu_kb(await is_admin_db(message.from_user.id)), parse_mode="Markdown")
    try: await bot.send_message(target, f"🎁 Тебе перевели **{amt}** 🎃!")
    except: pass

# --- МАГАЗИН И STARS ---
@dp.callback_query(F.data == "shop_menu")
async def show_shop(cb: types.CallbackQuery):
    await cb.message.edit_text("🛒 **Магазин**", reply_markup=shop_kb(cb.from_user.id), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data.startswith("buy_item_"))
async def buy_item(cb: types.CallbackQuery):
    item_id = cb.data.replace("buy_", "")
    item = SHOP_ITEMS.get(item_id)
    if not item: return await cb.answer("Нет товара.", show_alert=True)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT count FROM user_purchases WHERE user_id = $1 AND item_id = $2", cb.from_user.id, item_id)
    if (row['count'] if row else 0) >= item["limit"]: return await cb.answer("Лимит исчерпан!", show_alert=True)
    info = await get_user_info(cb.from_user.id)
    if info['balance'] < item["price"]: return await cb.answer("Мало тыкв!", show_alert=True)
    await update_balance(cb.from_user.id, "", -item["price"], is_spending=True)
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO user_purchases (user_id, item_id, count) VALUES ($1, $2, 1) ON CONFLICT (user_id, item_id) DO UPDATE SET count = count + 1", cb.from_user.id, item_id)
    await cb.message.edit_text(f"✅ Куплено **{item['name']}**!", reply_markup=main_menu_kb(await is_admin_db(cb.from_user.id)), parse_mode="Markdown")
    await notify_admins(f"🛒 `{cb.from_user.id}` купил **{item['name']}**.")

@dp.callback_query(F.data == "buy_stars_menu")
async def buy_stars_menu(cb: types.CallbackQuery):
    await cb.message.edit_text("💎 **Звезды** (1🎃=10⭐)\nМин: 100🎃", reply_markup=buy_stars_kb(), parse_mode="Markdown")
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
        await message.answer(f"🎉 +{p} 🎃", reply_markup=main_menu_kb(await is_admin_db(message.from_user.id)))

# --- КВЕСТЫ ---
@dp.callback_query(F.data == "quest_menu")
async def show_quests(cb: types.CallbackQuery):
    active = await check_and_rotate_quests()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT quest_id FROM user_quest_completions WHERE user_id = $1 AND completed_at > (SELECT value::timestamp FROM settings WHERE key = 'last_rotation')", cb.from_user.id)
    completed = [r['quest_id'] for r in rows]
    await cb.message.edit_text("🕸️ **Квесты** (обнов. каждые 12ч)", reply_markup=quests_kb(cb.from_user.id, active, completed), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data.startswith("quest_") & ~F.data.startswith("quest_menu"))
async def handle_quest(cb: types.CallbackQuery):
    qk = cb.data.replace("quest_", "")
    q = QUEST_POOL.get(qk)
    if not q: return await cb.answer("Устарел.", show_alert=True)
    await cb.message.edit_text(f"🕷️ **{q['name']}**\n{q['desc']}\n💰 {q['reward']} 🎃", reply_markup=proof_kb(qk), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data.startswith("proof_sent_"))
async def proof_sent(cb: types.CallbackQuery):
    qk = cb.data.replace("proof_sent_", "")
    q = QUEST_POOL.get(qk)
    if not q: return await cb.answer("Ошибка.", show_alert=True)
    user_id = cb.from_user.id
    async with db_pool.acquire() as conn:
        if await conn.fetchrow("SELECT 1 FROM quest_proofs WHERE user_id = $1 AND quest_name = $2 AND status = 'pending'", user_id, q["name"]):
            return await cb.answer("Ты уже отправил эту заявку.", show_alert=True)
        last_rot = await conn.fetchval("SELECT value FROM settings WHERE key = 'last_rotation'")
        if await conn.fetchrow("SELECT 1 FROM user_quest_completions WHERE user_id = $1 AND quest_id = $2 AND completed_at > $3", user_id, qk, last_rot):
            return await cb.answer("Ты уже выполнил этот квест!", show_alert=True)
    await save_proof(user_id, q["name"])
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO user_quest_completions (user_id, quest_id) VALUES ($1, $2)", user_id, qk)
    await cb.message.edit_text("✅ Заявка отправлена на проверку!", reply_markup=games_menu_kb())
    await cb.answer("Отправлено!")

# --- АДМИН ПАНЕЛЬ ---
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    await cb.message.edit_text("💀 **Панель Мастера**", reply_markup=admin_kb(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "admin_users_count")
async def admin_users_count(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    count = await get_total_users_count()
    await cb.message.edit_text(f"👥 **Статистика**\n\nВсего пользователей: **{count}**", reply_markup=admin_kb(), parse_mode="Markdown")
    await cb.answer()

# --- РАССЫЛКА ---
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    await state.set_state(Broadcast.text)
    await cb.message.edit_text("✉️ **Рассылка**\n\nОтправь текст сообщения (поддерживается Markdown):", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(Broadcast.text)
async def admin_broadcast_text(message: types.Message, state: FSMContext):
    await state.update_data(bc_text=message.text)
    await state.set_state(Broadcast.confirm)
    await message.answer(f"📝 **Предпросмотр:**\n\n{message.text}\n\nОтправить всем?", reply_markup=broadcast_confirm_kb(), parse_mode="Markdown")

@dp.callback_query(F.data == "broadcast_confirm_yes")
async def admin_broadcast_confirm(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    text = (await state.get_data()).get("bc_text")
    await state.clear()
    if not text: return await cb.answer("Текст потерян.", show_alert=True)
    users = await get_all_users()
    await cb.message.edit_text(f"🚀 Рассылка на {len(users)} пользователей...")
    await cb.answer()
    ok, blocked, failed = 0, 0, 0
    for uid in users:
        try:
            await bot.send_message(uid, text, parse_mode="Markdown")
            ok += 1
        except Exception as e:
            err = str(e).lower()
            if "blocked" in err or "chat not found" in err or "deactivated" in err:
                blocked += 1
            else:
                failed += 1
        await asyncio.sleep(0.05)
    await bot.send_message(
        cb.from_user.id,
        f"✅ **Рассылка завершена**\n\n📨 Доставлено: **{ok}**\n🚫 Заблокировали: **{blocked}**\n⚠️ Ошибок: **{failed}**",
        reply_markup=admin_kb(), parse_mode="Markdown"
    )

# --- ПРОМО ---
@dp.callback_query(F.data == "admin_create_promo")
async def admin_create_promo_start(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    await state.set_state(PromoCreate.name)
    await cb.message.edit_text("🎟️ Введи название промокода:", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(PromoCreate.name)
async def promo_name(message: types.Message, state: FSMContext):
    await state.update_data(promo_name=message.text.strip().upper())
    await state.set_state(PromoCreate.reward)
    await message.answer("Сколько тыкв будет давать промокод?", reply_markup=cancel_kb())

@dp.message(PromoCreate.reward)
async def promo_reward(message: types.Message, state: FSMContext):
    try: r = int(message.text.strip())
    except: return await message.answer("❌ Введи число.")
    if r <= 0: return await message.answer("❌ Больше 0.")
    await state.update_data(promo_reward=r)
    await state.set_state(PromoCreate.limit)
    await message.answer("Сколько раз можно использовать?", reply_markup=cancel_kb())

@dp.message(PromoCreate.limit)
async def promo_limit(message: types.Message, state: FSMContext):
    try: l = int(message.text.strip())
    except: return await message.answer("❌ Введи число.")
    if l <= 0: return await message.answer("❌ Больше 0.")
    data = await state.get_data()
    await state.clear()
    name, reward = data['promo_name'], data['promo_reward']
    async with db_pool.acquire() as conn:
        if await conn.fetchrow("SELECT 1 FROM promocodes WHERE code = $1", name):
            return await message.answer(f"❌ Промокод `{name}` уже существует!", reply_markup=admin_kb(), parse_mode="Markdown")
        await conn.execute("INSERT INTO promocodes (code, reward, limit_uses) VALUES ($1, $2, $3)", name, reward, l)
    await message.answer(f"✅ Промокод `{name}` создан!\n+{reward} 🎃\nЛимит: {l}", reply_markup=admin_kb(), parse_mode="Markdown")

# --- ЗАЯВКИ ---
@dp.callback_query(F.data == "admin_pending")
async def admin_pending(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    rows = await get_pending(10)
    if not rows: return await cb.message.edit_text("📜 Заявок нет.", reply_markup=admin_kb())
    await cb.message.edit_text("📜 **Активные заявки:**", reply_markup=admin_kb())
    for row in rows:
        builder = InlineKeyboardBuilder()
        builder.button(text=f"✅ Выдать", callback_data=f"admin_approve_{row['id']}")
        builder.button(text="❌ Отклонить", callback_data=f"admin_reject_{row['id']}")
        builder.adjust(1)
        await bot.send_message(cb.from_user.id, f"📜 **#{row['id']}**\n👤 `{row['user_id']}`\n🎯 {row['quest_name']}", reply_markup=builder.as_markup(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data.startswith("admin_approve_"))
async def admin_approve(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    pid = int(cb.data.replace("admin_approve_", ""))
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id, quest_name, status FROM quest_proofs WHERE id = $1", pid)
    if not row or row['status'] != 'pending': return await cb.answer("Обработана.", show_alert=True)
    reward = next((q["reward"] for q in QUEST_POOL.values() if q["name"] == row['quest_name']), 0)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM quest_proofs WHERE id = $1", pid)
    new_bal = await update_balance(row['user_id'], "", reward)
    await cb.message.edit_text(f"✅ Выдано {reward} 🎃 пользователю `{row['user_id']}`.")
    await cb.answer("Готово!")
    try: await bot.send_message(row['user_id'], f"🎉 Квест **{row['quest_name']}** подтвержден!\n+{reward} 🎃\nБаланс: **{new_bal}**.", parse_mode="Markdown")
    except: pass

@dp.callback_query(F.data.startswith("admin_reject_"))
async def admin_reject(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    pid = int(cb.data.replace("admin_reject_", ""))
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id, quest_name, status FROM quest_proofs WHERE id = $1", pid)
    if not row or row['status'] != 'pending': return await cb.answer("Обработана.", show_alert=True)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM quest_proofs WHERE id = $1", pid)
    await cb.message.edit_text(f"❌ Заявка #{pid} отклонена.")
    await cb.answer("Отклонено.")
    try: await bot.send_message(row['user_id'], f"❌ Квест **{row['quest_name']}** отклонен.")
    except: pass

# --- ПРОЧИЕ АДМИН ФУНКЦИИ ---
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

@dp.callback_query(F.data == "admin_check_profile")
async def admin_check_profile_start(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    await state.set_state(AdminViewProfile.target)
    await cb.message.edit_text("👤 Введи ID или @username:", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(AdminViewProfile.target)
async def admin_check_profile_input(message: types.Message, state: FSMContext):
    t = await find_user_id(message.text)
    if not t: return await message.answer("❌ Не найден.")
    await state.clear()
    info = await get_user_info(t)
    if not info: return await message.answer("❌ Нет в базе.")
    level, title = get_level_info(info['spent'])
    await message.answer(
        f"📊 **Профиль игрока**\n\n👤 @{info['username'] or 'скрыт'}\n🆔 `{t}`\n\n"
        f"🎃 Баланс: **{info['balance']}**\n💸 Потрачено: **{info['spent']}**\n📈 Уровень: **{level}** ({title})",
        reply_markup=admin_kb(), parse_mode="Markdown"
    )

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
    await state.clear()
    final = a if d["action"] == "give" else -a
    # Чтобы написать реально списанную сумму, нам нужно знать баланс ДО транзакции
    old_info = await get_user_info(d["target"])
    old_bal = old_info['balance'] if old_info else 0
    
    new = await update_balance(d["target"], "", final)
    
    actual_change = new - old_bal
    await message.answer(f"✅ Готово. Баланс: {new} 🎃", reply_markup=admin_kb())
    try: 
        if actual_change > 0:
            await bot.send_message(d["target"], f"🎁 Твой баланс пополнен на {actual_change} 🎃.")
        elif actual_change < 0:
            await bot.send_message(d["target"], f"🪓 С твоего баланса списано {-actual_change} 🎃.")
    except: pass

@dp.callback_query(F.data == "admin_take_level")
async def admin_take_level_start(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    await state.set_state(AdminTakeLevel.target_id)
    await cb.message.edit_text("🪓 У кого отобрать уровни?", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(AdminTakeLevel.target_id)
async def admin_take_level_input(message: types.Message, state: FSMContext):
    t = await find_user_id(message.text)
    if not t: return await message.answer("Не найден.")
    await state.update_data(target=t)
    await state.set_state(AdminTakeLevel.amount)
    await message.answer("Сколько опыта отнять?", reply_markup=cancel_kb())

@dp.message(AdminTakeLevel.amount)
async def admin_take_level_amt(message: types.Message, state: FSMContext):
    try: a = int(message.text.strip())
    except: return await message.answer("Число.")
    if a <= 0: return await message.answer("Больше 0.")
    t = (await state.get_data()).get("target")
    await state.clear()
    new_spent = await admin_take_level(t, a)
    new_level, _ = get_level_info(new_spent)
    await message.answer(f"✅ Уровень `{t}` теперь **{new_level}** (Опыт: {new_spent})", reply_markup=admin_kb(), parse_mode="Markdown")
    try: await bot.send_message(t, f"Твой уровень понижен до {new_level}.")
    except: pass

@dp.callback_query(F.data == "admin_take_level_all")
async def admin_take_level_all_start(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    await state.set_state(AdminTakeLevelAll.amount)
    await cb.message.edit_text("🪓 Сколько опыта отнять У ВСЕХ?", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(AdminTakeLevelAll.amount)
async def admin_take_level_all_input(message: types.Message, state: FSMContext):
    try: a = abs(int(message.text.strip()))
    except: return await message.answer("Число.")
    if a == 0: return await message.answer("Больше 0.")
    await state.clear()
    users = await get_all_users()
    await message.answer(f"🚀 Списание у {len(users)} пользователей...")
    for u in users:
        await admin_take_level(u, a)
        await asyncio.sleep(0.05)
    await message.answer(f"✅ Готово! Обработано {len(users)}.", reply_markup=admin_kb())

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
    await message.answer(f"🚀 Начисляю {a} для {len(users)}...")
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
    await message.answer(f"🚀 Списание {a} у {len(users)}...")
    for u in users:
        # Фикс уведомления о реально списанной сумме
        info = await get_user_info(u)
        if info and info['balance'] > 0:
            actual = min(info['balance'], a)
            await update_balance(u, "", -a, is_spending=True)
            try: await bot.send_message(u, f"🪓 Гнев Мастера! -{actual} 🎃")
            except: pass
        else:
            await update_balance(u, "", -a, is_spending=True)
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
    await cb.message.edit_text(f"🚀 Рассылка подарка для {len(users)}...")
    s, f = 0, 0
    for u in users:
        try:
            await bot.send_gift(user_id=u, gift_id=gid)
            s += 1
        except: f += 1
        await asyncio.sleep(0.5)
    await cb.message.answer(f"✅ Отправлено: {s}\nОшибок: {f}", reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_wipe_db")
async def admin_wipe_db(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    builder = InlineKeyboardBuilder()
    builder.button(text="⚠️ ДА, СНЕСТИ ВСЁ", callback_data="admin_wipe_db_confirm")
    builder.button(text="❌ Отмена", callback_data="admin_cancel")
    builder.adjust(1)
    await cb.message.edit_text("💣 **ВНИМАНИЕ!**\n\nБаза данных будет полностью удалена. Ты уверен?", reply_markup=builder.as_markup(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "admin_wipe_db_confirm")
async def admin_wipe_db_confirm(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id): return await cb.answer("Нет.", show_alert=True)
    await cb.message.edit_text("💣 **Уничтожаем базу...**")
    try:
        await wipe_database()
        await cb.message.edit_text("✅ **БАЗА СНЕСЕНА!**", reply_markup=admin_kb(), parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Wipe: {e}")
        await cb.message.edit_text(f"❌ Ошибка: {e}", reply_markup=admin_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_cancel")
async def admin_cancel(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("💀 Панель:", reply_markup=admin_kb())
    await cb.answer("Отменено")

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
    logging.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
