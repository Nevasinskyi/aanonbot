
import asyncio
import logging
import os
import time
from pathlib import Path

import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
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


# =====================================================
# CONFIG
# =====================================================

TOKEN = os.getenv("BOT_TOKEN", "9161228684:AAFxd7VQ1LwIo2ewaTnc8wQMrjSmGdh8haE")
OWNER_ID = int(os.getenv("OWNER_ID", "8792199502"))
CHANNEL_ID = os.getenv("ADMIN_CHANNEL_ID", "-1004412233871")
DB_PATH = os.getenv("DB_PATH", "anonbot.db")

if not TOKEN:
    raise RuntimeError("Не указан BOT_TOKEN")

if OWNER_ID <= 0:
    raise RuntimeError("Не указан OWNER_ID")

CHANNEL_ID = int(CHANNEL_ID) if CHANNEL_ID.lstrip("-").isdigit() else None

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)

bot = Bot(TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# =====================================================
# LEVELS
# =====================================================

LEVELS = {
    1: "Младший модератор",
    2: "Модератор",
    3: "Администратор",
    4: "Старший администратор",
    5: "Помощник создателя",
    6: "Создатель"
}

POSITIONS = {
    "staff": "Сотрудник",
    "deputy": "Заместитель начальника",
    "head": "Начальник"
}

DEFAULT_DEPARTMENTS = [
    "Модерация",
    "Контроль",
    "Поддержка",
    "Безопасность"
]


# =====================================================
# STATES
# =====================================================

class States(StatesGroup):
    message = State()
    reply = State()

    appoint_id = State()
    appoint_level = State()
    appoint_department = State()

    department_name = State()
    warning_id = State()
    warning_reason = State()


# =====================================================
# DATABASE
# =====================================================

async def execute(query, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, params)
        await db.commit()


async def one(query, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(query, params) as cursor:
            return await cursor.fetchone()


async def all_rows(query, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(query, params) as cursor:
            return await cursor.fetchall()


async def init_db():

    async with aiosqlite.connect(DB_PATH) as db:

        tables = [
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                created_at INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS departments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                level INTEGER NOT NULL,
                department_id INTEGER,
                position TEXT DEFAULT 'staff',
                appointed_by INTEGER,
                appointed_at INTEGER
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                content TEXT,
                created_at INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER NOT NULL,
                reported_id INTEGER NOT NULL,
                content TEXT,
                created_at INTEGER NOT NULL,
                status TEXT DEFAULT 'open',
                reviewed_by INTEGER,
                department_id INTEGER
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS ban_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER NOT NULL,
                requester_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                reason TEXT,
                status TEXT DEFAULT 'pending',
                reviewed_by INTEGER,
                created_at INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS bans (
                user_id INTEGER PRIMARY KEY,
                admin_id INTEGER NOT NULL,
                reason TEXT,
                created_at INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS blocks (
                blocker_id INTEGER NOT NULL,
                blocked_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(blocker_id, blocked_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                issuer_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                active INTEGER DEFAULT 1
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS department_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                department_id INTEGER,
                actor_id INTEGER NOT NULL,
                target_id INTEGER,
                action TEXT NOT NULL,
                details TEXT,
                created_at INTEGER NOT NULL
            )
            """
        ]

        for query in tables:
            await db.execute(query)

        # Миграции старых баз данных
        migrations = {
            "messages": [
                ("content", "TEXT")
            ],
            "reports": [
                ("content", "TEXT"),
                ("reviewed_by", "INTEGER"),
                ("department_id", "INTEGER")
            ],
            "admins": [
                ("department_id", "INTEGER"),
                ("position", "TEXT DEFAULT 'staff'"),
                ("appointed_by", "INTEGER"),
                ("appointed_at", "INTEGER")
            ]
        }

        for table, columns in migrations.items():

            async with db.execute(
                f"PRAGMA table_info({table})"
            ) as cursor:
                existing = {
                    row[1] for row in await cursor.fetchall()
                }

            for name, definition in columns:
                if name not in existing:
                    await db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                    )

        for name in DEFAULT_DEPARTMENTS:
            await db.execute(
                "INSERT OR IGNORE INTO departments(name) VALUES (?)",
                (name,)
            )

        await db.commit()


# =====================================================
# BASIC FUNCTIONS
# =====================================================

async def register_user(uid):
    await execute(
        "INSERT OR IGNORE INTO users VALUES (?, ?)",
        (uid, int(time.time()))
    )


async def get_level(uid):

    if uid == OWNER_ID:
        return 6

    result = await one(
        "SELECT level FROM admins WHERE user_id = ?",
        (uid,)
    )

    return result[0] if result else 0


async def get_admin(uid):
    return await one(
        """
        SELECT level, department_id, position
        FROM admins WHERE user_id = ?
        """,
        (uid,)
    )


async def get_department_name(department_id):

    if not department_id:
        return "Без отделения"

    result = await one(
        "SELECT name FROM departments WHERE id = ?",
        (department_id,)
    )

    return result[0] if result else "Неизвестное отделение"


async def is_banned(uid):
    return await one(
        "SELECT 1 FROM bans WHERE user_id = ?",
        (uid,)
    ) is not None


async def log_department(dept, actor, target, action, details=""):

    await execute(
        """
        INSERT INTO department_logs
        (department_id, actor_id, target_id, action, details, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (dept, actor, target, action, details, int(time.time()))
    )

    await channel_log(
        "ЖУРНАЛ ОТДЕЛЕНИЯ\n\n"
        f"Отделение: {await get_department_name(dept)}\n"
        f"Действие: {action}\n"
        f"Инициатор: {actor}\n"
        f"Пользователь: {target or '—'}\n"
        f"Подробности: {details or '—'}"
    )


async def channel_log(text):

    if not CHANNEL_ID:
        return

    try:
        await bot.send_message(CHANNEL_ID, text)
    except Exception:
        logging.exception("Ошибка публикации в канал")


async def is_blocked(sender, target):

    return await one(
        """
        SELECT 1 FROM blocks
        WHERE blocker_id = ? AND blocked_id = ?
        """,
        (target, sender)
    ) is not None


async def rate_allowed(uid):

    result = await one(
        """
        SELECT COUNT(*) FROM messages
        WHERE sender_id = ? AND created_at > ?
        """,
        (uid, int(time.time()) - 60)
    )

    return result[0] < 5


# =====================================================
# DEPARTMENT PERMISSIONS
# =====================================================

async def department_access(uid):

    if uid == OWNER_ID:
        return {
            "level": 6,
            "department_id": None,
            "position": "owner"
        }

    admin = await get_admin(uid)

    if not admin:
        return None

    return {
        "level": admin[0],
        "department_id": admin[1],
        "position": admin[2] or "staff"
    }


async def can_manage_department(actor, target):

    if actor == OWNER_ID:
        return True

    a = await department_access(actor)
    t = await department_access(target)

    if not a or not t:
        return False

    if a["department_id"] is None:
        return False

    if a["department_id"] != t["department_id"]:
        return False

    if a["position"] != "head":
        return False

    return t["position"] == "staff" and t["level"] < a["level"]


async def can_view_department(actor, dept_id):

    if actor == OWNER_ID:
        return True

    admin = await department_access(actor)

    if not admin:
        return False

    if admin["level"] < 1:
        return False

    return admin["department_id"] == dept_id


# =====================================================
# KEYBOARDS
# =====================================================

def main_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Моя ссылка",
                    callback_data="my_link"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Помощь",
                    callback_data="user_help"
                )
            ]
        ]
    )


def admin_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Жалобы",
                    callback_data="a:reports"
                ),
                InlineKeyboardButton(
                    text="Запросы на бан",
                    callback_data="a:requests"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Статистика",
                    callback_data="a:stats"
                ),
                InlineKeyboardButton(
                    text="Пользователи",
                    callback_data="a:users"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Моё отделение",
                    callback_data="d:home"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Администрация",
                    callback_data="a:staff"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Управление отделениями",
                    callback_data="d:list"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назначить администратора",
                    callback_data="a:appoint"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Выговор",
                    callback_data="a:warning"
                )
            ]
        ]
    )


def back_button(destination="admin"):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data=f"back:{destination}"
                )
            ]
        ]
    )


# =====================================================
# START
# =====================================================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):

    await state.clear()

    uid = message.from_user.id

    if await is_banned(uid):
        await message.answer("Ваш доступ к боту ограничен.")
        return

    await register_user(uid)

    args = message.text.split(maxsplit=1)

    if len(args) > 1:

        payload = args[1]

        if not payload.isdigit():
            await message.answer("Некорректная ссылка.")
            return

        target = int(payload)

        if target == uid:
            await message.answer("Нельзя отправить сообщение самому себе.")
            return

        if await is_banned(target):
            await message.answer("Получатель недоступен.")
            return

        await state.update_data(target_id=target)
        await state.set_state(States.message)

        await message.answer(
            "Вы можете отправить анонимное сообщение.\n\n"
            "Напишите текст или отправьте медиа.\n"
            "Для отмены используйте /cancel."
        )
        return

    link = await create_start_link(bot, str(uid), encode=False)

    await message.answer(
        "✦ ANONBOTSCHOOL15 ✦\n\n"
        "Добро пожаловать!\n"
        "Получайте и отправляйте анонимные сообщения.\n\n"
        f"Ваша ссылка:\n{link}",
        reply_markup=main_keyboard()
    )


@dp.callback_query(F.data == "my_link")
async def my_link(callback: CallbackQuery):

    link = await create_start_link(
        bot,
        str(callback.from_user.id),
        encode=False
    )

    await callback.message.answer(
        f"Ваша анонимная ссылка:\n{link}"
    )

    await callback.answer()


@dp.callback_query(F.data == "user_help")
async def user_help(callback: CallbackQuery):

    await callback.message.answer(
        "Как пользоваться ботом:\n\n"
        "1. Скопируйте личную ссылку.\n"
        "2. Поделитесь ею с друзьями.\n"
        "3. Получайте анонимные сообщения.\n"
        "4. Отвечайте через кнопку под сообщением.\n"
        "5. Используйте жалобу при нарушениях."
    )

    await callback.answer()


@dp.message(Command("myid"))
async def myid(message: Message):
    await message.answer(f"Ваш ID: {message.from_user.id}")


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "/start — главное меню\n"
        "/admin — администрация\n"
        "/myid — узнать ID\n"
        "/dept — моё отделение\n"
        "/cancel — отменить действие"
    )


@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):

    await state.clear()
    await message.answer("Действие отменено.")


# =====================================================
# ANONYMOUS MESSAGES
# =====================================================

def message_actions(sender_id):

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
                    text="Жалоба",
                    callback_data=f"report:{sender_id}"
                ),
                InlineKeyboardButton(
                    text="Заблокировать",
                    callback_data=f"block:{sender_id}"
                )
            ]
        ]
    )


@dp.message(States.message)
async def receive_message(message: Message, state: FSMContext):

    uid = message.from_user.id

    if await is_banned(uid):
        await state.clear()
        await message.answer("Ваш доступ ограничен.")
        return

    if message.text and message.text.startswith("/"):
        await message.answer("Используйте /cancel для отмены.")
        return

    data = await state.get_data()
    target = data.get("target_id")

    if not target:
        await state.clear()
        await message.answer("Ошибка. Откройте ссылку заново.")
        return

    if await is_blocked(uid, target):
        await state.clear()
        await message.answer("Получатель заблокировал вас.")
        return

    if not await rate_allowed(uid):
        await message.answer("Лимит: 5 сообщений в минуту.")
        return

    try:
        await bot.copy_message(
            target,
            message.chat.id,
            message.message_id,
            reply_markup=message_actions(uid)
        )

        content = (
            message.text
            or message.caption
            or "[Медиа-сообщение]"
        )

        await execute(
            """
            INSERT INTO messages(sender_id, target_id, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (uid, target, content[:4000], int(time.time()))
        )

        await message.answer("Сообщение отправлено анонимно.")
        await state.clear()

    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer("Не удалось доставить сообщение.")
        await state.clear()


@dp.callback_query(F.data.startswith("reply:"))
async def reply_start(callback: CallbackQuery, state: FSMContext):

    uid = callback.from_user.id
    target = int(callback.data.split(":")[1])

    if await is_banned(uid):
        await callback.answer("Доступ ограничен.", show_alert=True)
        return

    if uid == target:
        await callback.answer("Некорректное действие.")
        return

    await state.clear()
    await state.update_data(target_id=target)
    await state.set_state(States.reply)

    await callback.message.answer(
        "Напишите анонимный ответ.\n/cancel — отмена."
    )

    await callback.answer()


@dp.message(States.reply)
async def send_reply(message: Message, state: FSMContext):

    uid = message.from_user.id

    if await is_banned(uid):
        await state.clear()
        return

    if message.text and message.text.startswith("/"):
        await message.answer("Используйте /cancel.")
        return

    data = await state.get_data()
    target = data.get("target_id")

    if not target:
        await state.clear()
        await message.answer("Ошибка.")
        return

    if await is_blocked(uid, target):
        await state.clear()
        await message.answer("Получатель заблокировал вас.")
        return

    if not await rate_allowed(uid):
        await message.answer("Лимит: 5 сообщений в минуту.")
        return

    try:
        await bot.copy_message(
            target,
            message.chat.id,
            message.message_id,
            reply_markup=message_actions(uid)
        )

        content = message.text or message.caption or "[Медиа-сообщение]"

        await execute(
            """
            INSERT INTO messages(sender_id, target_id, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (uid, target, content[:4000], int(time.time()))
        )

        await message.answer("Ответ отправлен.")
        await state.clear()

    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer("Не удалось отправить ответ.")
        await state.clear()


# =====================================================
# BLOCK AND REPORT
# =====================================================

@dp.callback_query(F.data.startswith("block:"))
async def block_user(callback: CallbackQuery):

    uid = callback.from_user.id
    target = int(callback.data.split(":")[1])

    if uid == target:
        await callback.answer("Некорректное действие.")
        return

    await execute(
        """
        INSERT OR IGNORE INTO blocks VALUES (?, ?, ?)
        """,
        (uid, target, int(time.time()))
    )

    await callback.answer("Пользователь заблокирован.", show_alert=True)


@dp.callback_query(F.data.startswith("report:"))
async def report_user(callback: CallbackQuery):

    reporter = callback.from_user.id
    target = int(callback.data.split(":")[1])

    if reporter == target:
        await callback.answer("Некорректное действие.")
        return

    if await is_banned(reporter):
        await callback.answer("Доступ ограничен.", show_alert=True)
        return

    content = (
        callback.message.text
        or callback.message.caption
        or "[Медиа-сообщение]"
    )[:4000]

    dept = await one(
        "SELECT id FROM departments WHERE name = 'Модерация'"
    )

    dept_id = dept[0] if dept else None

    await execute(
        """
        INSERT INTO reports
        (reporter_id, reported_id, content, created_at, department_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (reporter, target, content, int(time.time()), dept_id)
    )

    await callback.answer(
        "Жалоба отправлена администрации.",
        show_alert=True
    )


# =====================================================
# ADMIN PANEL
# =====================================================

@dp.message(Command("admin"))
async def admin_panel(message: Message):

    level = await get_level(message.from_user.id)

    if level < 1:
        await message.answer("Нет доступа к администрации.")
        return

    await message.answer(
        "✦ ANONBOTSCHOOL15 ✦\n"
        "АДМИНИСТРАТИВНАЯ ПАНЕЛЬ\n\n"
        f"Уровень: {level}\n"
        f"Должность: {LEVELS[level]}",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data == "back:admin")
async def back_admin(callback: CallbackQuery):

    level = await get_level(callback.from_user.id)

    if level < 1:
        await callback.answer("Доступ запрещён.")
        return

    await callback.message.answer(
        "АДМИНИСТРАТИВНАЯ ПАНЕЛЬ",
        reply_markup=admin_keyboard()
    )

    await callback.answer()


# =====================================================
# GENERAL STATISTICS
# =====================================================

@dp.callback_query(F.data == "a:stats")
async def admin_stats(callback: CallbackQuery):

    if await get_level(callback.from_user.id) < 2:
        await callback.answer("Доступно со 2 уровня.", show_alert=True)
        return

    users = (await one("SELECT COUNT(*) FROM users"))[0]
    messages = (await one("SELECT COUNT(*) FROM messages"))[0]
    reports = (await one(
        "SELECT COUNT(*) FROM reports WHERE status='open'"
    ))[0]
    bans = (await one("SELECT COUNT(*) FROM bans"))[0]
    admins = (await one("SELECT COUNT(*) FROM admins"))[0]

    await callback.message.answer(
        "ОБЩАЯ СТАТИСТИКА\n\n"
        f"Пользователи: {users}\n"
        f"Сообщения: {messages}\n"
        f"Открытые жалобы: {reports}\n"
        f"Блокировки: {bans}\n"
        f"Администраторы: {admins}",
        reply_markup=back_button()
    )

    await callback.answer()


# =====================================================
# USER LIST
# =====================================================

@dp.callback_query(F.data == "a:users")
async def user_list(callback: CallbackQuery):

    if await get_level(callback.from_user.id) < 2:
        await callback.answer("Доступно со 2 уровня.", show_alert=True)
        return

    users = await all_rows(
        """
        SELECT user_id, created_at
        FROM users ORDER BY created_at DESC LIMIT 15
        """
    )

    count = (await one("SELECT COUNT(*) FROM users"))[0]

    text = f"ПОЛЬЗОВАТЕЛИ\nВсего: {count}\n\n"

    for uid, created in users:
        date = time.strftime("%d.%m.%Y", time.localtime(created))
        text += f"{uid} — {date}\n"

    await callback.message.answer(
        text,
        reply_markup=back_button()
    )

    await callback.answer()


# =====================================================
# DEPARTMENT HOME
# =====================================================

@dp.message(Command("dept"))
async def department_command(message: Message):

    await show_department(message.from_user.id, message)


async def show_department(uid, destination):

    access = await department_access(uid)

    if not access or not access
