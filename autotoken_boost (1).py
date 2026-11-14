import base64
import io
import json
import os
import random
import sys
import zipfile
from collections import deque
from datetime import date
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


# Укажите токен через переменную окружения BOT_TOKEN.
BOT_TOKEN = "9180340777:AAEGDqGWHBtnAQb9wooL057CH2TrMJMTVuc"
if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Установите токен бота в переменной окружения."
    )

# ID единственного администратора.
ADMIN_ID = 8708484648

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

MARK_UP = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Встал", callback_data="stood"),
            InlineKeyboardButton(text="Ошибка", callback_data="error"),
        ]
    ]
)

# Общий пул файлов: (имя файла, содержимое).
chat_pools: deque[tuple[str, bytes]] = deque()

# Чаты, привязанные командой /connect.
connected_chats: set[int] = set()

# Статистика: chat_id -> {date, tokens, stood, error, users}.
chat_stats: dict[int, dict] = {}

# (chat_id, message_id) -> user_id.
token_owner: dict[tuple[int, int], int] = {}

STATE_FILE = Path(__file__).resolve().with_name("bot_state.json")


def get_stats(chat_id: int, today: date) -> dict:
    """Возвращает статистику за день, создавая её при необходимости."""
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


def save_state() -> None:
    """Сохраняет пул, привязки, статистику и ожидающие отметки."""
    stats_data = {}
    for chat_id, st in chat_stats.items():
        stats_data[str(chat_id)] = {
            "date": st["date"].isoformat(),
            "tokens": int(st.get("tokens", 0)),
            "stood": int(st.get("stood", 0)),
            "error": int(st.get("error", 0)),
            "users": {
                str(uid): {
                    "name": user.get("name", "unknown"),
                    "tokens": int(user.get("tokens", 0)),
                    "stood": int(user.get("stood", 0)),
                    "error": int(user.get("error", 0)),
                }
                for uid, user in st.get("users", {}).items()
            },
        }

    data = {
        "connected": sorted(connected_chats),
        "pool": [
            [name, base64.b64encode(blob).decode("ascii")]
            for name, blob in chat_pools
        ],
        "stats": stats_data,
        "token_owner": [
            [chat_id, message_id, uid]
            for (chat_id, message_id), uid in token_owner.items()
        ],
    }

    temp_file = STATE_FILE.with_suffix(".tmp")
    try:
        temp_file.write_text(
            json.dumps(data, ensure_ascii=False),
            encoding="utf-8",
        )
        temp_file.replace(STATE_FILE)
    except Exception as exc:
        print(f"save_state error: {exc}")
        try:
            temp_file.unlink(missing_ok=True)
        except OSError:
            pass


def load_state() -> None:
    """Загружает сохранённое состояние; поддерживает старый формат файла."""
    if not STATE_FILE.exists():
        return

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))

        connected_chats.clear()
        connected_chats.update(int(chat_id) for chat_id in data.get("connected", []))

        chat_pools.clear()
        for name, encoded_blob in data.get("pool", []):
            chat_pools.append((str(name), base64.b64decode(encoded_blob)))

        chat_stats.clear()
        for chat_id, raw in data.get("stats", {}).items():
            users = {}
            for uid, user in raw.get("users", {}).items():
                users[int(uid)] = {
                    "name": user.get("name", "unknown"),
                    "tokens": int(user.get("tokens", 0)),
                    "stood": int(user.get("stood", 0)),
                    "error": int(user.get("error", 0)),
                }

            chat_stats[int(chat_id)] = {
                "date": date.fromisoformat(raw["date"]),
                "tokens": int(raw.get("tokens", 0)),
                "stood": int(raw.get("stood", 0)),
                "error": int(raw.get("error", 0)),
                "users": users,
            }

        token_owner.clear()
        for chat_id, message_id, uid in data.get("token_owner", []):
            token_owner[(int(chat_id), int(message_id))] = int(uid)

    except Exception as exc:
        print(f"load_state error: {exc}")


load_state()


def extract_txt(zip_bytes: bytes) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for name in archive.namelist():
            if name.lower().endswith(".txt") and not name.endswith("/"):
                files.append((name.rsplit("/", 1)[-1], archive.read(name)))
    return files


async def log_to_admin(text: str) -> None:
    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception as exc:
        print(f"log_to_admin error: {exc}")


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! 📦 Отправь ZIP-архив с .txt файлами, "
        "а затем используй /token 🎫, чтобы получать файлы по одному."
    )


@dp.message(Command("connect"))
async def cmd_connect(message: types.Message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        await message.answer("🚫 У вас нет прав на эту команду.")
        return

    connected_chats.add(message.chat.id)
    save_state()
    await log_to_admin(f"🔗 Чат привязан: {message.chat.id}")
    await message.answer(
        "✅ Бот привязан к этому чату. Пользователи могут использовать /token 🎫."
    )


@dp.message(Command("unconnect"))
async def cmd_unconnect(message: types.Message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        await message.answer("🚫 У вас нет прав на эту команду.")
        return

    connected_chats.discard(message.chat.id)
    save_state()
    await log_to_admin(f"🔓 Чат отвязан: {message.chat.id}")
    await message.answer(
        "🔓 Бот отвязан от этого чата. Команды бота больше недоступны."
    )


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
    if doc is None or not doc.file_name or not doc.file_name.lower().endswith(".zip"):
        await message.answer("⚠️ Пожалуйста, отправь ZIP-архив.")
        return

    file_info = await bot.get_file(doc.file_id)
    destination = io.BytesIO()
    await bot.download_file(file_info.file_path, destination=destination)

    try:
        files = extract_txt(destination.getvalue())
    except (zipfile.BadZipFile, OSError) as exc:
        await message.answer(f"⚠️ Не удалось прочитать ZIP-архив: {exc}")
        return

    if not files:
        await message.answer("⚠️ В архиве нет .txt файлов.")
        return

    chat_pools.extend(files)
    save_state()

    names = ", ".join(name for name, _ in files)
    await log_to_admin(
        f"📦 Загружен архив ({doc.file_name}): +{len(files)} txt, "
        f"всего в пуле {len(chat_pools)}\nФайлы: {names}"
    )
    await message.answer(
        f"✅ Архив обработан. .txt файлов в пуле: {len(chat_pools)}. "
        "Используй /token 🎫."
    )


@dp.message(Command("token"))
async def cmd_token(message: types.Message):
    if is_private(message):
        await message.answer("🚫 Команда /token недоступна в личных сообщениях.")
        return

    if not await is_connected(message):
        return

    if not chat_pools:
        await message.answer("📭 Файлов в пуле нет. Отправь ZIP-архив, чтобы добавить файлы.")
        return

    name, data = chat_pools.popleft()
    sent = await message.reply_document(
        types.BufferedInputFile(data, filename=name),
        caption=f"📄 {name}\n✅ Готов к приёму · 🎫 по /token",
        reply_markup=MARK_UP,
    )

    chat_id = message.chat.id
    uid = message.from_user.id
    token_owner[(chat_id, sent.message_id)] = uid

    st = get_stats(chat_id, date.today())
    st["tokens"] += 1
    username = message.from_user.username or message.from_user.full_name
    user = st["users"].setdefault(
        uid,
        {"name": username, "tokens": 0, "stood": 0, "error": 0},
    )
    user["name"] = username
    user["tokens"] += 1
    save_state()

    await log_to_admin(
        f"🪙 Токен взят:\nФайл: {name}\nПользователь: @{username} (id {uid})\n"
        f"Чат: {chat_id}\nОстаток в пуле: {len(chat_pools)}"
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

    st = get_stats(message.chat.id, date.today())
    total = st["tokens"]
    if total == 0:
        await message.answer("📭 За сегодня токены ещё не выдавались.")
        return

    def pct(value: int) -> str:
        return f"{round(value / total * 100)}%"

    ranked = sorted(
        st["users"].values(),
        key=lambda user: user["tokens"],
        reverse=True,
    )
    top_lines = "\n".join(
        f"{index}. @{user['name']} - ({user['tokens']})"
        for index, user in enumerate(ranked, start=1)
    ) or "Пока нет участников."

    text = (
        "✅ Проверка успешно завершена!\n"
        "📊 Статистика за сегодня\n\n"
        f"✅ Валид: {st['stood']} ({pct(st['stood'])})\n"
        f"⚠️ Ошибка: {st['error']} ({pct(st['error'])})\n"
        f"📦 Итого: {total} (100%)\n\n"
        "🏆 ТОП ПОЛЬЗОВАТЕЛЕЙ\n\n"
        f"{top_lines}"
    )
    await message.answer(text)


@dp.message(Command("boost"), F.chat.type == "private")
async def cmd_boost_private(message: types.Message):
    """Админская корректировка счётчиков из личного чата с ботом."""
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        await message.answer("🚫 У вас нет прав на эту команду.")
        return

    parts = (message.text or "").split()
    if len(parts) not in (2, 3):
        await message.answer(
            "Формат команды:\n"
            "/boost <количество> — если подключена одна группа\n"
            "/boost <chat_id> <количество> — если групп несколько"
        )
        return

    if len(parts) == 2:
        if not connected_chats:
            await message.answer("📭 Нет подключённых групп. Сначала используй /connect в группе.")
            return
        if len(connected_chats) != 1:
            chat_ids = "\n".join(str(chat_id) for chat_id in sorted(connected_chats))
            await message.answer(
                "⚠️ Подключено несколько групп. Укажи ID целевой группы:\n"
                f"/boost <chat_id> <количество>\n\nПодключённые ID:\n{chat_ids}"
            )
            return
        chat_id = next(iter(connected_chats))
        amount_raw = parts[1]
    else:
        try:
            chat_id = int(parts[1])
        except ValueError:
            await message.answer("⚠️ ID группы должен быть числом.")
            return
        amount_raw = parts[2]
        if chat_id not in connected_chats:
            await message.answer("⚠️ Эта группа не подключена к боту.")
            return

    try:
        amount = int(amount_raw)
    except ValueError:
        await message.answer("⚠️ Количество должно быть целым числом. Пример: /boost 10")
        return

    if not 1 <= amount <= 1000:
        await message.answer("⚠️ Укажи количество от 1 до 1000.")
        return

    st = get_stats(chat_id, date.today())
    eligible_ids = [
        uid for uid, user in st["users"].items()
        if int(user.get("tokens", 0)) > 0
    ]
    if not eligible_ids:
        await message.answer(
            "📭 В этой группе сегодня ещё нет участников, которые брали файлы через /token."
        )
        return

    # Случайно и равномерно распределяем добавления между участниками.
    shuffled_ids = eligible_ids[:]
    random.shuffle(shuffled_ids)
    distribution = {uid: 0 for uid in eligible_ids}

    for index in range(amount):
        if index > 0 and index % len(shuffled_ids) == 0:
            random.shuffle(shuffled_ids)
        uid = shuffled_ids[index % len(shuffled_ids)]
        user = st["users"][uid]
        user["tokens"] = int(user.get("tokens", 0)) + 1
        user["stood"] = int(user.get("stood", 0)) + 1
        distribution[uid] += 1

    # Изменяются исходные счётчики статистики и ТОПа; отдельных счётчиков нет.
    st["tokens"] += amount
    st["stood"] += amount
    save_state()

    details = [
        f"• @{st['users'][uid]['name']}: +{count}"
        for uid, count in distribution.items()
        if count
    ]
    await message.answer(
        f"✅ Счётчики группы {chat_id} обновлены.\n"
        f"Добавлено: {amount}\n"
        f"📦 Итого: {st['tokens']}\n"
        f"✅ Валид: {st['stood']}\n\n"
        "Распределение по участникам:\n" + "\n".join(details)
    )
    await log_to_admin(
        f"Корректировка счётчиков\nЧат: {chat_id}\nКоличество: {amount}\n"
        + "\n".join(details)
    )


@dp.message(Command("apanel"))
async def cmd_apanel(message: types.Message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        await message.answer("🚫 У вас нет доступа к админ-панели.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗒 Статус чатов", callback_data="apanel_status"),
                InlineKeyboardButton(text="🗑 Сброс пулов", callback_data="apanel_reset"),
            ],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="apanel_stats")],
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
            lines = [f"🔗 {chat_id} — подключён" for chat_id in sorted(connected_chats)]
            lines.append(f"📦 Всего файлов в пуле: {len(chat_pools)}")
            await callback.message.edit_text("🗒 Статус чатов\n\n" + "\n".join(lines))
        else:
            await callback.message.edit_text("📭 Нет подключённых чатов.")

    elif action == "apanel_reset":
        chat_pools.clear()
        save_state()
        await callback.message.edit_text("🗑 Все файлы из пула удалены.")

    elif action == "apanel_stats":
        lines = []
        for chat_id in sorted(connected_chats):
            st = get_stats(chat_id, date.today())
            lines.append(
                f"🎫 Токены: {st['tokens']}, ✅ встал: {st['stood']}, "
                f"⚠️ ошибка: {st['error']} (чат {chat_id})"
            )
        await callback.message.edit_text(
            "📊 Статистика\n\n" + "\n".join(lines) if lines else "📭 Нет данных."
        )

    await callback.answer()


@dp.callback_query(F.data.in_({"stood", "error"}))
async def on_mark(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    key = (chat_id, callback.message.message_id)
    uid = token_owner.pop(key, None)
    if uid is None:
        await callback.answer("Эта отметка уже обработана или устарела.", show_alert=True)
        return

    label = "встал" if callback.data == "stood" else "ошибка"
    caption = "✅ Помечен как встал" if callback.data == "stood" else "⚠️ Помечен как ошибка"

    try:
        await callback.message.edit_caption(caption=caption, reply_markup=None)
    except Exception:
        # Возвращаем привязку, чтобы можно было повторить действие после ошибки Telegram API.
        token_owner[key] = uid
        await callback.answer("Не удалось обновить сообщение. Попробуй ещё раз.", show_alert=True)
        return

    st = get_stats(chat_id, date.today())
    field = "stood" if callback.data == "stood" else "error"
    st[field] += 1

    user = st["users"].get(uid)
    if user is not None:
        user[field] += 1

    save_state()
    await log_to_admin(
        f"{'✅' if callback.data == 'stood' else '⚠️'} Отметка: {label}\n"
        f"Пользователь: id {uid}\nЧат: {chat_id}"
    )
    await callback.answer()


if __name__ == "__main__":
    import asyncio

    asyncio.run(dp.start_polling(bot))
