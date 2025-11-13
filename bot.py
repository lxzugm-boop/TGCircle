import asyncio
import logging
import os
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramServerError,
    TelegramNetworkError,
)

# ================== НАСТРОЙКИ ==================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не задана. Укажи токен бота в Env Vars.")

VIDEO_MAX_DURATION = int(os.getenv("VIDEO_MAX_DURATION", "90"))
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", str(20 * 1024 * 1024)))
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
TMP_DIR = Path(os.getenv("TMP_DIR", "tmp"))

# Порт для HTTP-сервера (Render задаёт PORT автоматически)
PORT = int(os.getenv("PORT", "10000"))

# ================== ЛОГИРОВАНИЕ ==================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("circlebot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ================== УТИЛИТЫ ==================


def build_ffmpeg_cmd(input_path: Path, output_path: Path) -> list[str]:
    """
    ffmpeg:
    - делает квадрат 640x640
    - без чёрных полей: зум + кроп по центру
    - видео H.264, аудио AAC
    """
    return [
        FFMPEG_BIN,
        "-y",
        "-i",
        str(input_path),
        "-vf",
        "scale=640:640:force_original_aspect_ratio=increase,"
        "crop=640:640",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(output_path),
    ]


async def run_ffmpeg(cmd: list[str], timeout: int = 120) -> None:
    logger.info("Running ffmpeg: %s", " ".join(cmd))
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.error("ffmpeg не найден. Проверь, что он установлен и доступен как '%s'.", FFMPEG_BIN)
        raise RuntimeError("ffmpeg not found")

    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        logger.error("ffmpeg превысил таймаут %s секунд и был убит.", timeout)
        raise RuntimeError("ffmpeg timeout")

    if process.returncode != 0:
        logger.error(
            "ffmpeg завершился с кодом %s, stderr: %s",
            process.returncode,
            stderr.decode(errors="ignore"),
        )
        raise RuntimeError("ffmpeg failed")


def human_size(num_bytes: int) -> str:
    mb = num_bytes / 1024 / 1024
    return f"{mb:.1f} МБ"


# ================== ХЕНДЛЕРЫ ==================


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! 👋\n"
        "Я превращаю обычные видео в телеграм-кружочки.\n\n"
        "Просто пришли мне видео (до "
        f"{VIDEO_MAX_DURATION} секунд и ~{human_size(MAX_FILE_SIZE)}), "
        "а я верну его как video note 🟣"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Как пользоваться ботом:\n"
        "1️⃣ Отправь обычное видео (не кружочек).\n"
        f"2️⃣ Длительность — до {VIDEO_MAX_DURATION} секунд.\n"
        f"3️⃣ Размер — до ~{human_size(MAX_FILE_SIZE)}.\n"
        "4️⃣ Я обработаю его и отправлю в виде круглого видео (со звуком!).\n\n"
        "Если что-то не работает — попробуй отправить видео меньшего размера или короче."
    )


@dp.message(Command("health"))
async def cmd_health(message: Message):
    await message.answer("✅ Бот в строю и готов к работе.")


@dp.message(F.text)
async def handle_text(message: Message):
    if message.text.startswith("/"):
        await message.answer("Не знаю такую команду 🤔 Попробуй /start, /help или просто пришли видео.")
    else:
        await message.answer("Пришли мне обычное видео — я сделаю из него кружочек 🟣")


@dp.message(F.video_note)
async def handle_video_note(message: Message):
    await message.answer(
        "Ты отправил уже кружочек 😊\n"
        "Пришли обычное видео, чтобы я сделал кружок из него."
    )


@dp.message(F.video)
async def handle_video(message: Message):
    video = message.video

    logger.info(
        "Got video: duration=%s, file_size=%s, mime_type=%s",
        video.duration,
        video.file_size,
        video.mime_type,
    )

    if video.duration and video.duration > VIDEO_MAX_DURATION:
        await message.answer(
            f"Видео слишком длинное ({video.duration} сек). "
            f"Максимальная длительность — {VIDEO_MAX_DURATION} секунд ⏱️"
        )
        return

    if video.file_size and video.file_size > MAX_FILE_SIZE:
        await message.answer(
            f"Файл слишком большой ({human_size(video.file_size)}). "
            f"Максимум — примерно {human_size(MAX_FILE_SIZE)}."
        )
        return

    status_msg = await message.answer("Принял видео, обрабатываю кружочек... 🔄")

    TMP_DIR.mkdir(exist_ok=True)
    tmp_id = str(uuid.uuid4())
    input_path = TMP_DIR / f"input_{tmp_id}.mp4"
    output_path = TMP_DIR / f"circle_{tmp_id}.mp4"

    try:
        # --- Скачивание ---
        try:
            file = await bot.get_file(video.file_id)
        except TelegramBadRequest as e:
            logger.error("TelegramBadRequest при get_file: %s", e)
            await status_msg.edit_text(
                f"Telegram не дал скачать видео: {e}. Попробуй отправить ещё раз или другое видео."
            )
            return
        except TelegramNetworkError as e:
            logger.error("TelegramNetworkError при get_file: %s", e)
            await status_msg.edit_text(
                "Проблема с сетью при скачивании видео из Telegram. Попробуй ещё раз чуть позже."
            )
            return

        logger.info("Downloading video file_id=%s to %s", video.file_id, input_path)
        await bot.download(file, destination=input_path)

        if not input_path.exists() or input_path.stat().st_size == 0:
            logger.error("Файл после скачивания отсутствует или пустой: %s", input_path)
            await status_msg.edit_text(
                "Не удалось корректно скачать видео из Telegram (файл пустой). Попробуй ещё раз."
            )
            return

        # --- Конвертация ---
        cmd = build_ffmpeg_cmd(input_path, output_path)
        await run_ffmpeg(cmd)

        if not output_path.exists() or output_path.stat().st_size == 0:
            logger.error("Выходной файл после ffmpeg отсутствует или пустой: %s", output_path)
            await status_msg.edit_text(
                "ffmpeg не смог создать корректное видео для кружочка. Попробуй другое видео."
            )
            return

        # --- Отправка кружочка ---
        logger.info("Sending video_note from %s (size=%s bytes)", output_path, output_path.stat().st_size)
        video_note = FSInputFile(output_path)

        try:
            await bot.send_video_note(
                chat_id=message.chat.id,
                video_note=video_note,
                # length не указываем — Telegram сам решит
            )
        except TelegramBadRequest as e:
            logger.error("TelegramBadRequest при send_video_note: %s", e)
            await status_msg.edit_text(
                "Telegram отклонил кружочек: "
                f"{e}\n\n"
                "Это может быть связано с форматом видео. Попробуй другое видео или короче."
            )
            return
        except TelegramServerError as e:
            logger.error("TelegramServerError при send_video_note: %s", e)
            await status_msg.edit_text(
                "Похоже, у Telegram проблемы на своей стороне. Попробуй ещё раз позже 🙏"
            )
            return
        except TelegramNetworkError as e:
            logger.error("TelegramNetworkError при send_video_note: %s", e)
            await status_msg.edit_text(
                "Сетевая ошибка при отправке кружочка. Попробуй ещё раз."
            )
            return

        await status_msg.edit_text("Готово! Вот твой кружочек 🟣")

    except RuntimeError as e:
        logger.error("RuntimeError при обработке видео: %s", e)
        await status_msg.edit_text(
            f"Не получилось обработать видео ({e}). "
            "Если проблема повторяется — напиши разработчику бота."
        )
    except Exception as e:
        logger.exception("Unexpected error while handling video")
        try:
            await status_msg.edit_text(
                f"Что-то пошло не так ({type(e).__name__}): {e}"
            )
        except Exception:
            pass
    finally:
        for path in (input_path, output_path):
            try:
                if path.exists():
                    path.unlink()
                    logger.info("Temp file removed: %s", path)
            except Exception as cleanup_err:
                logger.warning("Не удалось удалить временный файл %s: %s", path, cleanup_err)


# ================== МИНИ-HTTP СЕРВЕР ДЛЯ RENDER ==================


async def handle_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """
    Простейший HTTP-ответ, чтобы Render видел открытый порт и успешный healthcheck.
    """
    try:
        # читаем хотя бы что-то из запроса (но можно и не читать)
        await reader.read(1024)
    except Exception:
        pass

    response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"
    writer.write(response)
    try:
        await writer.drain()
    except Exception:
        pass
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


async def start_http_server():
    server = await asyncio.start_server(handle_http, "0.0.0.0", PORT)
    addr = ", ".join(str(sock.getsockname()) for sock in server.sockets)
    logger.info("HTTP server listening on %s", addr)
    async with server:
        await server.serve_forever()


# ================== ТОЧКА ВХОДА ==================


async def main():
    logger.info(
        "Starting bot polling + HTTP server... VIDEO_MAX_DURATION=%s, MAX_FILE_SIZE=%s, FFMPEG_BIN=%s, PORT=%s",
        VIDEO_MAX_DURATION,
        MAX_FILE_SIZE,
        FFMPEG_BIN,
        PORT,
    )

    bot_task = asyncio.create_task(dp.start_polling(bot))
    http_task = asyncio.create_task(start_http_server())

    await asyncio.gather(bot_task, http_task)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
