import io
import os
import json
import base64
import zipfile
from collections import deque
from datetime import date, datetime

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext

MARK_UP = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Встал", callback_data="stood"),
            InlineKeyboardButton(text="Ошибка", callback_data="error"),
        ]
    ]
)

BOT_TOKEN = "9180340777:AAEGDqGWHBtnAQb9wooL057CH2TrMJMTVuc"

# единственный администратор с доступом к панели /apanel
ADMIN_ID = 8708484648

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# общий пул файлов: deque из (filename, bytes) — FIFO, выдаются по очереди без повторов
chat_pools: deque = deque()

# чаты, привязанные к боту командой /connect
connected_chats: set[int] = set()

# статистика: chat_id -> {"date": date, "tokens": int, "stood": int,
#                          "error": int, "users": {user_id: {"name", "tokens", "stood", "error"}}}
chat_stats: dict[int, dict] = {}


def get_stats(chat_id: int, today: date) -> dict:
    st = chat_stats.get(chat_id)
    if st is None or st.get("date") != today:
        st = {
            "date": today,
            "tokens": 0,
            "stood": 0,
            "error": 0,
            "users": {},
        }
        chat_stats[chat_id] = st
    return st


# (chat_id, message_id) -> user_id, привязка выданного файла к взявшему пользователю
token_owner: dict[tuple[int, int], int] = {}

# === Персистентность пула на диск (переживает перезапуск бота) ===
import sys
STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(sys.argv[0])), "bot_state.json"
)


def save_state() -> None:
    data = {
        "connected": list(connected_chats),
        "pool": [[name, base64.b64encode(blob).decode()] for name, blob in chat_pools],
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print("save_state error:", e)


def load_state() -> None:
    global connected_chats
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        connected_chats = set(data.get("connected", []))
        chat_pools.clear()
        for name, b64 in data.get("pool", []):
            chat_pools.append((name, base64.b64decode(b64)))
    except FileNotFoundError:
        pass
    except Exception as e:
        print("load_state error:", e)


load_state()


def extract_txt(zip_bytes: bytes) -> list[tuple[str, bytes]]:
    files = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if name.lower().endswith(".txt") and not name.endswith("/"):
                base_name = name.split("/")[-1]
                files.append((base_name, zf.read(name)))
    return files


async def log_to_admin(text: str) -> None:
    """Отправляет лог в личные сообщения администратору."""
    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception as e:
        print("log_to_admin error:", e)


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! 📦 Отправь ZIP-архив с .txt файлами, "
        "а затем используй /token 🎫, чтобы получать файлы по одному."
    )


@dp.message(Command("connect"))
async def cmd_connect(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 У вас нет прав на эту команду.")
        return
    connected_chats.add(message.chat.id)
    save_state()
    await log_to_admin(f"🔗 Чат привязан: {message.chat.id}")
    await message.answer("✅ Бот привязан к этому чату. Пользователи могут использовать /token 🎫.")


@dp.message(Command("unconnect"))
async def cmd_unconnect(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 У вас нет прав на эту команду.")
        return
    connected_chats.discard(message.chat.id)
    save_state()
    await log_to_admin(f"🔓 Чат отвязан: {message.chat.id}")
    await message.answer("🔓 Бот отвязан от этого чата. Команды бота больше недоступны.")


async def is_connected(message: types.Message) -> bool:
    if message.chat.id not in connected_chats:
        await message.answer("⚠️ Бот не привязан к этому чату. Используй /connect.")
        return False
    return True


def is_private(message: types.Message) -> bool:
    return message.chat.type == "private"


@dp.message(F.document)
async def on_document(message: types.Message):
    doc = message.document
    album_log = f" media_group={message.media_group_id}" if message.media_group_id else ""
    print(f"[doc] {getattr(doc, 'file_name', '?')} ({len(chat_pools)} in pool){album_log}")
    if not doc.file_name.lower().endswith(".zip"):
        await message.answer("⚠️ Пожалуйста, отправь ZIP-архив.")
        return

    file = await bot.get_file(doc.file_id)
    dest = io.BytesIO()
    await bot.download_file(file.file_path, destination=dest)
    zip_bytes = dest.getvalue()

    files = extract_txt(zip_bytes)
    if not files:
        await message.answer("⚠️ В архиве нет .txt файлов.")
        return

    pool = chat_pools
    pool.extend(files)
    save_state()
    names = ", ".join(fn for fn, _ in files)
    await log_to_admin(
        f"📦 Загружен архив ({doc.file_name}): "
        f"+{len(files)} txt, всего в пуле {len(pool)}\n"
        f"Файлы: {names}"
    )
    await message.answer(
        f"✅ Архив обработан. .txt файлов в пулле: {len(pool)}. Используй /token 🎫."
    )


@dp.message(Command("token"))
async def cmd_token(message: types.Message):
    if is_private(message):
        await message.answer("🚫 Команда /token недоступна в личных сообщениях.")
        return

    if not await is_connected(message):
        return

    chat_id = message.chat.id

    if not chat_pools:
        await message.answer("📭 Файлов в пулле нету. Отправь ZIP-архив, чтобы добавить файлы.")
        return

    name, data = chat_pools.popleft()

    sent = await message.reply_document(
        types.BufferedInputFile(data, filename=name),
        caption=f"📄 {name}\n✅ Готов к приёму · 🎫 по /token",
        reply_markup=MARK_UP,
    )
    token_owner[(chat_id, sent.message_id)] = message.from_user.id

    st = get_stats(chat_id, date.today())
    st["tokens"] += 1
    uid = message.from_user.id
    username = message.from_user.username or message.from_user.full_name
    user = st["users"].setdefault(
        uid,
        {"name": username, "tokens": 0, "stood": 0, "error": 0},
    )
    user["name"] = username
    user["tokens"] += 1
    save_state()
    await log_to_admin(
        f"🪙 Токен взят:\n"
        f"Файл: {name}\n"
        f"Пользователь: @{username} (id {uid})\n"
        f"Чат: {chat_id}\n"
        f"Остаток в пуле: {len(chat_pools)}"
    )

    if not chat_pools:
        await message.answer("🎉 Файлы закончились.")


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if is_private(message):
        await message.answer("🚫 Команда /stats недоступна в личных сообщениях.")
        return

    if not await is_connected(message):
        return

    chat_id = message.chat.id
    st = get_stats(chat_id, date.today())
    if st["tokens"] == 0:
        await message.answer("📭 За сегодня токены ещё не выдавались.")
        return

    total = st["tokens"]

    def pct(n: int) -> str:
        return f"{round(n / total * 100)}%"

    ranked = sorted(
        st["users"].values(),
        key=lambda u: u["tokens"],
        reverse=True,
    )
    top_lines = "\n".join(
        f"{i + 1}. @{u['name']} - ({u['tokens']})"
        for i, u in enumerate(ranked)
    )

    msg = (
        "✅ Проверка успешно завершена!\n"
        "📊 Статистика за сегодня\n\n"
        f"✅ Валид: {st['stood']} ({pct(st['stood'])})\n"
        f"⚠️ Ошибка: {st['error']} ({pct(st['error'])})\n"
        f"📦 Итого: {total} (100%)\n\n"
        "🏆 ТОП ПОЛЬЗОВАТЕЛЕЙ\n\n"
        + top_lines
    )
    await message.answer(msg)


@dp.message(Command("apanel"))
async def cmd_apanel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 У вас нет доступа к админ-панели.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗒 Статус чатов", callback_data="apanel_status"),
                InlineKeyboardButton(text="🗑 Сброс пулов", callback_data="apanel_reset"),
            ],
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="apanel_stats"),
            ],
        ]
    )
    await message.answer(
        "🛠 АДМИН-ПАНЕЛЬ\nУправление пулами и статистикой",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data.startswith("apanel_"))
async def on_apanel_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("🚫 Доступ запрещён.", show_alert=True)
        return

    action = callback.data
    if action == "apanel_status":
        if connected_chats:
            lines = [f"🔗 {chat_id} — подключён" for chat_id in connected_chats]
            lines.append(f"📦 Всего файлов в пуле: {len(chat_pools)}")
            await callback.message.edit_text("🗒 Статус чатов\n\n" + "\n".join(lines))
        else:
            await callback.message.edit_text("📭 Нет подключённых чатов.")

    elif action == "apanel_reset":
        chat_pools.clear()
        save_state()
        await callback.message.edit_text("🗑 Все пулы файлов сброшены.")

    elif action == "apanel_stats":
        lines = []
        for chat_id in connected_chats:
            st = get_stats(chat_id, date.today())
            lines.append(
                f"🎫 Токены: {st['tokens']}, "
                f"✅ встал {st['stood']}, ⚠️ ошибка {st['error']} (чат {chat_id})"
            )
        await callback.message.edit_text(
            "📊 Статистика\n\n" + "\n".join(lines) if lines else "📭 Нет данных."
        )

    await callback.answer()


@dp.callback_query(F.data.in_({"stood", "error"}))
async def on_mark(callback: types.CallbackQuery):
    label = "встал" if callback.data == "stood" else "ошибка"
    if callback.data == "stood":
        caption = "✅ Помечен как встал"
    else:
        caption = "⚠️ Помечен как ошибка"
    await callback.message.edit_caption(
        caption=caption
    )

    chat_id = callback.message.chat.id
    uid = token_owner.pop((chat_id, callback.message.message_id), None)
    st = get_stats(chat_id, date.today())
    if callback.data == "stood":
        st["stood"] += 1
    else:
        st["error"] += 1

    if uid is not None:
        user = st["users"].get(uid)
        if user is not None:
            user["stood" if callback.data == "stood" else "error"] += 1

    await log_to_admin(
        f"{'✅' if callback.data == 'stood' else '⚠️'} Отметка: {label}\n"
        f"Пользователь: id {uid if uid is not None else 'неизвестно'}\n"
        f"Чат: {chat_id}"
    )

    await callback.answer()


if __name__ == "__main__":
    import asyncio

    asyncio.run(dp.start_polling(bot))