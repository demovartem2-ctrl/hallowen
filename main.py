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

# --- НАСТРОЙКИ И ЛОГИ ---
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

TOKEN = os.environ.get("BOT_TOKEN", "")
API_BASE = os.environ.get("API_BASE", "https://misugram.cloud-ip.cc")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

ADMIN_USERNAME = "sardelka"
DEFAULT_ADMINS = {1780243203, 1780243210}

GIFT_IDS = [
    "9000000000000152", "9000000000000153", "9000000000000154",
    "9000000000000155", "9000000000000156", "9000000000000157",
    "9000000000000158", "9000000000000159", "9000000000000160",
    "9000000000000200"
]

db_pool: asyncpg.Pool = None
active_games = {}

# --- БАЗА ДАННЫХ ---
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, ssl='require')
    async with db_pool.acquire() as c:
        await c.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY, username TEXT, balance BIGINT DEFAULT 0,
            spent BIGINT DEFAULT 0, registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await c.execute("""CREATE TABLE IF NOT EXISTS quest_proofs (
            id SERIAL PRIMARY KEY, user_id BIGINT, quest_id TEXT, quest_name TEXT,
            proof_text TEXT, status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await c.execute("CREATE TABLE IF NOT EXISTS user_purchases (user_id BIGINT, item_id TEXT, count INTEGER DEFAULT 0, PRIMARY KEY (user_id, item_id))")
        await c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        await c.execute("CREATE TABLE IF NOT EXISTS user_quest_completions (user_id BIGINT, quest_id TEXT, completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        await c.execute("CREATE TABLE IF NOT EXISTS admins (user_id BIGINT PRIMARY KEY)")
        await c.execute("CREATE TABLE IF NOT EXISTS user_bonuses (user_id BIGINT PRIMARY KEY, last_claim TIMESTAMP, streak INTEGER DEFAULT 0)")
        await c.execute("CREATE TABLE IF NOT EXISTS user_trickortreat (user_id BIGINT PRIMARY KEY, last_played TIMESTAMP)")
        await c.execute("CREATE TABLE IF NOT EXISTS user_wheel (user_id BIGINT PRIMARY KEY, last_played TIMESTAMP)")
        await c.execute("CREATE TABLE IF NOT EXISTS promocodes (code TEXT PRIMARY KEY, reward BIGINT, limit_uses INTEGER, current_uses INTEGER DEFAULT 0)")
        await c.execute("CREATE TABLE IF NOT EXISTS user_promo_uses (user_id BIGINT, code TEXT, PRIMARY KEY (user_id, code))")
        await c.execute("ALTER TABLE quest_proofs ADD COLUMN IF NOT EXISTS quest_id TEXT")
        for aid in DEFAULT_ADMINS:
            await c.execute("INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING", aid)

async def wipe_database():
    async with db_pool.acquire() as c:
        tables = ("users","quest_proofs","user_purchases","settings",
                  "user_quest_completions","user_bonuses","user_trickortreat",
                  "user_wheel","promocodes","user_promo_uses","admins")
        for t in tables:
            await c.execute(f"TRUNCATE TABLE {t} RESTART IDENTITY CASCADE")
        for aid in DEFAULT_ADMINS:
            await c.execute("INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING", aid)

async def get_user_info(uid: int):
    async with db_pool.acquire() as c:
        return await c.fetchrow("SELECT username, balance, spent, registered_at FROM users WHERE user_id=$1", uid)

async def update_balance(uid: int, uname: Optional[str], amt: int, is_spending: bool = False) -> int:
    async with db_pool.acquire() as c:
        row = await c.fetchrow("SELECT balance, spent FROM users WHERE user_id=$1", uid)
        if row:
            nb = max(0, row['balance'] + amt)
            diff = nb - row['balance']
            ns = row['spent'] + (-diff) if (is_spending and diff < 0) else row['spent']
            ns = max(0, ns)
            await c.execute("UPDATE users SET balance=$1, spent=$2, username=COALESCE($3, username) WHERE user_id=$4",
                            nb, ns, uname or None, uid)
            return nb
        nb = max(0, amt)
        ns = (-amt) if (is_spending and amt < 0) else 0
        await c.execute("INSERT INTO users (user_id, username, balance, spent) VALUES ($1,$2,$3,$4)", uid, uname or None, nb, ns)
        return nb

async def admin_take_level(uid: int, amt: int) -> int:
    async with db_pool.acquire() as c:
        row = await c.fetchrow("SELECT spent FROM users WHERE user_id=$1", uid)
        if not row: return 0
        ns = max(0, row['spent'] - amt)
        await c.execute("UPDATE users SET spent=$1 WHERE user_id=$2", ns, uid)
        return ns

def get_level_info(spent: int) -> tuple[int, str]:
    spent = spent or 0
    level = (spent // 1000) + 1
    titles = ["🥚 Новичок","🎃 Любитель","🦇 Охотник","💀 Мастер","👑 Король Тьмы"]
    if level >= 50: title = "👑 Тёмный Лорд"
    elif level <= len(titles): title = titles[level-1]
    else: title = "💀 Мастер"
    return level, title

async def is_admin_db(uid: int) -> bool:
    if uid in DEFAULT_ADMINS: return True
    async with db_pool.acquire() as c:
        return bool(await c.fetchrow("SELECT 1 FROM admins WHERE user_id=$1", uid))

async def notify_admins(text: str):
    async with db_pool.acquire() as c:
        rows = await c.fetch("SELECT user_id FROM admins")
    for aid in {r['user_id'] for r in rows} | DEFAULT_ADMINS:
        try: await bot.send_message(aid, text)
        except: pass

async def get_all_users():
    async with db_pool.acquire() as c:
        return [r['user_id'] for r in await c.fetch("SELECT user_id FROM users")]

async def get_total_users_count() -> int:
    async with db_pool.acquire() as c:
        return await c.fetchval("SELECT COUNT(*) FROM users") or 0

async def find_user_id(identifier: str) -> Optional[int]:
    identifier = identifier.strip().lstrip("@")
    if identifier.isdigit(): return int(identifier)
    async with db_pool.acquire() as c:
        row = await c.fetchrow("SELECT user_id FROM users WHERE LOWER(username)=$1", identifier.lower())
    return row['user_id'] if row else None

async def get_top(limit: int = 10):
    async with db_pool.acquire() as c:
        return await c.fetch("SELECT user_id, username, balance FROM users WHERE balance>0 ORDER BY balance DESC LIMIT $1::int", limit)

async def get_pending(limit: int = 10):
    async with db_pool.acquire() as c:
        return await c.fetch("SELECT id,user_id,quest_name,created_at FROM quest_proofs WHERE status='pending' ORDER BY id DESC LIMIT $1::int", limit)

async def save_proof(uid: int, qid: str, qname: str):
    async with db_pool.acquire() as c:
        await c.execute("INSERT INTO quest_proofs (user_id,quest_id,quest_name) VALUES ($1,$2,$3)", uid, qid, qname)

# --- КВЕСТЫ ---
QUEST_POOL = {
    "q1": {"name": "Раздай 5 подарков", "reward": 15, "desc": "Раздай 5 подарков."},
    "q2": {"name": "Фото с тыквой",     "reward": 10, "desc": "Сделай фото с тыквой."},
    "q3": {"name": "Купи 3 подарка",    "reward": 5,  "desc": "Купи 3 подарка себе."},
}

async def check_and_rotate_quests():
    async with db_pool.acquire() as c:
        last = await c.fetchval("SELECT value FROM settings WHERE key='last_rotation'")
        active = await c.fetchval("SELECT value FROM settings WHERE key='active_quests'")
        if last and active:
            try:
                if datetime.now() - datetime.fromisoformat(last) < timedelta(hours=12):
                    return active.split(",")
            except Exception:
                pass
        keys = list(QUEST_POOL.keys())
        random.shuffle(keys)
        active = keys[:3]
        now = datetime.now().isoformat()
        await c.execute("INSERT INTO settings (key,value) VALUES ('last_rotation',$1) ON CONFLICT (key) DO UPDATE SET value=$1", now)
        await c.execute("INSERT INTO settings (key,value) VALUES ('active_quests',$1) ON CONFLICT (key) DO UPDATE SET value=$1", ",".join(active))
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

# --- МАГАЗИН (ЦЕНЫ −37.5%) ---
SHOP_ITEMS = {
    "item_3blacks":  {"name": "3 блека",    "price": 1000, "limit": 2, "emoji": "🖤"},
    "item_nft_user": {"name": "Нфт юз",     "price": 1188, "limit": 1, "emoji": "🖼️"},
    "item_nft_num":  {"name": "Номер Nft",  "price": 1375, "limit": 1, "emoji": "🔢🖼️"},
    "item_scam":     {"name": "Scam метка", "price": 1875, "limit": 1, "emoji": "⚠️"},
}

# --- ХЕЛПЕР БЕЗОПАСНОГО РЕДАКТИРОВАНИЯ ---
async def safe_edit(msg: types.Message, text: str, kb=None, parse_mode="Markdown"):
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode=parse_mode)
    except Exception as e:
        if "message is not modified" in str(e).lower():
            return
        try:
            await msg.answer(text, reply_markup=kb, parse_mode=parse_mode)
        except Exception as e2:
            log.warning(f"safe_edit fail: {e2}")

# --- КЛАВИАТУРЫ ---
def main_menu_kb(is_admin: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t, c in [("🕸️ Квесты","quest_menu"),("💰 Баланс","balance_menu"),
                 ("🎮 Мини-игры","games_menu"),("🎟️ Промокод","enter_promo"),
                 ("📊 Профиль","show_profile"),("🏆 Зал славы","show_top"),
                 ("🎁 Бонус","daily_bonus"),("🆔 Мой ID","show_my_id"),
                 ("🆘 Поддержка","support_menu")]:
        b.button(text=t, callback_data=c)
    if is_admin: b.button(text="💀 Панель мастера", callback_data="admin_panel")
    b.adjust(2)
    return b.as_markup()

def balance_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t, c in [("🎃 Мой баланс","show_balance"),("🛒 Магазин","shop_menu"),
                 ("💸 Перевод","transfer_start"),("💎 Купить тыквы","buy_stars_menu"),
                 ("🔙 Назад","back_to_main")]:
        b.button(text=t, callback_data=c)
    b.adjust(1)
    return b.as_markup()

def games_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t, c in [("🚀 Ракета","game_rocket"),("🍬 Сладость/Гадость","game_trickortreat"),
                 ("📈 Апгрейдер","game_upgrader"),("🎡 Колесо (20🎃)","game_wheel"),
                 ("💣 Мины","mines_menu"),("🔙 Назад","back_to_main")]:
        b.button(text=t, callback_data=c)
    b.adjust(1)
    return b.as_markup()

def mines_difficulty_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t, c in [("🟢 1 мина","mines_diff_1"),("🟡 3 мины","mines_diff_3"),
                 ("🟠 5 мин","mines_diff_5"),("🔴 10 мин","mines_diff_10"),
                 ("💀 24 мины","mines_diff_24"),("🔙 Назад","games_menu")]:
        b.button(text=t, callback_data=c)
    b.adjust(1)
    return b.as_markup()

def mines_board_kb(opened: list, mult: float) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i in range(25):
        b.button(text="🟩" if i in opened else "⬜️",
                 callback_data="ignore" if i in opened else f"mines_open_{i}")
    b.button(text=f"💰 ЗАБРАТЬ (x{mult:.2f})", callback_data="mines_cashout")
    b.adjust(5,5,5,5,5,1)
    return b.as_markup()

def rocket_bet_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t, c in [("10 🎃","rocket_bet_10"),("50 🎃","rocket_bet_50"),
                 ("100 🎃","rocket_bet_100"),("✍️ Своя","rocket_bet_custom"),
                 ("🔙 Назад","games_menu")]:
        b.button(text=t, callback_data=c)
    b.adjust(2)
    return b.as_markup()

def rocket_cashout_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💰 ЗАБРАТЬ", callback_data="rocket_cashout")
    return b.as_markup()

def upgrader_choice_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⚡ Быстро (25%, x2.2)", callback_data="upgrade_fast")
    b.button(text="🐢 Медленно (45%, x1.6)", callback_data="upgrade_slow")
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
    for iid, d in SHOP_ITEMS.items():
        b.button(text=f"{d['emoji']} {d['name']} - {d['price']}🎃", callback_data=f"buy_item_{iid}")
    b.button(text="🔙 Назад", callback_data="balance_menu")
    b.adjust(1)
    return b.as_markup()

def buy_stars_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t, c in [("100 🎃 (1000 ⭐)","buy_stars_100"),("250 🎃 (2500 ⭐)","buy_stars_250"),
                 ("500 🎃 (5000 ⭐)","buy_stars_500"),("✍️ Своя","buy_stars_custom"),
                 ("🔙 Назад","balance_menu")]:
        b.button(text=t, callback_data=c)
    b.adjust(1)
    return b.as_markup()

def quests_kb(user_id: int, active: list, completed: list) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for qid in active:
        q = QUEST_POOL[qid]
        if qid in completed: b.button(text=f"✅ {q['name']}", callback_data="ignore")
        else: b.button(text=f"🎁 {q['name']} (+{q['reward']})", callback_data=f"quest_{qid}")
    b.button(text="🔙 Назад", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def proof_kb(qk: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"📩 Написать @{ADMIN_USERNAME}", url=f"https://t.me/{ADMIN_USERNAME}")
    b.button(text="✅ Я отправил", callback_data=f"proof_sent_{qk}")
    b.button(text="🔙 Назад", callback_data="quest_menu")
    b.adjust(1)
    return b.as_markup()

def admin_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t, c in [("🪄 Выдать","admin_give"),("🪓 Отобрать","admin_take"),
                 ("🪓 Отобрать лвл","admin_take_level"),("🪓 Отобрать лвл у всех","admin_take_level_all"),
                 ("🪄 Начислить всем","admin_give_all"),("🪓 Отобрать у всех","admin_take_all"),
                 ("✉️ Рассылка","admin_broadcast"),("🎁 Подарки","admin_gift_all"),
                 ("🎟️ Создать промокод","admin_create_promo"),("👥 Всего юзеров","admin_users_count"),
                 ("👤 Профиль","admin_check_profile"),("👁 Баланс","admin_check_bal"),
                 ("📜 Заявки","admin_pending"),("👑 Выдать админку","admin_grant"),
                 ("💣 Снести БД","admin_wipe_db"),("🔙 В меню","back_to_main")]:
        b.button(text=t, callback_data=c)
    b.adjust(1)
    return b.as_markup()

def admin_gift_choice_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for gid in GIFT_IDS: b.button(text=f"🎁 {gid}", callback_data=f"send_gift_{gid}")
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
    return b.as_markup()

# --- ИНИЦИАЛИЗАЦИЯ БОТА ---
if not TOKEN: raise RuntimeError("BOT_TOKEN не задан")
if not DATABASE_URL: raise RuntimeError("DATABASE_URL не задан")

session = AiohttpSession(api=TelegramAPIServer.from_base(API_BASE))
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())
# ---------- СТАРТ / НАВИГАЦИЯ ----------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await update_balance(message.from_user.id,
                         message.from_user.username or message.from_user.full_name, 0)
    await message.answer(
        f"🦇 Приветствую, {message.from_user.first_name}! 🕸️\n\nТьма сгущается... 🎃\nЧто выберешь?",
        reply_markup=main_menu_kb(await is_admin_db(message.from_user.id)))

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(cb.message, "🔮 Главное меню:", main_menu_kb(await is_admin_db(cb.from_user.id)))
    await cb.answer()

@dp.callback_query(F.data == "ignore")
async def ignore_cb(cb: types.CallbackQuery):
    await cb.answer("Недоступно.", show_alert=True)

@dp.callback_query(F.data == "show_my_id")
async def show_my_id(cb: types.CallbackQuery):
    await safe_edit(cb.message, f"🆔 ID: `{cb.from_user.id}`", main_menu_kb(await is_admin_db(cb.from_user.id)))
    await cb.answer()

@dp.callback_query(F.data == "support_menu")
async def show_support(cb: types.CallbackQuery):
    await safe_edit(cb.message, "🆘 Поддержка:\n👻 @sardelka\n👾 @mehura", support_kb())
    await cb.answer()

# ---------- ПРОМО ----------
@dp.message(Command("promo"))
async def cmd_promo(message: types.Message, state: FSMContext):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Использование: `/promo КОД`", parse_mode="Markdown")
    await process_promo_code(message.from_user.id, args[1].strip().upper(), message, state)

@dp.callback_query(F.data == "enter_promo")
async def enter_promo_cb(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(PromoEnter.code)
    await safe_edit(cb.message, "🎟️ Введи промокод:", cancel_kb("back_to_main"))
    await cb.answer()

@dp.message(PromoEnter.code)
async def enter_promo_input(message: types.Message, state: FSMContext):
    await process_promo_code(message.from_user.id, message.text.strip().upper(), message, state)

async def process_promo_code(user_id: int, code: str, message: types.Message, state: FSMContext):
    async with db_pool.acquire() as c:
        promo = await c.fetchrow("SELECT reward,limit_uses,current_uses FROM promocodes WHERE code=$1", code)
        if not promo:
            await state.clear()
            return await message.answer("❌ Такого промокода нет.")
        if promo['current_uses'] >= promo['limit_uses']:
            await state.clear()
            return await message.answer("❌ Промокод исчерпан.")
        used = await c.fetchrow("SELECT 1 FROM user_promo_uses WHERE user_id=$1 AND code=$2", user_id, code)
        if used:
            await state.clear()
            return await message.answer("❌ Ты уже использовал этот промокод.")

        res = await c.execute("UPDATE promocodes SET current_uses=current_uses+1 WHERE code=$1 AND current_uses<limit_uses", code)
        if res.endswith("0"):
            await state.clear()
            return await message.answer("❌ Промокод только что закончился.")

        await c.execute("INSERT INTO user_promo_uses (user_id, code) VALUES ($1,$2)", user_id, code)
        reward = promo['reward']

    new_bal = await update_balance(user_id, None, reward)
    await state.clear()
    await message.answer(f"🎉 Промокод активирован!\n+{reward} 🎃\nБаланс: **{new_bal}**.",
                         reply_markup=main_menu_kb(await is_admin_db(user_id)), parse_mode="Markdown")

# ---------- БАЛАНС / ПРОФИЛЬ / ТОП ----------
@dp.callback_query(F.data == "balance_menu")
async def show_balance_menu(cb: types.CallbackQuery):
    await safe_edit(cb.message, "💰 **Всё о тыквах:**", balance_menu_kb())
    await cb.answer()

@dp.callback_query(F.data == "show_balance")
async def show_balance(cb: types.CallbackQuery):
    info = await get_user_info(cb.from_user.id)
    await safe_edit(cb.message, f"🎃 Твой котёл: **{info['balance'] if info else 0}** тыкв.", balance_menu_kb())
    await cb.answer()

@dp.callback_query(F.data == "show_profile")
async def show_profile(cb: types.CallbackQuery):
    info = await get_user_info(cb.from_user.id)
    if not info:
        return await cb.answer("Ошибка", show_alert=True)
    level, title = get_level_info(info['spent'])
    reg = info['registered_at'].strftime("%d.%m.%Y") if info['registered_at'] else "—"
    await safe_edit(cb.message,
        f"📊 **Профиль**\n\n👤 @{info['username'] or 'скрыт'}\n🆔 `{cb.from_user.id}`\n📅 С нами с: {reg}\n\n"
        f"🎃 Баланс: **{info['balance']}**\n💸 Потрачено: **{info['spent']}**\n📈 Уровень: **{level}** ({title})",
        main_menu_kb(await is_admin_db(cb.from_user.id)))
    await cb.answer()

@dp.callback_query(F.data == "show_top")
async def show_top(cb: types.CallbackQuery):
    rows = await get_top(10)
    text = "🏆 **Зал славы**\n\n"
    medals = ["🥇","🥈","🥉"] + ["💀"]*10
    for i, row in enumerate(rows):
        name = f"@{row['username']}" if row['username'] else f"id {row['user_id']}"
        text += f"{medals[i]} {name} — {row['balance']} 🎃\n"
    await safe_edit(cb.message, text, main_menu_kb(await is_admin_db(cb.from_user.id)))
    await cb.answer()

# ---------- БОНУС ----------
@dp.callback_query(F.data == "daily_bonus")
async def claim_daily_bonus(cb: types.CallbackQuery):
    uid = cb.from_user.id
    now = datetime.now()
    async with db_pool.acquire() as c:
        row = await c.fetchrow("SELECT last_claim, streak FROM user_bonuses WHERE user_id=$1", uid)
    streak = 0
    if row:
        dt = now - row['last_claim']
        if dt < timedelta(hours=24):
            rem = timedelta(hours=24) - dt
            h, r = divmod(rem.seconds, 3600)
            m, _ = divmod(r, 60)
            return await cb.answer(f"⏳ Жди {h} ч. {m} мин.", show_alert=True)
        streak = row['streak'] or 0
        if dt > timedelta(hours=48): streak = 0
    streak += 1
    reward = {1:10, 2:15, 3:20, 4:25, 5:30}.get(streak, 50)
    new_bal = await update_balance(uid, None, reward)
    async with db_pool.acquire() as c:
        await c.execute("INSERT INTO user_bonuses (user_id,last_claim,streak) VALUES ($1,$2,$3) "
                        "ON CONFLICT (user_id) DO UPDATE SET last_claim=$2, streak=$3", uid, now, streak)
    await safe_edit(cb.message,
        f"🎁 **Дар!**\n🔥 Серия: **{streak}** дней!\n💰 Награда: **{reward}** 🎃\n\nБаланс: **{new_bal}**.",
        main_menu_kb(await is_admin_db(uid)))
    await cb.answer("Получено!")

# ---------- ИГРЫ МЕНЮ ----------
@dp.callback_query(F.data == "games_menu")
async def show_games(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if cb.from_user.id in active_games:
        return await cb.answer("Ты уже в игре!", show_alert=True)
    await safe_edit(cb.message, "🎮 **Игровой зал**", games_menu_kb())
    await cb.answer()

# ---------- МИНЫ ----------
def mines_multiplier(opened: int, difficulty: int) -> float:
    mult = 1.0
    for i in range(opened):
        denom = 25 - difficulty - i
        if denom <= 0:
            return 0.0
        mult *= (25 - i) / denom
    return max(1.0, mult * 0.85)

@dp.callback_query(F.data == "mines_menu")
async def mines_menu(cb: types.CallbackQuery):
    await safe_edit(cb.message, "💣 **Игра «Мины»**\n\nВыбери сложность:", mines_difficulty_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("mines_diff_"))
async def mines_difficulty(cb: types.CallbackQuery, state: FSMContext):
    diff = int(cb.data.replace("mines_diff_",""))
    await state.update_data(difficulty=diff)
    await state.set_state(MinesGame.bet)
    await safe_edit(cb.message, f"💣 Режим: **{diff} мин**\n\nВведи ставку (10–5000):", cancel_kb("mines_menu"))
    await cb.answer()

@dp.message(MinesGame.bet)
async def mines_bet_input(message: types.Message, state: FSMContext):
    try:
        bet = int(message.text.strip())
        if not (10 <= bet <= 5000): raise ValueError
    except:
        return await message.answer("❌ Введи сумму от 10 до 5000.")
    uid = message.from_user.id
    if uid in active_games:
        return await message.answer("❌ Сначала закончи текущую игру!")
    info = await get_user_info(uid)
    if not info or info['balance'] < bet:
        return await message.answer("❌ Недостаточно тыкв!")

    difficulty = (await state.get_data()).get("difficulty", 3)
    await update_balance(uid, None, -bet, is_spending=True)
    active_games[uid] = {"type":"mines","bet":bet,"mines":random.sample(range(25), difficulty),
                         "opened":[],"difficulty":difficulty}
    await message.answer(f"💣 **Поле 5х5** (мин: {difficulty})\n💰 Ставка: {bet} 🎃\n📈 Множитель: **x1.00**\n\nОткрывай клетки:",
                         reply_markup=mines_board_kb([], 1.0), parse_mode="Markdown")
    await state.clear()

@dp.callback_query(F.data.startswith("mines_open_"))
async def mines_open_cell(cb: types.CallbackQuery):
    uid = cb.from_user.id
    if uid not in active_games or active_games[uid].get("type") != "mines":
        return await cb.answer("Игра не найдена.", show_alert=True)
    game = active_games[uid]
    idx = int(cb.data.replace("mines_open_",""))
    if idx in game["opened"]:
        return await cb.answer("Уже открыто.", show_alert=True)

    if idx in game["mines"]:
        del active_games[uid]
        b = InlineKeyboardBuilder()
        for i in range(25):
            if i in game["mines"]: b.button(text="💥", callback_data="ignore")
            elif i in game["opened"]: b.button(text="🟩", callback_data="ignore")
            else: b.button(text="⬜️", callback_data="ignore")
        b.button(text="🔙 В меню", callback_data="games_menu")
        b.adjust(5,5,5,5,5,1)
        info = await get_user_info(uid)
        await safe_edit(cb.message,
            f"💥 **БАБАХ!**\n\nСтавка: {game['bet']} 🎃 сгорела.\n\n🎃 Баланс: **{info['balance'] if info else 0}**.",
            b.as_markup())
        return await cb.answer("Взрыв!")

    game["opened"].append(idx)
    mult = mines_multiplier(len(game["opened"]), game["difficulty"])
    if mult == 0.0:
        del active_games[uid]
        return await cb.answer("Поле исчерпано.", show_alert=True)

    if len(game["opened"]) == 25 - game["difficulty"]:
        win = int(game["bet"] * mult)
        new_bal = await update_balance(uid, None, win)
        del active_games[uid]
        await safe_edit(cb.message,
            f"🎉 **ПОБЕДА!**\n\nВыигрыш: **{win}** 🎃\nБаланс: **{new_bal}**.", games_menu_kb())
        return await cb.answer("Максимум!")

    await safe_edit(cb.message,
        f"💣 **Поле 5х5** (мин: {game['difficulty']})\n💰 Ставка: {game['bet']} 🎃\n"
        f"📈 Множитель: **x{mult:.2f}**\n\nЗабрать сейчас: {int(game['bet']*mult)} 🎃",
        mines_board_kb(game["opened"], mult))
    await cb.answer("Безопасно!")

@dp.callback_query(F.data == "mines_cashout")
async def mines_cashout(cb: types.CallbackQuery):
    uid = cb.from_user.id
    if uid not in active_games or active_games[uid].get("type") != "mines":
        return await cb.answer("Игра не найдена.", show_alert=True)
    game = active_games[uid]
    if not game["opened"]:
        return await cb.answer("Открой хотя бы одну клетку!", show_alert=True)
    del active_games[uid]
    mult = mines_multiplier(len(game["opened"]), game["difficulty"])
    win = int(game["bet"] * mult)
    new_bal = await update_balance(uid, None, win)
    await safe_edit(cb.message,
        f"💰 **Забрано!**\n\nМножитель: **x{mult:.2f}**\nВыигрыш: **{win}** 🎃\nБаланс: **{new_bal}**.",
        games_menu_kb())
    await cb.answer("Успел!")

# ---------- КОЛЕСО ----------
@dp.callback_query(F.data == "game_wheel")
async def wheel(cb: types.CallbackQuery):
    uid = cb.from_user.id
    now = datetime.now()
    async with db_pool.acquire() as c:
        row = await c.fetchrow("SELECT last_played FROM user_wheel WHERE user_id=$1", uid)
    if row and (now - row['last_played']) < timedelta(hours=6):
        rem = timedelta(hours=6) - (now - row['last_played'])
        h, r = divmod(rem.seconds, 3600)
        m, _ = divmod(r, 60)
        return await cb.answer(f"🎡 Жди {h} ч. {m} мин.", show_alert=True)
    info = await get_user_info(uid)
    if not info or info['balance'] < 20:
        return await cb.answer("❌ Нужно 20 тыкв!", show_alert=True)

    await update_balance(uid, None, -20, is_spending=True)
    async with db_pool.acquire() as c:
        await c.execute("INSERT INTO user_wheel (user_id,last_played) VALUES ($1,$2) "
                        "ON CONFLICT (user_id) DO UPDATE SET last_played=$2", uid, now)
    rnd = random.random()
    prize = 50 if rnd < 0.03 else 30 if rnd < 0.10 else 20 if rnd < 0.30 else 0
    if prize > 0:
        new_bal = await update_balance(uid, None, prize)
        text = f"🎡 **Крутится...**\n\n🎉 Выпало: **{prize}** 🎃!\nБаланс: **{new_bal}**."
    else:
        info = await get_user_info(uid)
        text = f"🎡 **Крутится...**\n\n💀 Проигрыш!\nБаланс: **{info['balance'] if info else 0}**."
    await safe_edit(cb.message, text, games_menu_kb())
    await cb.answer()

# ---------- СЛАДОСТЬ / ГАДОСТЬ ----------
@dp.callback_query(F.data == "game_trickortreat")
async def trick(cb: types.CallbackQuery):
    uid = cb.from_user.id
    now = datetime.now()
    async with db_pool.acquire() as c:
        row = await c.fetchrow("SELECT last_played FROM user_trickortreat WHERE user_id=$1", uid)
    if row and (now - row['last_played']) < timedelta(hours=24):
        rem = timedelta(hours=24) - (now - row['last_played'])
        h, r = divmod(rem.seconds, 3600)
        m, _ = divmod(r, 60)
        return await cb.answer(f"🎃 Жди {h} ч. {m} мин.", show_alert=True)
    info = await get_user_info(uid)
    if not info or info['balance'] < 25:
        return await cb.answer("❌ Нужно 25 тыкв!", show_alert=True)

    await update_balance(uid, None, -25, is_spending=True)
    async with db_pool.acquire() as c:
        await c.execute("INSERT INTO user_trickortreat (user_id,last_played) VALUES ($1,$2) "
                        "ON CONFLICT (user_id) DO UPDATE SET last_played=$2", uid, now)
    if random.random() < 0.40:
        new_bal = await update_balance(uid, None, 55)
        text = f"🍬 **Сладость!**\nЧистый выигрыш: +30 🎃\nБаланс: **{new_bal}**."
    else:
        info = await get_user_info(uid)
        text = f"🧟 **Гадость!**\n-25 🎃\nБаланс: **{info['balance'] if info else 0}**."
    await safe_edit(cb.message, text, games_menu_kb())
    await cb.answer()

# ---------- РАКЕТА ----------
@dp.callback_query(F.data == "game_rocket")
async def rocket_menu(cb: types.CallbackQuery):
    await safe_edit(cb.message, "🚀 **Ракета**\nВыбери ставку:", rocket_bet_kb())
    await cb.answer()

@dp.callback_query(F.data == "rocket_bet_custom")
async def rocket_custom(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(RocketGame.bet)
    await safe_edit(cb.message, "✍️ Введи сумму:", cancel_kb("games_menu"))
    await cb.answer()

@dp.message(RocketGame.bet)
async def rocket_input(message: types.Message, state: FSMContext):
    try:
        bet = int(message.text.strip())
        if bet <= 0: raise ValueError
    except:
        return await message.answer("❌ Введи число > 0.")
    info = await get_user_info(message.from_user.id)
    if not info or info['balance'] < bet:
        return await message.answer("❌ Недостаточно тыкв!")
    await state.clear()
    await start_rocket(message.from_user.id, bet, message)

@dp.callback_query(F.data.startswith("rocket_bet_"))
async def rocket_preset(cb: types.CallbackQuery):
    raw = cb.data.replace("rocket_bet_","")
    if raw == "custom": return
    try: bet = int(raw)
    except ValueError: return await cb.answer("Ошибка ставки.", show_alert=True)
    await start_rocket(cb.from_user.id, bet, cb.message, cb)

async def start_rocket(user_id: int, bet: int, message: types.Message, cb: types.CallbackQuery = None):
    if user_id in active_games:
        return await (cb.answer("Уже в игре!", show_alert=True) if cb else message.answer("Уже в игре!"))
    info = await get_user_info(user_id)
    if not info or info['balance'] < bet:
        return await (cb.answer("Мало тыкв!", show_alert=True) if cb else message.answer("Мало тыкв!"))

    game = {"bet": bet, "mult": 1.0, "crash": round(random.uniform(1.01, 1.8), 2),
            "type": "rocket", "crashed": False, "msg": None, "task": None}
    active_games[user_id] = game
    await update_balance(user_id, None, -bet, is_spending=True)

    text = f"🚀 **Взлет!**\nМножитель: **x1.00**\nСтавка: {bet} 🎃"
    if cb:
        await cb.message.edit_text(text, reply_markup=rocket_cashout_kb(), parse_mode="Markdown")
        game["msg"] = cb.message
    else:
        game["msg"] = await message.answer(text, reply_markup=rocket_cashout_kb(), parse_mode="Markdown")

    game["task"] = asyncio.create_task(rocket_loop(user_id, bet, game["crash"], game["msg"]))

async def rocket_loop(user_id: int, bet: int, crash: float, msg: types.Message):
    mult = 1.0
    try:
        while True:
            await asyncio.sleep(1)
            game = active_games.get(user_id)
            if not game or game.get("type") != "rocket" or game.get("crashed"): return
            mult += 0.05
            if mult >= crash:
                game["crashed"] = True
                await asyncio.sleep(0.5)
                try:
                    await bot.edit_message_text(chat_id=user_id, message_id=msg.message_id,
                        text=f"💥 **БУМ!**\nМножитель x{crash:.2f}\nПотеряно: {bet} 🎃", parse_mode="Markdown")
                except: pass
                active_games.pop(user_id, None)
                return
            game["mult"] = mult
            try:
                await bot.edit_message_text(chat_id=user_id, message_id=msg.message_id,
                    text=f"🚀 **Летит!**\nМножитель: **x{mult:.2f}**\nЗабрать: **{int(bet*mult)}** 🎃",
                    reply_markup=rocket_cashout_kb(), parse_mode="Markdown")
            except: pass
    except asyncio.CancelledError: pass
    except Exception as e:
        log.error(f"Rocket: {e}")
        active_games.pop(user_id, None)

@dp.callback_query(F.data == "rocket_cashout")
async def rocket_cashout(cb: types.CallbackQuery):
    uid = cb.from_user.id
    game = active_games.get(uid)
    if not game or game.get("type") != "rocket":
        return await cb.answer("Игра окончена!", show_alert=True)
    active_games.pop(uid, None)
    if game.get("task"): game["task"].cancel()

    win = int(game["bet"] * game["mult"])
    new_bal = await update_balance(uid, None, win)
    await safe_edit(cb.message,
        f"💰 **Забрано!**\nМножитель: x{game['mult']:.2f}\nВыигрыш: **{win}** 🎃\nБаланс: **{new_bal}**.",
        games_menu_kb())
    await cb.answer("Успел!")

# ---------- АПГРЕЙДЕР ----------
@dp.callback_query(F.data == "game_upgrader")
async def upgr_start(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(Upgrader.amount)
    await safe_edit(cb.message, "📈 **Апгрейдер**\nВведи сумму:", cancel_kb("games_menu"))
    await cb.answer()

@dp.message(Upgrader.amount)
async def upgr_input(message: types.Message, state: FSMContext):
    try:
        bet = int(message.text.strip())
        if bet <= 0: raise ValueError
    except:
        return await message.answer("❌ Введи число > 0.")
    info = await get_user_info(message.from_user.id)
    if not info or info['balance'] < bet:
        return await message.answer("❌ Мало тыкв!")
    await state.update_data(bet=bet)
    await message.answer(f"📈 Ставка: {bet} 🎃\nВыбери режим:", reply_markup=upgrader_choice_kb())

async def _upgrader_finish(cb: types.CallbackQuery, state: FSMContext, chance: float, mult_coef: float):
    data = await state.get_data()
    bet = data.get("bet")
    await state.clear()
    if not bet:
        return await cb.answer("Устарело.", show_alert=True)
    await update_balance(cb.from_user.id, None, -bet, is_spending=True)

    if random.random() < chance:
        win = int(bet * mult_coef)
        new_bal = await update_balance(cb.from_user.id, None, win)
        profit = win - bet
        text = f"🎉 Выигрыш: **+{profit}** 🎃 (x{mult_coef})\nБаланс: **{new_bal}**."
    else:
        info = await get_user_info(cb.from_user.id)
        text = f"💀 Проигрыш: **-{bet}** 🎃\nБаланс: **{info['balance'] if info else 0}**."

    b = InlineKeyboardBuilder()
    b.button(text="🔄 Ещё", callback_data="game_upgrader")
    b.button(text="🔙 В игры", callback_data="games_menu")
    b.adjust(1)
    await safe_edit(cb.message, text, b.as_markup())
    await cb.answer()

@dp.callback_query(F.data == "upgrade_fast")
async def upgr_fast(cb: types.CallbackQuery, state: FSMContext):
    await _upgrader_finish(cb, state, 0.25, 2.2)

@dp.callback_query(F.data == "upgrade_slow")
async def upgr_slow(cb: types.CallbackQuery, state: FSMContext):
    await _upgrader_finish(cb, state, 0.45, 1.6)

# ---------- ПЕРЕВОДЫ ----------
@dp.callback_query(F.data == "transfer_start")
async def transfer_start(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(TransferPumpkins.target)
    await safe_edit(cb.message, "💸 Введи **ID** или **@username** получателя:", cancel_kb("balance_menu"))
    await cb.answer()

@dp.message(TransferPumpkins.target)
async def transfer_target(message: types.Message, state: FSMContext):
    target = await find_user_id(message.text)
    if not target:
        return await message.answer("❌ Не найден.")
    if target == message.from_user.id:
        return await message.answer("❌ Себе нельзя.")
    info = await get_user_info(target)
    if not info:
        return await message.answer("❌ Пользователь не зарегистрирован в боте.")
    await state.update_data(target=target)
    await state.set_state(TransferPumpkins.amount)
    await message.answer(f"Сколько перевести `{target}`?", reply_markup=cancel_kb("balance_menu"), parse_mode="Markdown")

@dp.message(TransferPumpkins.amount)
async def transfer_amount(message: types.Message, state: FSMContext):
    try:
        amt = int(message.text.strip())
    except:
        return await message.answer("Число нужно.")
    if amt <= 0:
        return await message.answer("Больше 0.")

    target = (await state.get_data()).get("target")
    await state.clear()
    if not target:
        return await message.answer("Получатель потерян. Начни заново.")
    info = await get_user_info(message.from_user.id)
    if not info or info['balance'] < amt:
        return await message.answer("Мало тыкв!")

    await update_balance(message.from_user.id, None, -amt, is_spending=True)
    await update_balance(target, None, amt)
    await message.answer(f"✅ Переведено **{amt}** 🎃 пользователю `{target}`.",
                         reply_markup=main_menu_kb(await is_admin_db(message.from_user.id)),
                         parse_mode="Markdown")
    try:
        await bot.send_message(target, f"🎁 Тебе перевели **{amt}** 🎃!", parse_mode="Markdown")
    except:
        pass
        # ---------- МАГАЗИН ----------
@dp.callback_query(F.data == "shop_menu")
async def show_shop(cb: types.CallbackQuery):
    await safe_edit(cb.message, "🛒 **Магазин**\n\nЦены обновлены 🎃", shop_kb(cb.from_user.id))
    await cb.answer()

@dp.callback_query(F.data.startswith("buy_item_"))
async def buy_item(cb: types.CallbackQuery):
    item_id = cb.data.replace("buy_item_","")
    item = SHOP_ITEMS.get(item_id)
    if not item:
        return await cb.answer("Нет товара.", show_alert=True)

    async with db_pool.acquire() as c:
        row = await c.fetchrow("SELECT count FROM user_purchases WHERE user_id=$1 AND item_id=$2",
                               cb.from_user.id, item_id)
    if (row['count'] if row else 0) >= item["limit"]:
        return await cb.answer("Лимит исчерпан!", show_alert=True)

    info = await get_user_info(cb.from_user.id)
    if not info or info['balance'] < item["price"]:
        return await cb.answer("Мало тыкв!", show_alert=True)

    await update_balance(cb.from_user.id, None, -item["price"], is_spending=True)
    async with db_pool.acquire() as c:
        await c.execute("INSERT INTO user_purchases (user_id,item_id,count) VALUES ($1,$2,1) "
                        "ON CONFLICT (user_id,item_id) DO UPDATE SET count=user_purchases.count+1",
                        cb.from_user.id, item_id)

    await safe_edit(cb.message, f"✅ Куплено **{item['name']}** за {item['price']} 🎃!",
                    main_menu_kb(await is_admin_db(cb.from_user.id)))
    await notify_admins(f"🛒 {cb.from_user.id} купил {item['name']} за {item['price']} 🎃")
    await cb.answer("Куплено!")

# ---------- STARS ----------
@dp.callback_query(F.data == "buy_stars_menu")
async def buy_stars_menu(cb: types.CallbackQuery):
    await safe_edit(cb.message, "💎 **Звезды** (1🎃=10⭐)\nМин: 100🎃", buy_stars_kb())
    await cb.answer()

@dp.callback_query(F.data == "buy_stars_custom")
async def buy_stars_custom(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(BuyStars.custom_amount)
    await safe_edit(cb.message, "✍️ Введи кол-во тыкв (100–10000):", cancel_kb("balance_menu"))
    await cb.answer()

@dp.message(BuyStars.custom_amount)
async def buy_stars_amt(message: types.Message, state: FSMContext):
    try:
        p = int(message.text.strip())
    except:
        return await message.answer("Число.")
    if p < 100 or p > 10000:
        return await message.answer("Мин 100, макс 10000.")
    await state.clear()
    await message.answer_invoice(title="Тыквы", description=f"{p} тыкв",
        prices=[LabeledPrice(label="XTR", amount=p*10)],
        provider_token="", currency="XTR", payload=f"stars_{p}")

@dp.callback_query(F.data.startswith("buy_stars_"))
async def buy_stars_preset(cb: types.CallbackQuery):
    raw = cb.data.replace("buy_stars_","")
    if raw in ("custom","menu"): return
    try:
        p = int(raw)
    except ValueError:
        return await cb.answer("Ошибка.", show_alert=True)
    await cb.message.answer_invoice(title="Тыквы", description=f"{p} тыкв",
        prices=[LabeledPrice(label="XTR", amount=p*10)],
        provider_token="", currency="XTR", payload=f"stars_{p}")
    await cb.answer()

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

@dp.message(F.successful_payment)
async def success_pay(message: types.Message):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("stars_"):
        p = int(payload.split("_")[1])
        new_bal = await update_balance(message.from_user.id, None, p)
        await message.answer(f"🎉 +{p} 🎃\nБаланс: **{new_bal}**",
            reply_markup=main_menu_kb(await is_admin_db(message.from_user.id)),
            parse_mode="Markdown")

# ---------- КВЕСТЫ ----------
@dp.callback_query(F.data == "quest_menu")
async def show_quests(cb: types.CallbackQuery):
    active = await check_and_rotate_quests()
    async with db_pool.acquire() as c:
        last_rot = await c.fetchval("SELECT value FROM settings WHERE key='last_rotation'")
        rows = await c.fetch("SELECT quest_id FROM user_quest_completions WHERE user_id=$1 AND completed_at>$2",
                             cb.from_user.id, datetime.fromisoformat(last_rot))
    completed = [r['quest_id'] for r in rows]
    await safe_edit(cb.message, "🕸️ **Квесты** (обнов. каждые 12ч)",
                    quests_kb(cb.from_user.id, active, completed))
    await cb.answer()

@dp.callback_query(F.data.startswith("quest_"))
async def handle_quest(cb: types.CallbackQuery):
    raw = cb.data.replace("quest_","")
    if raw == "menu": return
    q = QUEST_POOL.get(raw)
    if not q:
        return await cb.answer("Устарел.", show_alert=True)
    await safe_edit(cb.message, f"🕷️ **{q['name']}**\n{q['desc']}\n💰 {q['reward']} 🎃", proof_kb(raw))
    await cb.answer()

@dp.callback_query(F.data.startswith("proof_sent_"))
async def proof_sent(cb: types.CallbackQuery):
    qk = cb.data.replace("proof_sent_","")
    q = QUEST_POOL.get(qk)
    if not q:
        return await cb.answer("Ошибка.", show_alert=True)
    uid = cb.from_user.id
    async with db_pool.acquire() as c:
        if await c.fetchrow("SELECT 1 FROM quest_proofs WHERE user_id=$1 AND quest_name=$2 AND status='pending'",
                            uid, q["name"]):
            return await cb.answer("Ты уже отправил эту заявку.", show_alert=True)
        last_rot = await c.fetchval("SELECT value FROM settings WHERE key='last_rotation'")
        if last_rot and await c.fetchrow(
                "SELECT 1 FROM user_quest_completions WHERE user_id=$1 AND quest_id=$2 AND completed_at>$3",
                uid, qk, datetime.fromisoformat(last_rot)):
            return await cb.answer("Ты уже выполнил этот квест!", show_alert=True)

    await save_proof(uid, qk, q["name"])
    await safe_edit(cb.message, "✅ Заявка отправлена на проверку!", games_menu_kb())
    await cb.answer("Отправлено!")

# ---------- АДМИН ПАНЕЛЬ ----------
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    await safe_edit(cb.message, "💀 **Панель Мастера**", admin_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_users_count")
async def admin_users_count(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    count = await get_total_users_count()
    await safe_edit(cb.message, f"👥 **Статистика**\n\nВсего пользователей: **{count}**", admin_kb())
    await cb.answer()

# ---------- РАССЫЛКА (ФИКС) ----------
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    await state.set_state(Broadcast.text)
    await safe_edit(cb.message, "✉️ **Рассылка**\n\nОтправь текст сообщения:", cancel_kb())
    await cb.answer()

@dp.message(Broadcast.text)
async def admin_broadcast_text(message: types.Message, state: FSMContext):
    await state.update_data(bc_text=message.text)
    await state.set_state(Broadcast.confirm)
    await message.answer(f"📝 **Предпросмотр:**\n\n{message.text}\n\nОтправить всем?",
                         reply_markup=broadcast_confirm_kb(), parse_mode="Markdown")

@dp.callback_query(F.data == "broadcast_confirm_yes")
async def admin_broadcast_confirm(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)

    text = (await state.get_data()).get("bc_text")
    await state.clear()
    if not text:
        return await cb.answer("Текст потерян.", show_alert=True)

    users = await get_all_users()
    await safe_edit(cb.message, f"🚀 Рассылка на {len(users)} пользователей...")
    await cb.answer()

    ok = blocked = failed = 0
    errors = []

    for uid in users:
        sent = False

        # Попытка 1: с Markdown
        try:
            await bot.send_message(uid, text, parse_mode="Markdown")
            ok += 1
            sent = True
        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ("blocked", "chat not found", "deactivated", "user is deactivated")):
                blocked += 1
                sent = True
            else:
                errors.append(f"{uid}: {e}")

        # Попытка 2: без разметки
        if not sent:
            try:
                await bot.send_message(uid, text)
                ok += 1
            except Exception as e2:
                failed += 1
                errors.append(f"{uid} (fallback): {e2}")

        await asyncio.sleep(0.05)

    err_preview = "\n".join(errors[:5]) if errors else "—"
    try:
        await bot.send_message(
            cb.from_user.id,
            f"✅ **Рассылка завершена**\n\n"
            f"📨 Доставлено: **{ok}**\n"
            f"🚫 Заблокировали: **{blocked}**\n"
            f"⚠️ Ошибок: **{failed}**\n\n"
            f"**Первые ошибки:**\n`{err_preview}`",
            reply_markup=admin_kb(), parse_mode="Markdown"
        )
    except Exception:
        await bot.send_message(
            cb.from_user.id,
            f"Рассылка завершена.\nДоставлено: {ok}\nЗаблокировали: {blocked}\nОшибок: {failed}\n\n"
            f"Первые ошибки:\n{err_preview}",
            reply_markup=admin_kb()
        )

# ---------- СОЗДАНИЕ ПРОМО ----------
@dp.callback_query(F.data == "admin_create_promo")
async def admin_create_promo_start(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    await state.set_state(PromoCreate.name)
    await safe_edit(cb.message, "🎟️ Введи название промокода:", cancel_kb())
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
    async with db_pool.acquire() as c:
        if await c.fetchrow("SELECT 1 FROM promocodes WHERE code=$1", name):
            return await message.answer(f"❌ Промокод `{name}` уже существует!",
                                        reply_markup=admin_kb(), parse_mode="Markdown")
        await c.execute("INSERT INTO promocodes (code,reward,limit_uses) VALUES ($1,$2,$3)",
                        name, reward, l)
    await message.answer(f"✅ Промокод `{name}` создан!\n+{reward} 🎃\nЛимит: {l}",
                         reply_markup=admin_kb(), parse_mode="Markdown")

# ---------- ЗАЯВКИ ----------
@dp.callback_query(F.data == "admin_pending")
async def admin_pending(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    rows = await get_pending(10)
    if not rows:
        return await safe_edit(cb.message, "📜 Заявок нет.", admin_kb())
    await safe_edit(cb.message, "📜 **Активные заявки:**", admin_kb())
    for row in rows:
        b = InlineKeyboardBuilder()
        b.button(text="✅ Выдать", callback_data=f"admin_approve_{row['id']}")
        b.button(text="❌ Отклонить", callback_data=f"admin_reject_{row['id']}")
        b.adjust(2)
        await bot.send_message(cb.from_user.id,
            f"📜 **#{row['id']}**\n👤 `{row['user_id']}`\n🎯 {row['quest_name']}",
            reply_markup=b.as_markup(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data.startswith("admin_approve_"))
async def admin_approve(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    pid = int(cb.data.replace("admin_approve_",""))
    async with db_pool.acquire() as c:
        row = await c.fetchrow("SELECT user_id,quest_id,quest_name,status FROM quest_proofs WHERE id=$1", pid)
    if not row or row['status'] != 'pending':
        return await cb.answer("Обработана.", show_alert=True)
    quest_id = row['quest_id']
    reward = QUEST_POOL.get(quest_id, {}).get("reward", 0)
    if not reward:
        reward = next((q["reward"] for q in QUEST_POOL.values() if q["name"] == row['quest_name']), 0)
    async with db_pool.acquire() as c:
        await c.execute("DELETE FROM quest_proofs WHERE id=$1", pid)
        await c.execute("INSERT INTO user_quest_completions (user_id,quest_id) VALUES ($1,$2)",
                        row['user_id'], quest_id)
    new_bal = await update_balance(row['user_id'], None, reward)
    await safe_edit(cb.message, f"✅ Выдано {reward} 🎃 пользователю `{row['user_id']}`.")
    await cb.answer("Готово!")
    try:
        await bot.send_message(row['user_id'],
            f"🎉 Квест **{row['quest_name']}** подтвержден!\n+{reward} 🎃\nБаланс: **{new_bal}**.",
            parse_mode="Markdown")
    except:
        pass

@dp.callback_query(F.data.startswith("admin_reject_"))
async def admin_reject(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    pid = int(cb.data.replace("admin_reject_",""))
    async with db_pool.acquire() as c:
        row = await c.fetchrow("SELECT user_id,quest_name,status FROM quest_proofs WHERE id=$1", pid)
    if not row or row['status'] != 'pending':
        return await cb.answer("Обработана.", show_alert=True)
    async with db_pool.acquire() as c:
        await c.execute("DELETE FROM quest_proofs WHERE id=$1", pid)
    await safe_edit(cb.message, f"❌ Заявка #{pid} отклонена.")
    await cb.answer("Отклонено.")
    try:
        await bot.send_message(row['user_id'], f"❌ Квест **{row['quest_name']}** отклонен.")
    except:
        pass

# ---------- АДМИН: БАЛАНС / ПРОФИЛЬ ----------
@dp.callback_query(F.data == "admin_check_bal")
async def admin_check_bal(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    await state.set_state(AdminViewBalance.target)
    await safe_edit(cb.message, "👁 Введи ID/username:", cancel_kb())
    await cb.answer()

@dp.message(AdminViewBalance.target)
async def admin_check_bal_input(message: types.Message, state: FSMContext):
    t = await find_user_id(message.text)
    if not t:
        return await message.answer("Не найден.")
    await state.clear()
    info = await get_user_info(t)
    await message.answer(f"👁 Баланс `{t}`: **{info['balance'] if info else 0}** 🎃",
                         reply_markup=admin_kb(), parse_mode="Markdown")

@dp.callback_query(F.data == "admin_check_profile")
async def admin_check_profile_start(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    await state.set_state(AdminViewProfile.target)
    await safe_edit(cb.message, "👤 Введи ID или @username:", cancel_kb())
    await cb.answer()

@dp.message(AdminViewProfile.target)
async def admin_check_profile_input(message: types.Message, state: FSMContext):
    t = await find_user_id(message.text)
    if not t:
        return await message.answer("❌ Не найден.")
    await state.clear()
    info = await get_user_info(t)
    if not info:
        return await message.answer("❌ Нет в базе.")
    level, title = get_level_info(info['spent'])
    await message.answer(
        f"📊 **Профиль игрока**\n\n👤 @{info['username'] or 'скрыт'}\n🆔 `{t}`\n\n"
        f"🎃 Баланс: **{info['balance']}**\n💸 Потрачено: **{info['spent']}**\n📈 Уровень: **{level}** ({title})",
        reply_markup=admin_kb(), parse_mode="Markdown")

# ---------- АДМИН: ВЫДАТЬ АДМИНКУ ----------
@dp.callback_query(F.data == "admin_grant")
async def admin_grant(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    await state.set_state(GrantAdmin.target_id)
    await safe_edit(cb.message, "👑 ID/username нового админа:", cancel_kb())
    await cb.answer()

@dp.message(GrantAdmin.target_id)
async def admin_grant_input(message: types.Message, state: FSMContext):
    t = await find_user_id(message.text)
    if not t:
        return await message.answer("Не найден.")
    await state.clear()
    async with db_pool.acquire() as c:
        await c.execute("INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING", t)
    await message.answer(f"✅ `{t}` теперь Мастер!", reply_markup=admin_kb(), parse_mode="Markdown")

# ---------- АДМИН: ВЫДАТЬ / ОТОБРАТЬ ----------
@dp.callback_query(F.data == "admin_give")
async def admin_give(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    await state.update_data(action="give")
    await state.set_state(AdminAmount.target_id)
    await safe_edit(cb.message, "🪄 Кому (ID/username):", cancel_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_take")
async def admin_take(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    await state.update_data(action="take")
    await state.set_state(AdminAmount.target_id)
    await safe_edit(cb.message, "🪓 У кого (ID/username):", cancel_kb())
    await cb.answer()

@dp.message(AdminAmount.target_id)
async def admin_target_input(message: types.Message, state: FSMContext):
    t = await find_user_id(message.text)
    if not t:
        return await message.answer("Не найден.")
    await state.update_data(target=t)
    await state.set_state(AdminAmount.amount)
    await message.answer("Сколько тыкв?", reply_markup=cancel_kb())

@dp.message(AdminAmount.amount)
async def admin_amt_input(message: types.Message, state: FSMContext):
    try:
        a = abs(int(message.text.strip()))
    except:
        return await message.answer("Число.")
    d = await state.get_data()
    await state.clear()
    final = a if d["action"] == "give" else -a
    old_info = await get_user_info(d["target"])
    old_bal = old_info['balance'] if old_info else 0
    new = await update_balance(d["target"], None, final, is_spending=(d["action"] == "take"))
    actual = new - old_bal
    await message.answer(f"✅ Готово. Баланс: {new} 🎃", reply_markup=admin_kb())
    try:
        if actual > 0:
            await bot.send_message(d["target"], f"🎁 Твой баланс пополнен на {actual} 🎃.")
        elif actual < 0:
            await bot.send_message(d["target"], f"🪓 С твоего баланса списано {-actual} 🎃.")
    except:
        pass

# ---------- АДМИН: ОТОБРАТЬ ЛВЛ ----------
@dp.callback_query(F.data == "admin_take_level")
async def admin_take_level_start(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    await state.set_state(AdminTakeLevel.target_id)
    await safe_edit(cb.message, "🪓 У кого отобрать уровни?", cancel_kb())
    await cb.answer()

@dp.message(AdminTakeLevel.target_id)
async def admin_take_level_input(message: types.Message, state: FSMContext):
    t = await find_user_id(message.text)
    if not t:
        return await message.answer("Не найден.")
    await state.update_data(target=t)
    await state.set_state(AdminTakeLevel.amount)
    await message.answer("Сколько опыта отнять?", reply_markup=cancel_kb())

@dp.message(AdminTakeLevel.amount)
async def admin_take_level_amt(message: types.Message, state: FSMContext):
    try:
        a = int(message.text.strip())
    except:
        return await message.answer("Число.")
    if a <= 0:
        return await message.answer("Больше 0.")
    t = (await state.get_data()).get("target")
    await state.clear()
    new_spent = await admin_take_level(t, a)
    new_level, _ = get_level_info(new_spent)
    await message.answer(f"✅ Уровень `{t}` теперь **{new_level}** (Опыт: {new_spent})",
                         reply_markup=admin_kb(), parse_mode="Markdown")
    try:
        await bot.send_message(t, f"Твой уровень понижен до {new_level}.")
    except:
        pass

@dp.callback_query(F.data == "admin_take_level_all")
async def admin_take_level_all_start(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    await state.set_state(AdminTakeLevelAll.amount)
    await safe_edit(cb.message, "🪓 Сколько опыта отнять У ВСЕХ?", cancel_kb())
    await cb.answer()

@dp.message(AdminTakeLevelAll.amount)
async def admin_take_level_all_input(message: types.Message, state: FSMContext):
    try:
        a = abs(int(message.text.strip()))
    except:
        return await message.answer("Число.")
    if a == 0:
        return await message.answer("Больше 0.")
    await state.clear()
    users = await get_all_users()
    await message.answer(f"🚀 Списание у {len(users)} пользователей...")
    for u in users:
        await admin_take_level(u, a)
        await asyncio.sleep(0.05)
    await message.answer(f"✅ Готово! Обработано {len(users)}.", reply_markup=admin_kb())

# ---------- АДМИН: ВСЕМ ----------
@dp.callback_query(F.data == "admin_give_all")
async def admin_give_all(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    await state.set_state(AdminGiveAll.amount)
    await safe_edit(cb.message, "🪄 Сколько начислить ВСЕМ?", cancel_kb())
    await cb.answer()

@dp.message(AdminGiveAll.amount)
async def admin_give_all_input(message: types.Message, state: FSMContext):
    try:
        a = int(message.text.strip())
    except:
        return await message.answer("Число.")
    if a <= 0:
        return await message.answer(">0.")
    await state.clear()
    users = await get_all_users()
    await message.answer(f"🚀 Начисляю {a} для {len(users)}...")
    for u in users:
        await update_balance(u, None, a)
        try:
            await bot.send_message(u, f"🎁 Дар! +{a} 🎃")
        except:
            pass
        await asyncio.sleep(0.05)
    await message.answer("✅ Готово!", reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_take_all")
async def admin_take_all(cb: types.CallbackQuery, state: FSMContext):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    await state.set_state(AdminTakeAll.amount)
    await safe_edit(cb.message, "🪓 Сколько отобрать У ВСЕХ?", cancel_kb())
    await cb.answer()

@dp.message(AdminTakeAll.amount)
async def admin_take_all_input(message: types.Message, state: FSMContext):
    try:
        a = abs(int(message.text.strip()))
    except:
        return await message.answer("Число.")
    if a == 0:
        return await message.answer("Больше 0.")
    await state.clear()
    users = await get_all_users()
    await message.answer(f"🚀 Списание {a} у {len(users)}...")
    for u in users:
        info = await get_user_info(u)
        actual = min(info['balance'], a) if info else 0
        if actual > 0:
            await update_balance(u, None, -actual, is_spending=True)
            try:
                await bot.send_message(u, f"🪓 Гнев Мастера! -{actual} 🎃")
            except:
                pass
        await asyncio.sleep(0.05)
    await message.answer("✅ Готово!", reply_markup=admin_kb())

# ---------- АДМИН: ПОДАРКИ ----------
@dp.callback_query(F.data == "admin_gift_all")
async def admin_gift_all(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    await safe_edit(cb.message, "🎁 Выбери подарок:", admin_gift_choice_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("send_gift_"))
async def send_gift(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    gid = cb.data.replace("send_gift_","")
    users = await get_all_users()
    await safe_edit(cb.message, f"🚀 Рассылка подарка для {len(users)}...")
    await cb.answer()

    if not hasattr(bot, "send_gift"):
        return await bot.send_message(cb.from_user.id,
            "❌ В этой версии aiogram нет метода `send_gift`. Обнови aiogram до 3.13+.",
            reply_markup=admin_kb())

    s = f = 0
    for u in users:
        try:
            await bot.send_gift(user_id=u, gift_id=gid)
            s += 1
        except Exception as e:
            log.warning(f"Не удалось отправить подарок {u}: {e}")
            f += 1
        await asyncio.sleep(0.3)

    await bot.send_message(cb.from_user.id, f"✅ Отправлено: {s}\nОшибок: {f}", reply_markup=admin_kb())

# ---------- АДМИН: WIPE ----------
@dp.callback_query(F.data == "admin_wipe_db")
async def admin_wipe_db(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    b = InlineKeyboardBuilder()
    b.button(text="⚠️ ДА, СНЕСТИ ВСЁ", callback_data="admin_wipe_db_confirm")
    b.button(text="❌ Отмена", callback_data="admin_cancel")
    b.adjust(1)
    await safe_edit(cb.message, "💣 **ВНИМАНИЕ!**\n\nБаза данных будет полностью удалена. Ты уверен?",
                    b.as_markup())
    await cb.answer()

@dp.callback_query(F.data == "admin_wipe_db_confirm")
async def admin_wipe_db_confirm(cb: types.CallbackQuery):
    if not await is_admin_db(cb.from_user.id):
        return await cb.answer("Нет.", show_alert=True)
    await safe_edit(cb.message, "💣 **Уничтожаем базу...**")
    try:
        await wipe_database()
        await safe_edit(cb.message, "✅ **БАЗА СНЕСЕНА!**", admin_kb())
    except Exception as e:
        log.error(f"Wipe: {e}")
        await safe_edit(cb.message, f"❌ Ошибка: {e}", admin_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_cancel")
async def admin_cancel(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(cb.message, "💀 Панель:", admin_kb())
    await cb.answer("Отменено")

# ---------- СЕРВЕР / MAIN ----------
async def healthcheck(request):
    return web.Response(text="OK")

async def root(request):
    return web.Response(text="Bot is running")

async def main():
    await init_db()
    app = web.Application()
    app.router.add_get("/healthcheck", healthcheck)
    app.router.add_get("/", root)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
    await site.start()
    log.info("Бот запущен!")
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
