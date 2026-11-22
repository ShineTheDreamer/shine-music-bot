"""
Telegram-бот: редактор MP3 (теги, обложка, Slowed / Speed Up / Bass Boost, голосовое).
Универсальная сборка для Docker-хостингов (Koyeb, Render и т.п.).
Стек: aiogram 3.x, mutagen, pydub, Pillow, aiohttp. Системный ffmpeg ставится в Dockerfile.

Токен: переменная окружения BOT_TOKEN.
HTTP-сервер: слушает порт из переменной PORT (по умолчанию 8000) и отвечает "OK"
на любой запрос, чтобы хостинг и пингер видели, что бот активен.
Временные файлы: только в ./temp_audio, удаляются после обработки.
"""

import asyncio
import html
import logging
import os
import re
import shutil
import time
import uuid
from collections import defaultdict
from contextlib import suppress
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiohttp import web
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from mutagen.easyid3 import EasyID3
from mutagen.id3 import APIC, ID3, TIT2, TPE1, ID3NoHeaderError
from mutagen.mp3 import MP3
from PIL import Image
from pydub import AudioSegment
from pydub.effects import normalize

# ───────────────────────────── Настройки ─────────────────────────────

# Токен берётся из переменной окружения BOT_TOKEN (задаётся в панели хостинга).
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", "8000"))   # порт HTTP-сервера (хостинг обычно задаёт PORT сам)

TEMP_ROOT = Path.cwd() / "temp_audio"   # рабочие папки — в текущей директории
MAX_FILE_SIZE = 20 * 1024 * 1024        # лимит скачивания файлов у Bot API (20 МБ)
MP3_BITRATE = "256k"                    # битрейт при пересохранении после эффектов
MAX_TEXT_LEN = 100                      # максимальная длина названия / исполнителя
STALE_AFTER = 3 * 60 * 60               # через сколько секунд бездействия чистим папку

SLOWED_FACTOR = 0.85                    # -15% скорости (и тональности)
SPEEDUP_FACTOR = 1.20                   # +20% скорости (и тональности)

logging.basicConfig(level=logging.INFO)
router = Router()

# Блокировка на пользователя: не даём запустить две тяжёлые обработки одновременно
user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

EFFECT_TITLES = {"slowed": "Slowed", "speedup": "Speed Up", "bass": "Bass Boost"}


# ───────────────────────────── FSM-состояния ─────────────────────────────

class Editing(StatesGroup):
    menu = State()             # главное меню
    waiting_title = State()    # ждём новое название
    waiting_artist = State()   # ждём нового исполнителя
    waiting_cover = State()    # ждём фото для обложки


# ───────────────────────────── Клавиатуры и тексты ─────────────────────────────

def menu_kb() -> InlineKeyboardMarkup:
    """Главное меню с кнопками действий."""
    def btn(text: str, data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=data)

    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("✏️ Название", "title"), btn("👤 Исполнитель", "artist")],
        [btn("🖼 Обложка", "cover")],
        [btn("🐌 Slowed", "slowed"), btn("⚡️ Speed Up", "speedup")],
        [btn("🔊 Bass Boost", "bass"), btn("🎤 В голосовое", "voice")],
        [btn("✅ Готово", "done")],
        [btn("❌ Отмена", "cancel")],
    ])


def back_kb() -> InlineKeyboardMarkup:
    """Кнопка «Назад» на экранах ввода."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ])


def menu_text(data: dict, note: str | None = None) -> str:
    """Текст меню с текущим состоянием трека."""
    effects = " → ".join(data.get("effects", [])) or "нет"
    cover = "есть ✅" if data.get("cover") else "нет"
    text = (
        "🎵 <b>Редактор трека</b>\n\n"
        f"✏️ <b>Название:</b> {html.escape(data['title'])}\n"
        f"👤 <b>Исполнитель:</b> {html.escape(data['artist'])}\n"
        f"🖼 <b>Обложка:</b> {cover}\n"
        f"🎛 <b>Эффекты:</b> {effects}\n\n"
        "Меняй что нужно и жми «✅ Готово», чтобы получить файл."
    )
    if note:
        text += f"\n\n{note}"
    return text


# ───────────────────────────── Работа с файлами (синхронная, через to_thread) ─────────────────────────────

def read_tags(path: Path) -> tuple[str | None, str | None]:
    """Читает название и исполнителя из ID3-тегов."""
    try:
        easy = EasyID3(str(path))
    except Exception:
        return None, None
    title = (easy.get("title") or [None])[0]
    artist = (easy.get("artist") or [None])[0]
    return title, artist


def prepare_cover(src: Path, dst: Path) -> bool:
    """Приводит любую картинку к JPEG (не больше 1000x1000)."""
    try:
        with Image.open(src) as img:
            img = img.convert("RGB")
            img.thumbnail((1000, 1000))
            img.save(dst, "JPEG", quality=90)
        return True
    except Exception:
        logging.exception("Не удалось обработать обложку")
        return False


def extract_cover(mp3_path: Path, workdir: Path) -> bool:
    """Достаёт встроенную обложку из оригинального MP3 (если она есть)."""
    try:
        pics = ID3(str(mp3_path)).getall("APIC")
    except Exception:
        return False
    if not pics:
        return False
    raw = workdir / "cover_orig"
    raw.write_bytes(pics[0].data)
    ok = prepare_cover(raw, workdir / "cover.jpg")
    raw.unlink(missing_ok=True)
    return ok


def change_speed(sound: AudioSegment, factor: float) -> AudioSegment:
    """
    Меняет скорость вместе с тональностью (как проигрыватель винила):
    подменяем frame_rate без пересчёта данных, затем возвращаем исходную
    частоту дискретизации. factor < 1 -> медленнее и ниже, > 1 -> быстрее и выше.
    """
    new_rate = int(sound.frame_rate * factor)
    shifted = sound._spawn(sound.raw_data, overrides={"frame_rate": new_rate})
    return shifted.set_frame_rate(sound.frame_rate)


def bass_boost(sound: AudioSegment) -> AudioSegment:
    """
    Усиливает низы: выделяем низкие частоты фильтром, делаем их громче и
    подмешиваем к слегка приглушённому оригиналу (чтобы не было клиппинга).
    """
    base = sound.apply_gain(-4)
    bass = sound.low_pass_filter(150).low_pass_filter(150).apply_gain(+2)
    return normalize(base.overlay(bass), headroom=0.5)


def apply_effect(path: Path, effect: str) -> None:
    """Применяет эффект к рабочему файлу и перезаписывает его."""
    sound = AudioSegment.from_file(str(path))
    if effect == "slowed":
        sound = change_speed(sound, SLOWED_FACTOR)
    elif effect == "speedup":
        sound = change_speed(sound, SPEEDUP_FACTOR)
    elif effect == "bass":
        sound = bass_boost(sound)

    tmp = path.with_name("processing.mp3")
    try:
        sound.export(str(tmp), format="mp3", bitrate=MP3_BITRATE)
        os.replace(tmp, path)  # атомарная замена рабочего файла
    finally:
        tmp.unlink(missing_ok=True)  # убираем промежуточный файл даже при ошибке


def make_voice(path: Path, out: Path) -> int:
    """Конвертирует трек в OGG/Opus (формат голосовых). Возвращает длительность, сек."""
    sound = AudioSegment.from_file(str(path)).set_channels(1).set_frame_rate(48000)
    sound.export(str(out), format="ogg", codec="libopus", parameters=["-b:a", "64k"])
    return int(len(sound) / 1000)


def build_final(data: dict) -> tuple[Path, Path | None, int]:
    """
    Собирает итоговый MP3: копия рабочего файла + новые теги + обложка.
    Возвращает (путь к mp3, путь к миниатюре или None, длительность в секундах).
    """
    workdir = Path(data["workdir"])
    final = workdir / "final.mp3"
    shutil.copy(workdir / "track.mp3", final)

    # pydub при экспорте теряет теги, поэтому пишем их в самом конце
    try:
        tags = ID3(str(final))
    except ID3NoHeaderError:
        tags = ID3()
    tags.delall("TIT2")
    tags.delall("TPE1")
    tags.delall("APIC")
    tags.add(TIT2(encoding=3, text=data["title"]))
    tags.add(TPE1(encoding=3, text=data["artist"]))

    thumb = None
    cover = data.get("cover")
    if cover and Path(cover).exists():
        tags.add(APIC(
            encoding=3, mime="image/jpeg", type=3, desc="Cover",
            data=Path(cover).read_bytes(),
        ))
        # Миниатюра для плеера Telegram: JPEG, до 320x320 и меньше 200 КБ
        thumb = workdir / "thumb.jpg"
        with Image.open(cover) as img:
            img = img.convert("RGB")
            img.thumbnail((320, 320))
            img.save(thumb, "JPEG", quality=85)

    tags.save(str(final), v2_version=3)  # ID3v2.3 — максимальная совместимость
    duration = int(MP3(str(final)).info.length)
    return final, thumb, duration


# ───────────────────────────── Управление сессией ─────────────────────────────

async def cleanup_session(state: FSMContext) -> None:
    """Удаляет временную папку пользователя и сбрасывает FSM."""
    data = await state.get_data()
    if data.get("workdir"):
        shutil.rmtree(data["workdir"], ignore_errors=True)
    await state.clear()


async def cleanup_loop() -> None:
    """Фоновая уборка: удаляет папки брошенных сессий, чтобы не копились на диске."""
    while True:
        await asyncio.sleep(30 * 60)
        now = time.time()
        for d in TEMP_ROOT.iterdir():
            try:
                track = d / "track.mp3"
                last = track.stat().st_mtime if track.exists() else d.stat().st_mtime
                if now - last > STALE_AFTER:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


async def show_menu(bot: Bot, state: FSMContext, note: str | None = None,
                    keyboard: bool = True) -> None:
    """Обновляет сообщение-меню (редактирует его на месте)."""
    data = await state.get_data()
    try:
        await bot.edit_message_text(
            text=menu_text(data, note),
            chat_id=data["chat_id"],
            message_id=data["menu_msg_id"],
            reply_markup=menu_kb() if keyboard else None,
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def require_session(cb: CallbackQuery, state: FSMContext) -> dict | None:
    """Проверяет, что у пользователя есть активный трек."""
    data = await state.get_data()
    if not data.get("workdir") or not Path(data["workdir"]).exists():
        await cb.answer("Сначала отправь MP3-файл", show_alert=True)
        return None
    return data


# ───────────────────────────── Команды ─────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Привет! Пришли мне MP3-файл, и я помогу его отредактировать:\n"
        "поменять название, исполнителя, обложку, сделать Slowed / Speed Up / "
        "Bass Boost или отправить трек голосовым сообщением."
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await cleanup_session(state)
    await message.answer("Отменено. Пришли новый MP3, когда будешь готов.")


# ───────────────────────────── Получение MP3 ─────────────────────────────

@router.message(
    F.audio | (F.document.mime_type == "audio/mpeg") | F.document.file_name.lower().endswith(".mp3")
)
async def handle_audio(message: Message, state: FSMContext, bot: Bot) -> None:
    media = message.audio or message.document
    name = media.file_name or "track.mp3"

    # Принимаем только MP3
    if not (name.lower().endswith(".mp3") or media.mime_type == "audio/mpeg"):
        await message.answer("Пока я умею работать только с MP3 🙏")
        return
    if media.file_size and media.file_size > MAX_FILE_SIZE:
        await message.answer("Файл слишком большой: бот может скачать до 20 МБ.")
        return

    await cleanup_session(state)  # если был старый трек — убираем его файлы
    status = await message.answer("⏳ Скачиваю трек...")

    workdir = TEMP_ROOT / f"{message.from_user.id}_{uuid.uuid4().hex[:8]}"
    workdir.mkdir(parents=True, exist_ok=True)
    track = workdir / "track.mp3"

    try:
        await bot.download(media, destination=track)
    except Exception:
        logging.exception("Ошибка скачивания")
        shutil.rmtree(workdir, ignore_errors=True)
        await status.edit_text("❌ Не удалось скачать файл, попробуй ещё раз.")
        return

    # Название/исполнитель: из тегов, затем из данных Telegram, затем из имени файла
    tag_title, tag_artist = await asyncio.to_thread(read_tags, track)
    title = tag_title or getattr(media, "title", None) or Path(name).stem
    artist = tag_artist or getattr(media, "performer", None) or "Unknown Artist"

    # Если в файле уже была обложка — сохраняем её, чтобы она не потерялась
    has_cover = await asyncio.to_thread(extract_cover, track, workdir)

    await state.set_state(Editing.menu)
    await state.set_data({
        "workdir": str(workdir),
        "title": title[:MAX_TEXT_LEN],
        "artist": artist[:MAX_TEXT_LEN],
        "cover": str(workdir / "cover.jpg") if has_cover else None,
        "effects": [],
        "chat_id": status.chat.id,
        "menu_msg_id": status.message_id,
    })
    await show_menu(bot, state)


# ───────────────────────────── Название / исполнитель ─────────────────────────────

@router.callback_query(F.data == "title")
async def ask_title(cb: CallbackQuery, state: FSMContext) -> None:
    if not await require_session(cb, state):
        return
    await state.set_state(Editing.waiting_title)
    await cb.message.edit_text("✏️ Отправь новое <b>название</b> трека:", reply_markup=back_kb())
    await cb.answer()


@router.callback_query(F.data == "artist")
async def ask_artist(cb: CallbackQuery, state: FSMContext) -> None:
    if not await require_session(cb, state):
        return
    await state.set_state(Editing.waiting_artist)
    await cb.message.edit_text("👤 Отправь нового <b>исполнителя</b>:", reply_markup=back_kb())
    await cb.answer()


@router.message(StateFilter(Editing.waiting_title, Editing.waiting_artist), F.text)
async def got_text(message: Message, state: FSMContext, bot: Bot) -> None:
    """Принимает текст для названия или исполнителя (зависит от состояния)."""
    field = "title" if await state.get_state() == Editing.waiting_title.state else "artist"
    await state.update_data(**{field: message.text.strip()[:MAX_TEXT_LEN]})
    await state.set_state(Editing.menu)
    await show_menu(bot, state)
    with suppress(TelegramBadRequest):
        await message.delete()  # убираем сообщение пользователя, чтобы чат был чистым


@router.message(StateFilter(Editing.waiting_title, Editing.waiting_artist))
async def got_not_text(message: Message) -> None:
    await message.answer("Нужно отправить обычный текст 🙂")


# ───────────────────────────── Обложка ─────────────────────────────

@router.callback_query(F.data == "cover")
async def ask_cover(cb: CallbackQuery, state: FSMContext) -> None:
    if not await require_session(cb, state):
        return
    await state.set_state(Editing.waiting_cover)
    await cb.message.edit_text("🖼 Отправь <b>фото</b>, которое станет обложкой:",
                               reply_markup=back_kb())
    await cb.answer()


@router.message(Editing.waiting_cover, F.photo | F.document)
async def got_cover(message: Message, state: FSMContext, bot: Bot) -> None:
    # Принимаем и обычное фото, и картинку, отправленную файлом
    if message.photo:
        file = message.photo[-1]  # самое большое разрешение
    else:
        file = message.document
        if not (file.mime_type or "").startswith("image/"):
            await message.answer("Это не картинка. Отправь фото 🖼")
            return

    data = await state.get_data()
    workdir = Path(data["workdir"])
    raw = workdir / "cover_raw"
    cover = workdir / "cover.jpg"

    await bot.download(file, destination=raw)
    ok = await asyncio.to_thread(prepare_cover, raw, cover)
    raw.unlink(missing_ok=True)
    if not ok:
        await message.answer("Не получилось прочитать картинку, попробуй другую.")
        return

    await state.update_data(cover=str(cover))
    await state.set_state(Editing.menu)
    await show_menu(bot, state)
    with suppress(TelegramBadRequest):
        await message.delete()


@router.message(Editing.waiting_cover)
async def got_not_photo(message: Message) -> None:
    await message.answer("Жду фото для обложки 🖼")


@router.callback_query(F.data == "back")
async def go_back(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Возврат из экрана ввода в главное меню."""
    if not await require_session(cb, state):
        return
    await state.set_state(Editing.menu)
    await show_menu(bot, state)
    await cb.answer()


# ───────────────────────────── Эффекты ─────────────────────────────

@router.callback_query(F.data.in_({"slowed", "speedup", "bass"}))
async def on_effect(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await require_session(cb, state)
    if not data:
        return

    lock = user_locks[cb.from_user.id]
    if lock.locked():
        await cb.answer("⏳ Подожди, идёт обработка...")
        return

    effect = cb.data
    async with lock:
        await cb.answer()
        await show_menu(bot, state, note=f"⏳ Применяю «{EFFECT_TITLES[effect]}»...",
                        keyboard=False)
        try:
            # pydub блокирующий, поэтому уводим в отдельный поток
            await asyncio.to_thread(apply_effect, Path(data["workdir"]) / "track.mp3", effect)
        except Exception:
            logging.exception("Ошибка эффекта")
            await show_menu(bot, state, note="❌ Не удалось применить эффект.")
            return

        effects = data.get("effects", []) + [EFFECT_TITLES[effect]]
        await state.update_data(effects=effects)
        await show_menu(bot, state, note=f"✅ «{EFFECT_TITLES[effect]}» применён")


# ───────────────────────────── Голосовое ─────────────────────────────

@router.callback_query(F.data == "voice")
async def on_voice(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await require_session(cb, state)
    if not data:
        return

    lock = user_locks[cb.from_user.id]
    if lock.locked():
        await cb.answer("⏳ Подожди, идёт обработка...")
        return

    async with lock:
        await cb.answer()
        await show_menu(bot, state, note="⏳ Конвертирую в голосовое...", keyboard=False)
        workdir = Path(data["workdir"])
        ogg = workdir / "voice.ogg"
        try:
            duration = await asyncio.to_thread(make_voice, workdir / "track.mp3", ogg)
            await bot.send_voice(data["chat_id"], FSInputFile(ogg), duration=duration)
            note = "🎤 Голосовое отправлено. Можно продолжать редактирование."
        except TelegramBadRequest:
            # Например, у пользователя запрещены голосовые в настройках приватности
            logging.exception("Не удалось отправить голосовое")
            note = "❌ Telegram не принял голосовое (возможно, они запрещены в настройках приватности)."
        except Exception:
            logging.exception("Ошибка конвертации в голосовое")
            note = "❌ Ошибка конвертации в голосовое."
        finally:
            ogg.unlink(missing_ok=True)  # временный OGG удаляем сразу
        await show_menu(bot, state, note=note)


# ───────────────────────────── Готово / Отмена ─────────────────────────────

@router.callback_query(F.data == "done")
async def on_done(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await require_session(cb, state)
    if not data:
        return

    lock = user_locks[cb.from_user.id]
    if lock.locked():
        await cb.answer("⏳ Подожди, идёт обработка...
