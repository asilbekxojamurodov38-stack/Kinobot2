import asyncio
import hashlib
import logging
import os
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNELS = [c.strip() for c in os.getenv("CHANNELS", "").split(",") if c.strip()]
DATABASE_URL = os.getenv("DATABASE_URL", "")
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "")
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_PATH = "/webhook"
SECRET = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
pool = None


def clean_dsn(url: str) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "channel_binding"]
    return urlunparse(p._replace(query=urlencode(q)))


async def init_db(app):
    global pool
    pool = await asyncpg.create_pool(
        clean_dsn(DATABASE_URL),
        min_size=1,
        max_size=3,
        max_inactive_connection_lifetime=30,
        statement_cache_size=0,
    )
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS movies (
            code TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            poster TEXT,
            video TEXT,
            link TEXT
        )"""
    )
    await pool.execute(
        "CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY)"
    )


async def get_movie(code: str):
    return await pool.fetchrow(
        "SELECT code, title, description, poster, video, link FROM movies WHERE code = $1",
        code,
    )


async def save_user(user_id: int):
    await pool.execute(
        "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id
    )


# ------------------------ MAJBURIY OBUNA ------------------------
async def not_subscribed(user_id: int) -> list[str]:
    missing = []
    for ch in CHANNELS:
        try:
            member = await bot.get_chat_member(ch, user_id)
            if member.status in ("left", "kicked"):
                missing.append(ch)
        except Exception as e:
            logging.warning("Kanal tekshirib bo'lmadi %s: %s", ch, e)
    return missing


def sub_keyboard(missing: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"➕ {ch}", url=f"https://t.me/{ch.lstrip('@')}")]
        for ch in missing
    ]
    rows.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def ensure_subscribed(message: Message) -> bool:
    missing = await not_subscribed(message.from_user.id)
    if missing:
        await message.answer(
            "Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:",
            reply_markup=sub_keyboard(missing),
        )
        return False
    return True


@dp.callback_query(F.data == "check_sub")
async def check_sub(call: CallbackQuery):
    missing = await not_subscribed(call.from_user.id)
    if missing:
        await call.answer("Hali hamma kanalga obuna bo'lmadingiz!", show_alert=True)
    else:
        await call.message.answer("Rahmat! Endi kino kodini yuboring 🎬")
        await call.answer()


# ---------------------------- ADMIN ----------------------------
class AddMovie(StatesGroup):
    code = State()
    title = State()
    description = State()
    poster = State()
    video = State()
    link = State()


def is_admin(message: Message) -> bool:
    return message.from_user.id == ADMIN_ID


@dp.message(Command("add"))
async def add_start(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    await state.set_state(AddMovie.code)
    await message.answer("Kino kodini yuboring (masalan: 145). Bekor qilish: /cancel")


@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    await state.clear()
    await message.answer("Bekor qilindi.")


@dp.message(AddMovie.code, F.text)
async def add_code(message: Message, state: FSMContext):
    await state.update_data(code=message.text.strip())
    await state.set_state(AddMovie.title)
    await message.answer("Kino nomini yuboring:")


@dp.message(AddMovie.title, F.text)
async def add_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AddMovie.description)
    await message.answer("Tavsif yuboring (janr, yil, qisqacha mazmun):")


@dp.message(AddMovie.description, F.text)
async def add_desc(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await state.set_state(AddMovie.poster)
    await message.answer("Muqova rasmini yuboring (yoki /skip):")


@dp.message(AddMovie.poster, F.photo)
async def add_poster(message: Message, state: FSMContext):
    await state.update_data(poster=message.photo[-1].file_id)
    await state.set_state(AddMovie.video)
    await message.answer("Kino videosini yuboring (yoki /skip):")


@dp.message(AddMovie.poster, Command("skip"))
async def skip_poster(message: Message, state: FSMContext):
    await state.update_data(poster=None)
    await state.set_state(AddMovie.video)
    await message.answer("Kino videosini yuboring (yoki /skip):")


@dp.message(AddMovie.video, F.video | F.document)
async def add_video(message: Message, state: FSMContext):
    file_id = message.video.file_id if message.video else message.document.file_id
    await state.update_data(video=file_id)
    await state.set_state(AddMovie.link)
    await message.answer("Havola yuboring yoki /skip:")


@dp.message(AddMovie.video, Command("skip"))
async def skip_video(message: Message, state: FSMContext):
    await state.update_data(video=None)
    await state.set_state(AddMovie.link)
    await message.answer("Havola yuboring yoki /skip:")


async def finish_add(message: Message, state: FSMContext, link):
    data = await state.get_data()
    await pool.execute(
        """INSERT INTO movies (code, title, description, poster, video, link)
           VALUES ($1, $2, $3, $4, $5, $6)
           ON CONFLICT (code) DO UPDATE SET
             title = EXCLUDED.title, description = EXCLUDED.description,
             poster = EXCLUDED.poster, video = EXCLUDED.video, link = EXCLUDED.link""",
        data["code"],
        data["title"],
        data.get("description"),
        data.get("poster"),
        data.get("video"),
        link,
    )
    await state.clear()
    await message.answer(f"✅ Saqlandi: {data['code']} — {data['title']}")


@dp.message(AddMovie.link, Command("skip"))
async def skip_link(message: Message, state: FSMContext):
    await finish_add(message, state, None)


@dp.message(AddMovie.link, F.text)
async def add_link(message: Message, state: FSMContext):
    await finish_add(message, state, message.text.strip())


@dp.message(Command("del"))
async def delete_movie(message: Message):
    if not is_admin(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Foydalanish: /del 145")
        return
    await pool.execute("DELETE FROM movies WHERE code = $1", parts[1].strip())
    await message.answer("O'chirildi (agar bunday kod bo'lgan bo'lsa).")


@dp.message(Command("stats"))
async def stats(message: Message):
    if not is_admin(message):
        return
    users = await pool.fetchval("SELECT COUNT(*) FROM users")
    movies = await pool.fetchval("SELECT COUNT(*) FROM movies")
    await message.answer(f"👥 Foydalanuvchilar: {users}\n🎬 Kinolar: {movies}")


# ---------------------------- FOYDALANUVCHI ----------------------------
@dp.message(CommandStart())
async def start(message: Message):
    await save_user(message.from_user.id)
    if not await ensure_subscribed(message):
        return
    await message.answer("Salom! 🎬 Kino kodini yuboring, men topib beraman.")


@dp.message(F.text, ~F.text.startswith("/"))
async def get_code(message: Message):
    await save_user(message.from_user.id)
    if not await ensure_subscribed(message):
        return

    movie = await get_movie(message.text.strip())
    if not movie:
        await message.answer("❌ Bunday kodli kino topilmadi.")
        return

    caption = (
        f"🎬 <b>{movie['title']}</b>\n\n"
        f"{(movie['description'] or '')[:700]}\n\n🔢 Kod: {movie['code']}"
    )
    keyboard = None
    if movie["link"]:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="▶️ Ko'rish", url=movie["link"])]]
        )

    if movie["poster"]:
        await message.answer_photo(
            movie["poster"], caption=caption, parse_mode="HTML", reply_markup=keyboard
        )
    else:
        await message.answer(caption, parse_mode="HTML", reply_markup=keyboard)

    if movie["video"]:
        try:
            await message.answer_video(movie["video"])
        except Exception:
            await message.answer_document(movie["video"])


# ---------------------------- SERVER ----------------------------
async def on_startup(bot: Bot):
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}{WEBHOOK_PATH}", secret_token=SECRET)
        logging.info("Webhook o'rnatildi: %s%s", BASE_URL, WEBHOOK_PATH)


async def health(request):
    return web.Response(text="OK")


def main():
    dp.startup.register(on_startup)
    app = web.Application()
    app.router.add_get("/", health)
    app.on_startup.append(init_db)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=SECRET).register(
        app, path=WEBHOOK_PATH
    )
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
