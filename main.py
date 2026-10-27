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
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web

# --- КОНФИГУРАЦИЯ ---
TOKEN = os.environ.get("BOT_TOKEN", "")
API_BASE = os.environ.get("API_BASE", "https://misugram.cloud-ip.cc")
ADMIN_USERNAME = "sardelka"
ADMIN_IDS = {1780243203}  # твой ID в клиенте

DB_FILE = "halloween_bot.db"

def is_admin(user: types.User) -> bool:
    if user is None:
        return False
    return user.id in ADMIN_IDS or (user.username or "").lower() == ADMIN_USERNAME.lower()

# --- БАЗА ---
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
        cur.execute(
            "UPDATE users SET balance = ?, username = ? WHERE user_id = ?",
            (new_balance, uname, user_id),
        )
    else:
        new_balance = amount
        cur.execute(
            "INSERT INTO users (user_id, username, balance) VALUES (?, ?, ?)",
            (user_id, username, new_balance),
        )
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
    cur.execute(
        "SELECT user_id, username, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT ?",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def get_pending(limit: int = 20):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, quest_name, created_at FROM quest_proofs "
        "WHERE status='pending' ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows

# --- FSM ---
class GivePumpkin(StatesGroup):
    target_id = State()
    amount = State()

# --- КЛАВИАТУРЫ ---
def main_menu_kb(user: types.User) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Квест 🎃", callback_data="quest_menu")
    b.button(text="Баланс 🎃", callback_data="show_balance")
    b.button(text="Топ 🎃", callback_data="show_top")
    if is_admin(user):
        b.button(text="⚙️ Админ-панель", callback_data="admin_panel")
    b.adjust(2)
    return b.as_markup()

def quests_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎁 Раздай 5 подарков (+25 монет)", callback_data="quest_gifts")
    b.button(text="📸 Фото с тыквой (+15 монет)", callback_data="quest_pumpkin")
    b.button(text="🔙 В меню", callback_data="back_to_main")
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
    b.button(text="🎃 Выдать тыквы", callback_data="admin_give")
    b.button(text="📋 Заявки (pending)", callback_data="admin_pending")
    b.button(text="🔙 В меню", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def cancel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="admin_cancel")
    b.adjust(1)
    return b.as_markup()

# --- БОТ ---
logging.basicConfig(level=logging.INFO)
session = AiohttpSession(api=TelegramAPIServer.from_base(API_BASE))
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())

QUESTS = {
    "quest_gifts": {
        "name": "Раздай 5 подарков",
        "reward": 25,
        "desc": "Раздай 5 подарков друзьям или в чатах.",
    },
    "quest_pumpkin": {
        "name": "Фото с тыквой",
        "reward": 15,
        "desc": "Сделай фото с тыквой и отправь его организатору.",
    },
}

# --- ХЭНДЛЕРЫ ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    update_balance(
        message.from_user.id,
        message.from_user.username or message.from_user.full_name,
        0,
    )
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n"
        f"Добро пожаловать в Хэллоуин-бота! 🎃\n\n"
        f"Зарабатывай тыквы 🎃, выполняя квесты.",
        reply_markup=main_menu_kb(message.from_user),
    )

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Главное меню:", reply_markup=main_menu_kb(cb.from_user))
    await cb.answer()

@dp.callback_query(F.data == "show_balance")
async def show_balance(cb: types.CallbackQuery):
    balance = get_balance(cb.from_user.id)
    await cb.message.edit_text(
        f"🎃 Твой баланс: **{balance}** тыкв.",
        reply_markup=main_menu_kb(cb.from_user),
        parse_mode="Markdown",
    )
    await cb.answer()

@dp.callback_query(F.data == "show_top")
async def show_top(cb: types.CallbackQuery):
    rows = get_top(10)
    if not rows:
        text = "Пока никого нет в топе. Стань первым! 🎃"
    else:
        medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 20
        text = "🏆 **Топ по тыквам** 🎃\n\n"
        for i, (uid, uname, bal) in enumerate(rows):
            name = f"@{uname}" if uname else f"id {uid}"
            text += f"{medals[i]} {name} — {bal} 🎃\n"
    await cb.message.edit_text(
        text, reply_markup=main_menu_kb(cb.from_user), parse_mode="Markdown"
    )
    await cb.answer()

@dp.callback_query(F.data == "quest_menu")
async def show_quests(cb: types.CallbackQuery):
    await cb.message.edit_text(
        "🎃 Доступные квесты:\n\nВыполни задание и получи награду!",
        reply_markup=quests_kb(),
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("quest_") & ~F.data.startswith("quest_menu"))
async def handle_quest(cb: types.CallbackQuery):
    quest = QUESTS.get(cb.data)
    if not quest:
        await cb.answer("Неизвестный квест.", show_alert=True)
        return
    await cb.message.edit_text(
        f"🎃 Квест: **{quest['name']}**\n\n"
        f"{quest['desc']}\n\n"
        f"💰 Награда: **{quest['reward']} тыкв**\n\n"
        f"📩 Доказательство — @{ADMIN_USERNAME}",
        reply_markup=proof_kb(cb.data),
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
    await cb.message.edit_text(
        f"✅ Заявка на **{quest['name']}** отправлена!\n"
        f"Организатор @{ADMIN_USERNAME} проверит её.",
        parse_mode="Markdown",
    )
    await cb.answer("Заявка отправлена!")

# --- АДМИН-ПАНЕЛЬ ---
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: types.CallbackQuery):
    if not is_admin(cb.from_user):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    await cb.message.edit_text("⚙️ Админ-панель:", reply_markup=admin_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_pending")
async def admin_pending(cb: types.CallbackQuery):
    if not is_admin(cb.from_user):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    rows = get_pending(20)
    if not rows:
        text = "Нет заявок в ожидании."
    else:
        text = "🎃 Заявки на проверку:\n\n"
        for pid, uid, qname, ts in rows:
            text += f"#{pid} · user_id={uid} · {qname}\n"
        text += "\nКоманды:\n/approve <id>\n/reject <id>"
    await cb.message.edit_text(text, reply_markup=admin_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_give")
async def admin_give_start(cb: types.CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(GivePumpkin.target_id)
    await cb.message.edit_text(
        "🎃 Введи **ID пользователя**, которому нужно выдать тыквы:",
        parse_mode="Markdown",
        reply_markup=cancel_kb(),
    )
    await cb.answer()

@dp.callback_query(F.data == "admin_cancel")
async def admin_cancel(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("⚙️ Админ-панель:", reply_markup=admin_kb())
    await cb.answer("Отменено")

@dp.message(GivePumpkin.target_id)
async def give_target(message: types.Message, state: FSMContext):
    try:
        target = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("Нужен числовой ID. Попробуй ещё раз.")
        return
    await state.update_data(target=target)
    await state.set_state(GivePumpkin.amount)
    await message.answer(
        f"Сколько тыкв 🎃 выдать пользователю `{target}`?",
        parse_mode="Markdown",
        reply_markup=cancel_kb(),
    )

@dp.message(GivePumpkin.amount)
async def give_amount(message: types.Message, state: FSMContext):
    try:
        amount = int(message.text.strip())
        if amount == 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Нужно целое число (не 0).")
        return
    data = await state.get_data()
    target = data["target"]
    await state.clear()
    new_balance = update_balance(target, "", amount)
    await message.answer(
        f"✅ Выдано **{amount}** 🎃 пользователю `{target}`.\nЕго баланс: **{new_balance}**.",
        parse_mode="Markdown",
    )
    try:
        sign = "начислено" if amount > 0 else "списано"
        await bot.send_message(
            target,
            f"🎃 Тебе {sign} **{abs(amount)}** тыкв!\nБаланс: **{new_balance}**.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logging.warning(f"Не смог уведомить {target}: {e}")

# --- КОМАНДЫ МОДЕРАЦИИ ---
@dp.message(F.text == "/top")
async def cmd_top(message: types.Message):
    rows = get_top(10)
    if not rows:
        await message.answer("Топ пуст.")
        return
    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 20
    text = "🏆 **Топ по тыквам** 🎃\n\n"
    for i, (uid, uname, bal) in enumerate(rows):
        name = f"@{uname}" if uname else f"id {uid}"
        text += f"{medals[i]} {name} — {bal} 🎃\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "/pending")
async def cmd_pending(message: types.Message):
    if not is_admin(message.from_user):
        return
    rows = get_pending(30)
    if not rows:
        await message.answer("Нет заявок в ожидании.")
        return
    text = "🎃 Заявки на проверку:\n\n"
    for pid, uid, qname, ts in rows:
        text += f"#{pid} · user_id={uid} · {qname}\n"
    text += "\n/approve <id> или /reject <id>"
    await message.answer(text)

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
        await message.answer("Заявка не найдена.")
        return
    uid, qname, status = row
    if status != "pending":
        conn.close()
        await message.answer(f"Заявка уже {status}.")
        return
    reward = next((q["reward"] for q in QUESTS.values() if q["name"] == qname), 0)
    cur.execute("UPDATE quest_proofs SET status='approved' WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    new_balance = update_balance(uid, "", reward)
    try:
        await bot.send_message(
            uid,
            f"🎉 Квест **{qname}** подтверждён! +{reward} 🎃\nБаланс: {new_balance}",
            parse_mode="Markdown",
        )
    except Exception:
        pass
    await message.answer(f"✅ Заявка #{pid} подтверждена, начислено {reward}.")

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
        await message.answer("Заявка не найдена.")
        return
    uid, qname, status = row
    if status != "pending":
        conn.close()
        await message.answer(f"Заявка уже {status}.")
        return
    cur.execute("UPDATE quest_proofs SET status='rejected' WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    try:
        await bot.send_message(
            uid,
            f"❌ Заявка на **{qname}** отклонена. Напиши @{ADMIN_USERNAME}.",
            parse_mode="Markdown",
        )
    except Exception:
        pass
    await message.answer(f"Заявка #{pid} отклонена.")

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
