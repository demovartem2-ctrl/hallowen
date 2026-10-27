import asyncio
import logging
import os
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web

# --- КОНФИГУРАЦИЯ ---
TOKEN = os.environ.get("BOT_TOKEN", "")  # обязательно задай в env
API_BASE = os.environ.get("API_BASE", "https://misugram.cloud-ip.cc")
ADMIN_USERNAME = "sardelka"

# --- БАЗА ДАННЫХ ---
DB_FILE = "halloween_bot.db"

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
    cur.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row:
        new_balance = row[0] + amount
        cur.execute(
            "UPDATE users SET balance = ?, username = ? WHERE user_id = ?",
            (new_balance, username, user_id),
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

# --- КЛАВИАТУРЫ ---
def main_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Квест 🎃", callback_data="quest_menu")
    b.button(text="Баланс 🎃", callback_data="show_balance")
    b.adjust(2)
    return b.as_markup()

def quests_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎁 Раздай 5 подарков (+25 монет)", callback_data="quest_gifts")
    b.button(text="📸 Фото с тыквой (+15 монет)", callback_data="quest_pumpkin")
    b.button(text="🔙 Назад в меню", callback_data="back_to_main")
    b.adjust(1)
    return b.as_markup()

def proof_kb(quest_key: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(
        text=f"📩 Написать @{ADMIN_USERNAME}",
        url=f"https://t.me/{ADMIN_USERNAME}",
    )
    b.button(text="✅ Я отправил доказательство", callback_data=f"proof_sent_{quest_key}")
    b.button(text="🔙 Назад к квестам", callback_data="quest_menu")
    b.adjust(1)
    return b.as_markup()

# --- БОТ С КАСТОМНЫМ API ---
logging.basicConfig(level=logging.INFO)

session = AiohttpSession(api=TelegramAPIServer.from_base(API_BASE))
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher()

# --- ХЭНДЛЕРЫ ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.full_name
    update_balance(user_id, username, 0)
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n"
        f"Добро пожаловать в Хэллоуин-бота! 🎃\n\n"
        f"Здесь ты зарабатываешь монеты 🎃, выполняя квесты.\n"
        f"Выбери действие:",
        reply_markup=main_menu_kb(),
    )

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(cb: types.CallbackQuery):
    await cb.message.edit_text("Главное меню:", reply_markup=main_menu_kb())
    await cb.answer()

@dp.callback_query(F.data == "show_balance")
async def show_balance(cb: types.CallbackQuery):
    balance = get_balance(cb.from_user.id)
    await cb.message.edit_text(
        f"🎃 Твой баланс: **{balance}** монет.",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown",
    )
    await cb.answer()

@dp.callback_query(F.data == "quest_menu")
async def show_quests(cb: types.CallbackQuery):
    await cb.message.edit_text(
        "🎃 Доступные квесты:\n\nВыполни задание и получи награду!",
        reply_markup=quests_kb(),
    )
    await cb.answer()

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

@dp.callback_query(F.data.startswith("quest_") & ~F.data.startswith("quest_menu"))
async def handle_quest(cb: types.CallbackQuery):
    quest = QUESTS.get(cb.data)
    if not quest:
        await cb.answer("Неизвестный квест.", show_alert=True)
        return
    await cb.message.edit_text(
        f"🎃 Квест: **{quest['name']}**\n\n"
        f"{quest['desc']}\n\n"
        f"💰 Награда: **{quest['reward']} монет**\n\n"
        f"📩 Для доказательства напиши: @{ADMIN_USERNAME}\n"
        f"После отправки нажми кнопку ниже.",
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
        f"✅ Заявка на квест **{quest['name']}** отправлена!\n\n"
        f"Организатор @{ADMIN_USERNAME} проверит доказательство "
        f"и начислит {quest['reward']} монет.",
        parse_mode="Markdown",
    )
    await cb.answer("Заявка отправлена!")

# --- АДМИН-КОМАНДЫ (только для @sardelka) ---
@dp.message(F.text == "/pending")
async def cmd_pending(message: types.Message):
    if (message.from_user.username or "").lower() != ADMIN_USERNAME.lower():
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, quest_name, created_at FROM quest_proofs WHERE status='pending' ORDER BY id DESC LIMIT 30"
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await message.answer("Нет заявок в ожидании.")
        return
    text = "🎃 Заявки на проверку:\n\n"
    for pid, uid, qname, ts in rows:
        text += f"#{pid} · user_id={uid} · {qname}\n"
    text += "\nПодтвердить: /approve <id>\nОтклонить: /reject <id>"
    await message.answer(text)

@dp.message(F.text.startswith("/approve "))
async def cmd_approve(message: types.Message):
    if (message.from_user.username or "").lower() != ADMIN_USERNAME.lower():
        return
    try:
        pid = int(message.text.split()[1])
    except (IndexError, ValueError):
        await message.answer("Формат: /approve <id>")
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, quest_name, status FROM quest_proofs WHERE id = ?", (pid,)
    )
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
            uid, f"🎉 Квест **{qname}** подтверждён! +{reward} монет 🎃\nБаланс: {new_balance}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logging.warning(f"Не смог уведомить {uid}: {e}")
    await message.answer(f"✅ Заявка #{pid} подтверждена, начислено {reward}.")

@dp.message(F.text.startswith("/reject "))
async def cmd_reject(message: types.Message):
    if (message.from_user.username or "").lower() != ADMIN_USERNAME.lower():
        return
    try:
        pid = int(message.text.split()[1])
    except (IndexError, ValueError):
        await message.answer("Формат: /reject <id>")
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, quest_name, status FROM quest_proofs WHERE id = ?", (pid,)
    )
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
            uid, f"❌ Заявка на квест **{qname}** отклонена. Напиши @{ADMIN_USERNAME}.",
            parse_mode="Markdown",
        )
    except Exception:
        pass
    await message.answer(f"Заявка #{pid} отклонена.")

# --- HEALTHCHECK ДЛЯ RENDER ---
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
