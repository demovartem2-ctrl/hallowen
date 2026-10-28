import os
import shutil
import sqlite3

# Путь, где сейчас лежит база (эфемерная папка проекта)
OLD_DB = "/opt/render/project/src/halloween_bot.db"
# Новый путь на постоянном диске (который ты создал в настройках Render)
NEW_DB = "/var/data/halloween_bot.db"

# Создаем папку, если её нет (на всякий случай)
os.makedirs("/var/data", exist_ok=True)

if os.path.exists(OLD_DB):
    # Копируем базу
    shutil.copy2(OLD_DB, NEW_DB)
    print(f"✅ База скопирована из {OLD_DB} в {NEW_DB}")
    
    # Проверяем, что балансы на месте
    conn = sqlite3.connect(NEW_DB)
    cur = conn.cursor()
    cur.execute("SELECT user_id, balance FROM users LIMIT 5")
    print("Пример данных из новой базы:", cur.fetchall())
    conn.close()
else:
    print("❌ Старая база не найдена. Возможно, она уже стерлась.")
    print("Будет создана новая при запуске бота.")
