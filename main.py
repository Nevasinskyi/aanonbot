
import asyncio
import logging
import os
import time

import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.utils.deep_linking import create_start_link
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError


# ==================================================
# CONFIG
# ==================================================

TOKEN = os.getenv("BOT_TOKEN", "9161228684:AAFxd7VQ1LwIo2ewaTnc8wQMrjSmGdh8haE")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "8792199502").split(",")
    if x.strip().isdigit()
}

DB_PATH = os.getenv("DB_PATH", "anonbot.db")

if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN!")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ==================================================
# STATES
# ==================================================

class AnonStates(StatesGroup):
    waiting_for_message = State()
    waiting_for_reply = State()


# ==================================================
# DATABASE
# ==================================================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                created_at INTEGER NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                blocker_id INTEGER NOT NULL,
                blocked_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (blocker_id, blocked_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS bans (
                user_id INTEGER PRIMARY KEY,
                admin_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER NOT NULL,
                reported_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                status TEXT DEFAULT 'open',
                UNIQUE(reporter_id, reported_id)
            )
        """)

        await db.commit()


async def register_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users VALUES (?, ?)",
            (user_id, int(time.time()))
        )
        await db.commit()


async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM bans WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            return await cursor.fetchone() is not None


async def is_blocked(sender_id: int, target_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT 1 FROM blocks
            WHERE blocker_id = ? AND blocked_id = ?
            """,
            (target_id, sender_id)
        ) as cursor:
            return await cursor.fetchone() is not None


async def check_rate_limit(user_id: int) -> bool:
    now = int(time.time())

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT COUNT(*) FROM messages
            WHERE sender_id = ? AND created_at > ?
            """,
            (user_id, now - 60)
        ) as cursor:
            result = await cursor.fetchone()

    return result[0] < 5


async def log_message(sender_id: int, target_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO messages
            (sender_id, target_id, created_at)
            VALUES (?, ?, ?)
            """,
            (sender_id, target_id, int(time.time()))
        )
        await db.commit()


# ==================================================
# KEYBOARDS
# ==================================================

def main_keyboard(link: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Моя анонимная ссылка",
                    url=link
                )
            ]
        ]
    )


def message_keyboard(sender_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Ответить",
                    callback_data=f"reply:{sender_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Пожаловаться",
                    callback_data=f"report:{sender_id}"
                ),
                InlineKeyboardButton(
                    text="Заблокировать",
                    callback_data=f"block:{sender_id}"
                )
            ]
        ]
    )


def admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Статистика",
                    callback_data="admin:stats"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Жалобы",
                    callback_data="admin:reports"
                )
            ]
        ]
    )


# ==================================================
# START
# ==================================================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):

    await state.clear()

    user_id = message.from_user.id

    if await is_banned(user_id):
        await message.answer("Ваш доступ к боту ограничен.")
        return

    await register_user(user_id)

    args = message.text.split(maxsplit=1)

    if len(args) > 1:

        payload = args[1].strip()

        if not payload.isdigit():
            await message.answer("Некорректная ссылка.")
            return

        target_id = int(payload)

        if target_id == user_id:
            await message.answer(
                "Нельзя отправить сообщение самому себе."
            )
            return

        if await is_banned(target_id):
            await message.answer("Получатель недоступен.")
            return

        await state.update_data(target_id=target_id)

        await state.set_state(
            AnonStates.waiting_for_message
        )

        await message.answer(
            "Вы можете отправить анонимное сообщение.\n\n"
            "Отправьте текст, фотографию, видео, "
            "голосовое сообщение или стикер.\n\n"
            "Получатель не увидит ваш Telegram-аккаунт.\n\n"
            "/cancel — отменить отправку."
        )
        return

    link = await create_start_link(
        bot,
        str(user_id),
        encode=False
    )

    await message.answer(
        "Добро пожаловать в AnonBotShkola15!\n\n"
        "Здесь вы можете получать анонимные сообщения.\n\n"
        "Ваша персональная ссылка:\n"
        f"{link}\n\n"
        "Поделитесь ссылкой с друзьями!",
        reply_markup=main_keyboard(link)
    )


# ==================================================
# CANCEL
# ==================================================

@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):

    if await state.get_state() is None:
        await message.answer("Нет активного действия.")
        return

    await state.clear()
    await message.answer("Действие отменено.")


# ==================================================
# HELP AND USER ID
# ==================================================

@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "AnonBotShkola15 — анонимные сообщения.\n\n"
        "/start — главное меню\n"
        "/cancel — отменить действие\n"
        "/help — помощь\n"
        "/myid — узнать свой ID\n\n"
        "Не отправляйте личные данные незнакомым людям."
    )


@dp.message(Command("myid"))
async def myid(message: Message):
    await message.answer(
        f"Ваш Telegram ID: {message.from_user.id}"
    )


# ==================================================
# ANONYMOUS MESSAGE
# ==================================================

@dp.message(AnonStates.waiting_for_message)
async def receive_message(
    message: Message,
    state: FSMContext
):

    sender_id = message.from_user.id

    if await is_banned(sender_id):
        await state.clear()
        await message.answer("Ваш доступ ограничен.")
        return

    if message.text and message.text.startswith("/"):
        await message.answer(
            "Команды нельзя отправлять как сообщения.\n"
            "Используйте /cancel для отмены."
        )
        return

    data = await state.get_data()
    target_id = data.get("target_id")

    if not target_id:
        await state.clear()
        await message.answer("Ошибка. Откройте ссылку заново.")
        return

    if await is_blocked(sender_id, target_id):
        await state.clear()
        await message.answer(
            "Вы не можете отправить сообщение этому пользователю."
        )
        return

    if not await check_rate_limit(sender_id):
        await message.answer(
            "Слишком много сообщений. "
            "Попробуйте через минуту."
        )
        return

    try:
        await bot.copy_message(
            chat_id=target_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=message_keyboard(sender_id)
        )

        await log_message(sender_id, target_id)

        await message.answer(
            "Ваше анонимное сообщение отправлено!"
        )

        await state.clear()

    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer(
            "Не удалось доставить сообщение. "
            "Возможно, получатель недоступен."
        )
        await state.clear()


# ==================================================
# ANONYMOUS REPLY
# ==================================================

@dp.callback_query(F.data.startswith("reply:"))
async def reply_callback(
    callback: CallbackQuery,
    state: FSMContext
):

    sender_id = callback.from_user.id

    if await is_banned(sender_id):
        await callback.answer(
            "Ваш доступ ограничен.",
            show_alert=True
        )
        return

    target_id = int(callback.data.split(":")[1])

    if target_id == sender_id:
        await callback.answer("Некорректный запрос.")
        return

    if await is_blocked(sender_id, target_id):
        await callback.answer(
            "Вы заблокировали этого пользователя.",
            show_alert=True
        )
        return

    await state.clear()

    await state.update_data(target_id=target_id)

    await state.set_state(AnonStates.waiting_for_reply)

    await callback.answer()

    await callback.message.answer(
        "Напишите анонимный ответ.\n\n"
        "/cancel — отменить."
    )


@dp.message(AnonStates.waiting_for_reply)
async def send_reply(
    message: Message,
    state: FSMContext
):

    sender_id = message.from_user.id

    if await is_banned(sender_id):
        await state.clear()
        await message.answer("Ваш доступ ограничен.")
        return

    if message.text and message.text.startswith("/"):
        await message.answer(
            "Команды нельзя отправлять как ответы.\n"
            "Используйте /cancel."
        )
        return

    data = await state.get_data()
    target_id = data.get("target_id")

    if not target_id:
        await state.clear()
        await message.answer("Ошибка. Попробуйте ещё раз.")
        return

    if await is_blocked(sender_id, target_id):
        await state.clear()
        await message.answer("Этот пользователь заблокировал вас.")
        return

    if not await check_rate_limit(sender_id):
        await message.answer(
            "Слишком много сообщений. Попробуйте позже."
        )
        return

    try:
        await bot.copy_message(
            chat_id=target_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=message_keyboard(sender_id)
        )

        await log_message(sender_id, target_id)

        await message.answer("Анонимный ответ отправлен!")

        await state.clear()

    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer("Не удалось отправить ответ.")
        await state.clear()


# ==================================================
# BLOCK USER
# ==================================================

@dp.callback_query(F.data.startswith("block:"))
async def block_user(callback: CallbackQuery):

    blocker_id = callback.from_user.id
    blocked_id = int(callback.data.split(":")[1])

    if blocker_id == blocked_id:
        await callback.answer("Некорректный запрос.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO blocks
            VALUES (?, ?, ?)
            """,
            (blocker_id, blocked_id, int(time.time()))
        )
        await db.commit()

    await callback.answer(
        "Пользователь заблокирован.",
        show_alert=True
    )


# ==================================================
# REPORT USER
# ==================================================

@dp.callback_query(F.data.startswith("report:"))
async def report_user(callback: CallbackQuery):

    reporter_id = callback.from_user.id
    reported_id = int(callback.data.split(":")[1])

    if reporter_id == reported_id:
        await callback.answer("Некорректный запрос.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO reports
            (reporter_id, reported_id, created_at)
            VALUES (?, ?, ?)
            """,
            (reporter_id, reported_id, int(time.time()))
        )
        await db.commit()

    await callback.answer(
        "Жалоба отправлена администрации.",
        show_alert=True
    )


# ==================================================
# ADMIN CHECK
# ==================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ==================================================
# ADMIN PANEL
# ==================================================

@dp.message(Command("admin"))
async def admin_panel(message: Message):

    if not is_admin(message.from_user.id):
        await message.answer("У вас нет доступа.")
        return

    await message.answer(
        "ADMIN PANEL\n\n"
        "Управление AnonBotShkola15",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data.startswith("admin:"))
async def admin_callback(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):
        await callback.answer(
            "Доступ запрещён.",
            show_alert=True
        )
        return

    action = callback.data.split(":")[1]

    if action == "stats":

        async with aiosqlite.connect(DB_PATH) as db:

            async with db.execute(
                "SELECT COUNT(*) FROM users"
            ) as cursor:
                users = (await cursor.fetchone())[0]

            async with db.execute(
                "SELECT COUNT(*) FROM messages"
            ) as cursor:
                messages = (await cursor.fetchone())[0]

            async with db.execute(
                "SELECT COUNT(*) FROM reports WHERE status='open'"
            ) as cursor:
                reports = (await cursor.fetchone())[0]

            async with db.execute(
                "SELECT COUNT(*) FROM bans"
            ) as cursor:
                bans = (await cursor.fetchone())[0]

        await callback.message.answer(
            "СТАТИСТИКА ANONBOTSCHOOL15\n\n"
            f"Пользователей: {users}\n"
            f"Сообщений: {messages}\n"
            f"Открытых жалоб: {reports}\n"
            f"Заблокировано: {bans}"
        )

    elif action == "reports":

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                """
                SELECT id, reporter_id, reported_id
                FROM reports
                WHERE status = 'open'
                ORDER BY id DESC
                LIMIT 10
                """
            ) as cursor:
                reports = await cursor.fetchall()

        if not reports:
            await callback.message.answer("Новых жалоб нет.")
        else:
            for report_id, reporter_id, reported_id in reports:

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="Закрыть жалобу",
                                callback_data=f"resolve:{report_id}"
                            )
                        ]
                    ]
                )

                await callback.message.answer(
                    f"Жалоба №{report_id}\n"
                    f"Отправитель жалобы: {reporter_id}\n"
                    f"Пользователь: {reported_id}",
                    reply_markup=keyboard
                )

    await callback.answer()


# ==================================================
# RESOLVE REPORT
# ==================================================

@dp.callback_query(F.data.startswith("resolve:"))
async def resolve_report(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.")
        return

    try:
        report_id = int(callback.data.split(":")[1])
    except ValueError:
        await callback.answer("Некорректный ID.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE reports
            SET status = 'closed'
            WHERE id = ?
            """,
            (report_id,)
        )
        await db.commit()

    await callback.answer("Жалоба закрыта.")
    await callback.message.edit_text(
        f"Жалоба №{report_id} закрыта."
    )


# ==================================================
# BAN / UNBAN
# ==================================================

@dp.message(Command("ban"))
async def ban_user(message: Message):

    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    args = message.text.split()

    if len(args) != 2 or not args[1].isdigit():
        await message.answer(
            "Использование: /ban USER_ID"
        )
        return

    user_id = int(args[1])

    if user_id in ADMIN_IDS:
        await message.answer("Нельзя заблокировать администратора.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO bans
            VALUES (?, ?, ?)
            """,
            (user_id, message.from_user.id, int(time.time()))
        )
        await db.commit()

    await message.answer(
        f"Пользователь {user_id} заблокирован."
    )


@dp.message(Command("unban"))
async def unban_user(message: Message):

    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    args = message.text.split()

    if len(args) != 2 or not args[1].isdigit():
        await message.answer(
            "Использование: /unban USER_ID"
        )
        return

    user_id = int(args[1])

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM bans WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()

    await message.answer(
        f"Пользователь {user_id} разблокирован."
    )


# ==================================================
# UNKNOWN MESSAGE
# ==================================================

@dp.message()
async def unknown_message(message: Message):

    await message.answer(
        "Используйте /start для открытия главного меню.\n"
        "/help — список команд."
    )


# ==================================================
# MAIN
# ==================================================

async def main():

    await init_db()

    print("================================")
    print("AnonBotShkola15")
    print("Database initialized")
    print("Bot is running!")
    print("================================")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
