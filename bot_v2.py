import asyncio
import logging
import os
import re
import shutil
import tempfile
import traceback

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ====================== НАСТРОЙКИ ======================
# Вставь токен СЮДА, в кавычках (без os.getenv!)
BOT_TOKEN = "9239456665:AAGPZLjsrWp0wZ6HaP8YB4ZlZ2IXAyRUX3E"

# Каналы спонсоров. Бот должен быть АДМИНИСТРАТОРОМ в каждом канале.
SPONSORS = [
    {"title": "Спонсор 1", "username": "@unixsite"},
    {"title": "Спонсор 2", "username": "@historyneww"},
]

COOPERATION_TEXT = "🤝 По вопросам сотрудничества пишите: @historylande"
SUBSCRIPTIONS_TEXT = "💎 Раздел подписок скоро появится. Следите за обновлениями!"

# Адрес локального Telegram Bot API сервера.
# Пусто "" = обычный режим (лимит 50 МБ).
# Например "http://localhost:8081" = лимит 2 ГБ (см. инструкцию).
LOCAL_API_URL = ""

# Файл с куки для Instagram/YouTube (формат Netscape). Положи рядом с ботом.
COOKIES_FILE = "cookies.txt"
# ========================================================

MAX_FILE_SIZE = (1990 if LOCAL_API_URL else 49) * 1024 * 1024

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()

URL_RE = re.compile(r"https?://\S+")

verified_users: set[int] = set()
user_links: dict[int, str] = {}


# ---------------------- Клавиатуры ----------------------
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤝 Сотрудничество", callback_data="coop")],
            [InlineKeyboardButton(text="💎 Подписки", callback_data="subs")],
            [InlineKeyboardButton(text="🎬 Скачать видео без водяных знаков", callback_data="download")],
        ]
    )


def sponsors_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"📢 {s['title']}",
            url=f"https://t.me/{s['username'].lstrip('@')}",
        )]
        for s in SPONSORS
    ]
    rows.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_subs")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def quality_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="480p", callback_data="q_480"),
                InlineKeyboardButton(text="720p", callback_data="q_720"),
                InlineKeyboardButton(text="1080p", callback_data="q_1080"),
            ]
        ]
    )


# ---------------------- Проверка подписки ----------------------
async def is_subscribed(bot: Bot, user_id: int) -> bool:
    for s in SPONSORS:
        try:
            member = await bot.get_chat_member(chat_id=s["username"], user_id=user_id)
            if member.status not in ("member", "administrator", "creator"):
                return False
        except Exception as e:
            logging.warning("Не удалось проверить %s: %s", s["username"], e)
            return False
    return True


# ---------------------- Хендлеры ----------------------
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("👋 Привет! Выбери действие:", reply_markup=main_menu())


@dp.callback_query(F.data == "back")
async def cb_back(call: CallbackQuery):
    await call.message.edit_text("👋 Привет! Выбери действие:", reply_markup=main_menu())
    await call.answer()


@dp.callback_query(F.data == "coop")
async def cb_coop(call: CallbackQuery):
    await call.message.answer(COOPERATION_TEXT)
    await call.answer()


@dp.callback_query(F.data == "subs")
async def cb_subs(call: CallbackQuery):
    await call.message.answer(SUBSCRIPTIONS_TEXT)
    await call.answer()


@dp.callback_query(F.data == "download")
async def cb_download(call: CallbackQuery, bot: Bot):
    if call.from_user.id in verified_users and await is_subscribed(bot, call.from_user.id):
        await call.message.answer("📎 Скиньте ссылку на видео (YouTube, TikTok, Reels).")
    else:
        verified_users.discard(call.from_user.id)
        await call.message.edit_text(
            "Чтобы скачать видео, подпишитесь на спонсоров 👇\n"
            "После подписки нажмите «Проверить подписку».",
            reply_markup=sponsors_menu(),
        )
    await call.answer()


@dp.callback_query(F.data == "check_subs")
async def cb_check(call: CallbackQuery, bot: Bot):
    if await is_subscribed(bot, call.from_user.id):
        verified_users.add(call.from_user.id)
        await call.message.answer("✅ Подписка подтверждена!\n\n📎 Скиньте ссылку на видео (YouTube, TikTok, Reels).")
        await call.answer()
    else:
        await call.answer("❌ Вы подписаны не на все каналы. Подпишитесь и проверьте ещё раз.", show_alert=True)


@dp.message(F.text.regexp(URL_RE))
async def got_link(message: Message, bot: Bot):
    uid = message.from_user.id
    if uid not in verified_users or not await is_subscribed(bot, uid):
        verified_users.discard(uid)
        await message.answer("Сначала подпишитесь на спонсоров 👇", reply_markup=sponsors_menu())
        return
    user_links[uid] = URL_RE.search(message.text).group(0)
    await message.answer("Выберите качество:", reply_markup=quality_menu())


# ---------------------- Скачивание ----------------------
def download_video(url: str, quality: int, out_dir: str) -> str:
    opts = {
        # Предпочитаем h264 + aac: такие видео нормально играют в Telegram
        "format": (
            f"bv*[height<={quality}][vcodec^=avc1]+ba[acodec^=mp4a]/"
            f"bv*[height<={quality}]+ba/"
            f"b[height<={quality}]/b"
        ),
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        "quiet": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)

    if not os.path.exists(path):
        base = os.path.splitext(path)[0]
        for ext in ("mp4", "mkv", "webm"):
            if os.path.exists(f"{base}.{ext}"):
                return f"{base}.{ext}"
    return path


def friendly_error(e: Exception) -> str:
    text = str(e)
    low = text.lower()
    if "ffmpeg" in low or "ffprobe" in low:
        return "Не найден ffmpeg. Установите его и добавьте в PATH."
    if "login" in low or "cookies" in low or "rate-limit" in low or "sign in" in low:
        return "Площадка требует вход в аккаунт. Добавьте файл cookies.txt рядом с ботом."
    if "private" in low:
        return "Видео приватное или недоступно."
    if "javascript" in low or "js runtime" in low or "signature" in low:
        return "YouTube требует обновить yt-dlp и установить Deno."
    return text[:300]


@dp.callback_query(F.data.startswith("q_"))
async def cb_quality(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    url = user_links.get(uid)
    if not url:
        await call.answer("Сначала отправьте ссылку.", show_alert=True)
        return
    if not await is_subscribed(bot, uid):
        verified_users.discard(uid)
        await call.message.answer("Сначала подпишитесь на спонсоров 👇", reply_markup=sponsors_menu())
        await call.answer()
        return

    requested = int(call.data.split("_")[1])
    await call.answer()
    status = await call.message.answer(f"⏳ Скачиваю в {requested}p, подождите...")

    tmp = tempfile.mkdtemp()
    try:
        loop = asyncio.get_running_loop()
        path = None
        used_quality = None

        # Пробуем выбранное качество, а если файл слишком большой — ниже
        for q in [x for x in (1080, 720, 480, 360) if x <= requested]:
            if q != requested:
                await status.edit_text(f"⚠️ Файл слишком большой, пробую {q}p...")
            candidate = await loop.run_in_executor(None, download_video, url, q, tmp)
            if os.path.getsize(candidate) <= MAX_FILE_SIZE:
                path, used_quality = candidate, q
                break
            os.remove(candidate)

        if not path:
            limit_mb = MAX_FILE_SIZE // (1024 * 1024)
            await status.edit_text(
                f"⚠️ Видео не помещается в лимит Telegram ({limit_mb} МБ) даже в 360p."
            )
            return

        await status.edit_text("📤 Отправляю файл...")
        caption = f"✅ Готово! Качество: {used_quality}p"
        if path.endswith(".mp4"):
            await call.message.answer_video(
                FSInputFile(path), caption=caption, supports_streaming=True
            )
        else:
            await call.message.answer_document(FSInputFile(path), caption=caption)
        await status.delete()
    except Exception as e:
        traceback.print_exc()  # полная ошибка в консоль
        await status.edit_text(f"❌ Не удалось скачать:\n{friendly_error(e)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def main():
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬ_СВОЙ_ТОКЕН":
        raise SystemExit("Вставьте токен в BOT_TOKEN в начале файла")

    if LOCAL_API_URL:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(LOCAL_API_URL, is_local=True),
            timeout=3600,
        )
    else:
        session = AiohttpSession(timeout=600)

    bot = Bot(BOT_TOKEN, session=session)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
