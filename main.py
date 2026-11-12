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

async def show_department(uid, destination):

    access = await department_access(uid)

    if not access:
        await destination.answer("У вас нет доступа к отделениям.")
        return

    dept_id = access["department_id"]

    if uid == OWNER_ID:
        departments = await all_rows(
            "SELECT id, name FROM departments ORDER BY id"
        )

        text = "УПРАВЛЕНИЕ ОТДЕЛЕНИЯМИ\n\n"

        for did, name in departments:
            text += f"#{did} — {name}\n"

        await destination.answer(
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=name,
                            callback_data=f"d:show:{did}"
                        )
                    ]
                    for did, name in departments
                ] + [
                    [
                        InlineKeyboardButton(
                            text="Создать отделение",
                            callback_data="d:create"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Назад",
                            callback_data="back:admin"
                        )
                    ]
                ]
            )
        )
        return

    if not dept_id:
        await destination.answer("Вы не состоите в отделении.")
        return

    await show_department_info(uid, dept_id, destination)


async def show_department_info(uid, dept_id, destination):

    access = await department_access(uid)

    if not access:
        await destination.answer("Нет доступа.")
        return

    if uid != OWNER_ID and access["department_id"] != dept_id:
        await destination.answer("Вы не состоите в этом отделении.")
        return

    department = await one(
        "SELECT name FROM departments WHERE id = ?",
        (dept_id,)
    )

    if not department:
        await destination.answer("Отделение не найдено.")
        return

    employees = await all_rows(
        """
        SELECT user_id, level, position
        FROM admins
        WHERE department_id = ?
        ORDER BY
            CASE position
                WHEN 'head' THEN 1
                WHEN 'deputy' THEN 2
                ELSE 3
            END,
            level DESC
        """,
        (dept_id,)
    )

    text = (
        f"ОТДЕЛЕНИЕ: {department[0]}\n\n"
        f"Сотрудников: {len(employees)}\n\n"
    )

    for user_id, level, position in employees:

        try:
            chat = await bot.get_chat(user_id)

            if chat.username:
                username = f"@{chat.username}"
            else:
                username = chat.full_name

        except Exception:
            username = f"ID {user_id}"

        position_name = POSITIONS.get(position, "Сотрудник")
        level_name = LEVELS.get(level, "Неизвестно")

        text += (
            f"{position_name}\n"
            f"{username}\n"
            f"{level_name}\n\n"
        )

    buttons = []

    if uid == OWNER_ID:
        buttons.extend([
            [
                InlineKeyboardButton(
                    text="Назначить начальника",
                    callback_data=f"d:appoint:{dept_id}:head"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назначить заместителя",
                    callback_data=f"d:appoint:{dept_id}:deputy"
                )
            ],
            [
                InlineKeyboardButton(
                    text="История отделения",
                    callback_data=f"d:logs:{dept_id}"
                )
            ]
        ])

    buttons.append([
        InlineKeyboardButton(
            text="Назад",
            callback_data="d:list" if uid == OWNER_ID else "d:home"
        )
    ])

    await destination.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data == "d:home")
async def department_home_callback(callback: CallbackQuery):

    await show_department(callback.from_user.id, callback.message)
    await callback.answer()


@dp.callback_query(F.data == "d:list")
async def department_list_callback(callback: CallbackQuery):

    if callback.from_user.id != OWNER_ID:
        await callback.answer("Доступно только создателю.", show_alert=True)
        return

    await show_department(callback.from_user.id, callback.message)
    await callback.answer()


@dp.callback_query(F.data.startswith("d:show:"))
async def department_show_callback(callback: CallbackQuery):

    if callback.from_user.id != OWNER_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    dept_id = int(callback.data.split(":")[2])

    await show_department_info(
        callback.from_user.id,
        dept_id,
        callback.message
    )

    await callback.answer()


# =====================================================
# DEPARTMENT CREATION
# =====================================================

@dp.callback_query(F.data == "d:create")
async def department_create_start(
    callback: CallbackQuery,
    state: FSMContext
):

    if callback.from_user.id != OWNER_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    await state.clear()
    await state.set_state(States.department_name)

    await callback.message.answer(
        "Введите название нового отделения:"
    )

    await callback.answer()


@dp.message(States.department_name)
async def department_create_finish(
    message: Message,
    state: FSMContext
):

    if message.from_user.id != OWNER_ID:
        await state.clear()
        return

    name = (message.text or "").strip()

    if len(name) < 3 or len(name) > 40:
        await message.answer("Название должно содержать от 3 до 40 символов.")
        return

    try:
        await execute(
            "INSERT INTO departments(name) VALUES (?)",
            (name,)
        )
    except Exception:
        await message.answer("Такое отделение уже существует.")
        return

    department = await one(
        "SELECT id FROM departments WHERE name = ?",
        (name,)
    )

    await log_department(
        department[0],
        message.from_user.id,
        None,
        "Создание отделения",
        name
    )

    await state.clear()
    await message.answer(f"Отделение «{name}» создано.")


# =====================================================
# DEPARTMENT HEADS AND DEPUTIES
# =====================================================

@dp.callback_query(F.data.startswith("d:appoint:"))
async def department_appoint_start(
    callback: CallbackQuery,
    state: FSMContext
):

    if callback.from_user.id != OWNER_ID:
        await callback.answer("Только создатель может назначать руководителей.")
        return

    parts = callback.data.split(":")

    dept_id = int(parts[2])
    position = parts[3]

    if position not in ("head", "deputy"):
        await callback.answer("Некорректная должность.")
        return

    department = await one(
        "SELECT name FROM departments WHERE id = ?",
        (dept_id,)
    )

    if not department:
        await callback.answer("Отделение не найдено.")
        return

    await state.clear()
    await state.update_data(
        department_id=dept_id,
        position=position
    )
    await state.set_state(States.appoint_id)

    await callback.message.answer(
        f"Введите Telegram ID сотрудника для назначения на должность "
        f"«{POSITIONS[position]}» в отделение «{department[0]}»:"
    )

    await callback.answer()



@dp.message(States.appoint_id)
async def department_appoint_finish(
    message: Message,
    state: FSMContext
):
    if message.from_user.id != OWNER_ID:
        await state.clear()
        return

    if not message.text or not message.text.strip().isdigit():
        await message.answer("Введите корректный Telegram ID.")
        return

    target = int(message.text.strip())
    data = await state.get_data()

    dept_id = data.get("department_id")
    position = data.get("position")

    if not dept_id or position not in ("head", "deputy"):
        await message.answer("Ошибка назначения. Начните заново.")
        await state.clear()
        return

    if target == OWNER_ID:
        await message.answer("Создателя нельзя назначить сотрудником.")
        return

    department = await one(
        "SELECT name FROM departments WHERE id = ?",
        (dept_id,)
    )

    if not department:
        await message.answer("Отделение не найдено.")
        await state.clear()
        return

    old_admin = await get_admin(target)

    level = old_admin[0] if old_admin else 1

    await execute(
        """
        INSERT INTO admins
        (user_id, level, department_id, position, appointed_by, appointed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            level = excluded.level,
            department_id = excluded.department_id,
            position = excluded.position,
            appointed_by = excluded.appointed_by,
            appointed_at = excluded.appointed_at
        """,
        (
            target,
            level,
            dept_id,
            position,
            message.from_user.id,
            int(time.time())
        )
    )

    await log_department(
        dept_id,
        message.from_user.id,
        target,
        "Назначение руководителя",
        POSITIONS[position]
    )

    username = await user_display(target)

    try:
        await bot.send_message(
            target,
            "Вас назначили сотрудником ANONBOTSCHOOL15.\n\n"
            f"Отделение: {department[0]}\n"
            f"Должность: {POSITIONS[position]}\n"
            f"Уровень: {LEVELS[level]}"
        )
    except Exception:
        pass

    await state.clear()

    await message.answer(
        "Назначение выполнено!\n\n"
        f"Пользователь: {username}\n"
        f"Отделение: {department[0]}\n"
        f"Должность: {POSITIONS[position]}\n"
        f"Уровень: {LEVELS[level]}"
    )



# =====================================================
# DEPARTMENT LOGS
# =====================================================

@dp.callback_query(F.data.startswith("d:logs:"))
async def department_logs_callback(callback: CallbackQuery):

    if callback.from_user.id != OWNER_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    dept_id = int(callback.data.split(":")[2])

    logs = await all_rows(
        """
        SELECT actor_id, target_id, action, details, created_at
        FROM department_logs
        WHERE department_id = ?
        ORDER BY id DESC
        LIMIT 20
        """,
        (dept_id,)
    )

    text = "ИСТОРИЯ ОТДЕЛЕНИЯ\n\n"

    if not logs:
        text += "История пока пуста."

    for actor, target, action, details, created in logs:

        date = time.strftime(
            "%d.%m.%Y %H:%M",
            time.localtime(created)
        )

        text += (
            f"{date}\n"
            f"Действие: {action}\n"
            f"Инициатор: {actor}\n"
            f"Пользователь: {target or '—'}\n"
            f"Подробности: {details or '—'}\n\n"
        )

    await callback.message.answer(
        text[:4000],
        reply_markup=back_button()
    )

    await callback.answer()


# =====================================================
# ADMIN STAFF LIST
# =====================================================

async def user_display(user_id):

    try:
        chat = await bot.get_chat(user_id)

        if chat.username:
            return f"@{chat.username}"

        return f"{chat.full_name} (ID: {user_id})"

    except Exception:
        return f"ID: {user_id}"


@dp.callback_query(F.data == "a:staff")
async def admin_staff(callback: CallbackQuery):

    if await get_level(callback.from_user.id) < 4:
        await callback.answer(
            "Просмотр администрации доступен с 4 уровня.",
            show_alert=True
        )
        return

    admins = await all_rows(
        """
        SELECT user_id, level, department_id, position
        FROM admins
        ORDER BY level DESC
        LIMIT 40
        """
    )

    text = "АДМИНИСТРАЦИЯ\n\n"

    for uid, level, dept_id, position in admins:

        username = await user_display(uid)
        department = await get_department_name(dept_id)

        text += (
            f"{username}\n"
            f"{LEVELS.get(level, 'Неизвестно')}\n"
            f"{POSITIONS.get(position, 'Сотрудник')}\n"
            f"Отделение: {department}\n\n"
        )

    if not admins:
        text += "Администраторов пока нет."

    await callback.message.answer(
        text[:4000],
        reply_markup=back_button()
    )

    await callback.answer()


# =====================================================
# ADMIN APPOINTMENT
# =====================================================

@dp.callback_query(F.data == "a:appoint")
async def appoint_start(
    callback: CallbackQuery,
    state: FSMContext
):

    if callback.from_user.id != OWNER_ID:
        await callback.answer(
            "Назначать администраторов может только создатель.",
            show_alert=True
        )
        return

    await state.clear()
    await state.set_state(States.appoint_id)

    await state.update_data(action="admin_appoint")

    await callback.message.answer(
        "Введите Telegram ID пользователя, которого хотите назначить:"
    )

    await callback.answer()


@dp.message(States.appoint_id)
async def appoint_id_handler(
    message: Message,
    state: FSMContext
):

    if message.from_user.id != OWNER_ID:
        await state.clear()
        return

    data = await state.get_data()

    if data.get("action") == "admin_appoint":

        if not message.text or not message.text.strip().isdigit():
            await message.answer("Введите числовой Telegram ID.")
            return

        target = int(message.text.strip())

        if target == OWNER_ID:
            await message.answer("Вы уже являетесь создателем.")
            await state.clear()
            return

        await state.update_data(target_id=target)
        await state.set_state(States.appoint_level)

        buttons = [
            [
                InlineKeyboardButton(
                    text=f"{level} уровень — {name}",
                    callback_data=f"a:setlevel:{level}"
                )
            ]
            for level, name in LEVELS.items()
            if level < 6
        ]

        await message.answer(
            "Выберите уровень администратора:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=buttons
            )
        )
        return

    # Если действие не является назначением администратора,
    # обработчик отдела продолжит работу отдельным сценарием.
    await message.answer(
        "Не удалось определить действие. Используйте /cancel."
    )


@dp.callback_query(F.data.startswith("a:setlevel:"))
async def appoint_level_callback(
    callback: CallbackQuery,
    state: FSMContext
):

    if callback.from_user.id != OWNER_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    level = int(callback.data.split(":")[2])

    if level not in LEVELS or level == 6:
        await callback.answer("Некорректный уровень.")
        return

    data = await state.get_data()
    target = data.get("target_id")

    if not target:
        await callback.answer("Сначала укажите пользователя.")
        return

    await state.update_data(level=level)
    await state.set_state(States.appoint_department)

    departments = await all_rows(
        "SELECT id, name FROM departments ORDER BY id"
    )

    buttons = [
        [
            InlineKeyboardButton(
                text=name,
                callback_data=f"a:setdept:{dept_id}"
            )
        ]
        for dept_id, name in departments
    ]

    buttons.append([
        InlineKeyboardButton(
            text="Без отделения",
            callback_data="a:setdept:0"
        )
    ])

    await callback.message.answer(
        "Выберите отделение:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        )
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("a:setdept:"))
async def appoint_department_callback(
    callback: CallbackQuery,
    state: FSMContext
):

    if callback.from_user.id != OWNER_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    dept_id = int(callback.data.split(":")[2])

    if dept_id:
        department = await one(
            "SELECT id FROM departments WHERE id = ?",
            (dept_id,)
        )

        if not department:
            await callback.answer("Отделение не найдено.")
            return

    data = await state.get_data()

    target = data.get("target_id")
    level = data.get("level")

    if not target or not level:
        await callback.answer("Данные назначения потеряны.")
        await state.clear()
        return

    await execute(
        """
        INSERT INTO admins
        (user_id, level, department_id, position, appointed_by, appointed_at)
        VALUES (?, ?, ?, 'staff', ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            level = excluded.level,
            department_id = excluded.department_id,
            position = 'staff',
            appointed_by = excluded.appointed_by,
            appointed_at = excluded.appointed_at
        """,
        (
            target,
            level,
            dept_id or None,
            callback.from_user.id,
            int(time.time())
        )
    )

    department_name = await get_department_name(dept_id)

    username = await user_display(target)

    await channel_log(
        "НОВОЕ НАЗНАЧЕНИЕ\n\n"
        f"Сотрудник: {username}\n"
        f"Уровень: {LEVELS[level]}\n"
        f"Отделение: {department_name}\n"
        f"Назначил: {await user_display(callback.from_user.id)}"
    )

    try:
        await bot.send_message(
            target,
            "Вас назначили администратором ANONBOTSCHOOL15.\n\n"
            f"Уровень: {LEVELS[level]}\n"
            f"Отделение: {department_name}"
        )
    except Exception:
        pass

    await state.clear()

    await callback.message.answer(
        "Администратор успешно назначен.\n\n"
        f"Пользователь: {username}\n"
        f"Уровень: {LEVELS[level]}\n"
        f"Отделение: {department_name}"
    )

    await callback.answer()


# =====================================================
# REPORTS
# =====================================================

@dp.callback_query(F.data == "a:reports")
async def reports_list(callback: CallbackQuery):

    if await get_level(callback.from_user.id) < 1:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    reports = await all_rows(
        """
        SELECT id, reporter_id, reported_id, content, created_at
        FROM reports
        WHERE status = 'open'
        ORDER BY id DESC
        LIMIT 15
        """
    )

    if not reports:
        await callback.message.answer(
            "Открытых жалоб нет.",
            reply_markup=back_button()
        )
        await callback.answer()
        return

    buttons = []

    for report_id, reporter, reported, content, created in reports:

        buttons.append([
            InlineKeyboardButton(
                text=f"Жалоба #{report_id} — пользователь {reported}",
                callback_data=f"a:report:{report_id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="Назад",
            callback_data="back:admin"
        )
    ])

    await callback.message.answer(
        f"ОТКРЫТЫЕ ЖАЛОБЫ: {len(reports)}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        )
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("a:report:"))
async def report_details(callback: CallbackQuery):

    level = await get_level(callback.from_user.id)

    if level < 1:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    report_id = int(callback.data.split(":")[2])

    report = await one(
        """
        SELECT reporter_id, reported_id, content, created_at, status
        FROM reports WHERE id = ?
        """,
        (report_id,)
    )

    if not report:
        await callback.answer("Жалоба не найдена.")
        return

    reporter, reported, content, created, status = report

    date = time.strftime(
        "%d.%m.%Y %H:%M",
        time.localtime(created)
    )

    text = (
        f"ЖАЛОБА #{report_id}\n\n"
        f"Отправитель: {await user_display(reporter)}\n"
        f"На пользователя: {await user_display(reported)}\n"
        f"Дата: {date}\n"
        f"Статус: {status}\n\n"
        f"Содержание жалобы:\n{content or 'Текст отсутствует'}"
    )

    buttons = []

    if level >= 3:
        buttons.append([
            InlineKeyboardButton(
                text="Заблокировать пользователя",
                callback_data=f"a:ban:{reported}:{report_id}"
            )
        ])
    else:
        buttons.append([
            InlineKeyboardButton(
                text="Запросить блокировку",
                callback_data=f"a:requestban:{report_id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="Закрыть жалобу",
            callback_data=f"a:closereport:{report_id}"
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            text="Назад",
            callback_data="a:reports"
        )
    ])

    await callback.message.answer(
        text[:4000],
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        )
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("a:closereport:"))
async def close_report(callback: CallbackQuery):

    if await get_level(callback.from_user.id) < 1:
        await callback.answer("Нет доступа.")
        return

    report_id = int(callback.data.split(":")[2])

    await execute(
        """
        UPDATE reports
        SET status = 'closed', reviewed_by = ?
        WHERE id = ?
        """,
        (callback.from_user.id, report_id)
    )

    await callback.message.answer("Жалоба закрыта.")
    await callback.answer()


# =====================================================
# BAN REQUESTS
# =====================================================

@dp.callback_query(F.data.startswith("a:requestban:"))
async def create_ban_request(callback: CallbackQuery):

    level = await get_level(callback.from_user.id)

    if level not in (1, 2):
        await callback.answer(
            "Эта функция предназначена для 1 и 2 уровня.",
            show_alert=True
        )
        return

    report_id = int(callback.data.split(":")[2])

    report = await one(
        """
        SELECT reported_id, content, status
        FROM reports WHERE id = ?
        """,
        (report_id,)
    )

    if not report or report[2] != "open":
        await callback.answer("Жалоба недоступна.")
        return

    target, reason, status = report

    await execute(
        """
        INSERT INTO ban_requests
        (report_id, requester_id, target_id, reason, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (
            report_id,
            callback.from_user.id,
            target,
            reason,
            int(time.time())
        )
    )

    await channel_log(
        "ЗАПРОС НА БЛОКИРОВКУ\n\n"
        f"Жалоба: #{report_id}\n"
        f"Пользователь: {await user_display(target)}\n"
        f"Запросил: {await user_display(callback.from_user.id)}"
    )

    await callback.message.answer(
        "Запрос на блокировку отправлен старшей администрации."
    )

    await callback.answer()


@dp.callback_query(F.data == "a:requests")
async def ban_requests_list(callback: CallbackQuery):

    if await get_level(callback.from_user.id) < 3:
        await callback.answer(
            "Запросы доступны с 3 уровня.",
            show_alert=True
        )
        return

    requests = await all_rows(
        """
        SELECT id, report_id, requester_id, target_id, reason
        FROM ban_requests
        WHERE status = 'pending'
        ORDER BY id DESC
        LIMIT 15
        """
    )

    if not requests:
        await callback.message.answer(
            "Ожидающих запросов нет.",
            reply_markup=back_button()
        )
        await callback.answer()
        return

    buttons = []

    for request_id, report_id, requester, target, reason in requests:
        buttons.append([
            InlineKeyboardButton(
                text=f"Запрос #{request_id} — {target}",
                callback_data=f"a:request:{request_id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="Назад",
            callback_data="back:admin"
        )
    ])

    await callback.message.answer(
        "ЗАПРОСЫ НА БЛОКИРОВКУ",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        )
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("a:request:"))
async def ban_request_details(callback: CallbackQuery):

    if await get_level(callback.from_user.id) < 3:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    request_id = int(callback.data.split(":")[2])

    request = await one(
        """
        SELECT report_id, requester_id, target_id, reason, status
        FROM ban_requests WHERE id = ?
        """,
        (request_id,)
    )

    if not request:
        await callback.answer("Запрос не найден.")
        return

    report_id, requester, target, reason, status = request

    if status != "pending":
        await callback.answer("Запрос уже обработан.")
        return

    text = (
        f"ЗАПРОС #{request_id}\n\n"
        f"Жалоба: #{report_id}\n"
        f"Запросил: {await user_display(requester)}\n"
        f"Пользователь: {await user_display(target)}\n\n"
        f"Причина:\n{reason or 'Не указана'}"
    )

    buttons = [
        [
            InlineKeyboardButton(
                text="Одобрить и заблокировать",
                callback_data=f"a:approve:{request_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="Отклонить",
                callback_data=f"a:reject:{request_id}"
            )
        ]
    ]

    await callback.message.answer(
        text[:4000],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("a:approve:"))
async def approve_ban_request(callback: CallbackQuery):

    if await get_level(callback.from_user.id) < 3:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    request_id = int(callback.data.split(":")[2])

    request = await one(
        """
        SELECT target_id, reason, report_id, status
        FROM ban_requests WHERE id = ?
        """,
        (request_id,)
    )

    if not request or request[3] != "pending":
        await callback.answer("Запрос уже обработан.")
        return

    target, reason, report_id, status = request

    await execute(
        """
        INSERT OR REPLACE INTO bans(user_id, admin_id, reason, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (target, callback.from_user.id, reason, int(time.time()))
    )

    await execute(
        """
        UPDATE ban_requests
        SET status = 'approved', reviewed_by = ?
        WHERE id = ?
        """,
        (callback.from_user.id, request_id)
    )

    await execute(
        """
        UPDATE reports
        SET status = 'closed', reviewed_by = ?
        WHERE id = ?
        """,
        (callback.from_user.id, report_id)
    )

    try:
        await bot.send_message(
            target,
            "Ваш доступ к ANONBOTSCHOOL15 ограничен администрацией."
        )
    except Exception:
        pass

    await channel_log(
        "ПОЛЬЗОВАТЕЛЬ ЗАБЛОКИРОВАН\n\n"
        f"Пользователь: {await user_display(target)}\n"
        f"Администратор: {await user_display(callback.from_user.id)}\n"
        f"Причина: {reason or 'Не указана'}"
    )

    await callback.message.answer("Запрос одобрен. Пользователь заблокирован.")
    await callback.answer()


@dp.callback_query(F.data.startswith("a:reject:"))
async def reject_ban_request(callback: CallbackQuery):

    if await get_level(callback.from_user.id) < 3:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    request_id = int(callback.data.split(":")[2])

    await execute(
        """
        UPDATE ban_requests
        SET status = 'rejected', reviewed_by = ?
        WHERE id = ? AND status = 'pending'
        """,
        (callback.from_user.id, request_id)
    )

    await callback.message.answer("Запрос отклонён.")
    await callback.answer()


# =====================================================
# DIRECT BAN
# =====================================================

@dp.callback_query(F.data.startswith("a:ban:"))
async def direct_ban(callback: CallbackQuery):

    if await get_level(callback.from_user.id) < 3:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")

    target = int(parts[2])
    report_id = int(parts[3])

    if target == OWNER_ID:
        await callback.answer("Нельзя заблокировать создателя.")
        return

    report = await one(
        "SELECT content FROM reports WHERE id = ?",
        (report_id,)
    )

    reason = report[0] if report else "Блокировка по жалобе"

    await execute(
        """
        INSERT OR REPLACE INTO bans(user_id, admin_id, reason, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (target, callback.from_user.id, reason, int(time.time()))
    )

    await execute(
        """
        UPDATE reports
        SET status = 'closed', reviewed_by = ?
        WHERE id = ?
        """,
        (callback.from_user.id, report_id)
    )

    try:
        await bot.send_message(
            target,
            "Ваш доступ к ANONBOTSCHOOL15 ограничен администрацией."
        )
    except Exception:
        pass

    await channel_log(
        "БЛОКИРОВКА ПО ЖАЛОБЕ\n\n"
        f"Пользователь: {await user_display(target)}\n"
        f"Администратор: {await user_display(callback.from_user.id)}\n"
        f"Жалоба: #{report_id}"
    )

    await callback.message.answer("Пользователь заблокирован.")
    await callback.answer()


# =====================================================
# WARNINGS
# =====================================================

@dp.callback_query(F.data == "a:warning")
async def warning_start(
    callback: CallbackQuery,
    state: FSMContext
):

    if await get_level(callback.from_user.id) < 4:
        await callback.answer(
            "Выдавать выговоры может администрация с 4 уровня.",
            show_alert=True
        )
        return

    await state.clear()
    await state.set_state(States.warning_id)

    await callback.message.answer(
        "Введите Telegram ID администратора, которому хотите выдать выговор:"
    )

    await callback.answer()


@dp.message(States.warning_id)
async def warning_id_handler(
    message: Message,
    state: FSMContext
):

    if await get_level(message.from_user.id) < 4:
        await state.clear()
        return

    if not message.text or not message.text.strip().isdigit():
        await message.answer("Введите числовой Telegram ID.")
        return

    target = int(message.text.strip())

    if target == OWNER_ID:
        await message.answer("Нельзя выдать выговор создателю.")
        return

    admin = await get_admin(target)

    if not admin:
        await message.answer("Этот пользователь не является администратором.")
        return

    if admin[0] >= await get_level(message.from_user.id):
        await message.answer(
            "Нельзя выдать выговор администратору равного или более высокого уровня."
        )
        return

    await state.update_data(warning_target=target)
    await state.set_state(States.warning_reason)

    await message.answer("Укажите причину выговора:")


@dp.message(States.warning_reason)
async def warning_reason_handler(
    message: Message,
    state: FSMContext
):

    if await get_level(message.from_user.id) < 4:
        await state.clear()
        return

    reason = (message.text or "").strip()

    if len(reason) < 3:
        await message.answer("Укажите более подробную причину.")
        return

    data = await state.get_data()
    target = data.get("warning_target")

    if not target:
        await state.clear()
        await message.answer("Ошибка назначения.")
        return

    await execute(
        """
        INSERT INTO warnings(admin_id, issuer_id, reason, created_at, active)
        VALUES (?, ?, ?, ?, 1)
        """,
        (target, message.from_user.id, reason, int(time.time()))
    )

    await channel_log(
        "ВЫДАН ВЫГОВОР\n\n"
        f"Администратор: {await user_display(target)}\n"
        f"Выдал: {await user_display(message.from_user.id)}\n"
        f"Причина: {reason}"
    )

    try:
        await bot.send_message(
            target,
            f"Вам выдан выговор.\nПричина: {reason}"
        )
    except Exception:
        pass

    await state.clear()
    await message.answer("Выговор успешно выдан.")


# =====================================================
# BACK TO ADMIN
# =====================================================

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
# UNKNOWN CALLBACK
# =====================================================

@dp.callback_query()
async def unknown_callback(callback: CallbackQuery):

    await callback.answer(
        "Эта кнопка больше недоступна.",
        show_alert=True
    )


# =====================================================
# START BOT
# =====================================================

async def main():

    await init_db()

    logging.info("Database initialized")
    logging.info("Bot is running!")

    await dp.start_polling(bot)


if __name__ == "__main__":

    try:
        asyncio.run(main())

    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped")

    finally:
        pass
