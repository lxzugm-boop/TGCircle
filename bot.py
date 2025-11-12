
import asyncio
import logging
import os
import subprocess
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile

# --- Settings ---
BOT_TOKEN = os.environ["BOT_TOKEN"]  # set on Render
VIDEO_MAX_DURATION = int(os.getenv("VIDEO_MAX_DURATION", "90"))  # seconds
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")  # just in case


# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("circlebot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def build_ffmpeg_cmd(input_path: Path, output_path: Path) -> list:
    """
    Build ffmpeg command that:
    - makes video square 720x720
    - keeps aspect ratio with padding
    - encodes to H.264
    """
    return [
        FFMPEG_BIN,
        "-y",  # overwrite
        "-i",
        str(input_path),
        "-vf",
        "scale=720:720:force_original_aspect_ratio=decrease,"
        "pad=720:720:(ow-iw)/2:(oh-ih)/2",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-movflags",
        "+faststart",
        "-an",  # drop audio (кружочки обычно без важного звука)
        str(output_path),
    ]


async def run_ffmpeg(cmd: list) -> None:
    """
    Run ffmpeg asynchronously.
    """
    logger.info("Running ffmpeg: %s", " ".join(cmd))
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        logger.error("ffmpeg failed with code %s, stderr: %s", process.returncode, stderr.decode(errors="ignore"))
        raise RuntimeError("ffmpeg failed")


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! 👋\n"
        "Я превращаю обычные видео в телеграм-кружочки.\n\n"
        "Просто пришли мне видео (до "
        f"{VIDEO_MAX_DURATION} секунд), а я верну его как video note 🟣"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Как пользоваться ботом:\n"
        "1️⃣ Отправь обычное видео (не кружочек).\n"
        f"2️⃣ Длительность — до {VIDEO_MAX_DURATION} секунд.\n"
        "3️⃣ Я обработаю его и отправлю в виде круглого видео (video note).\n\n"
        "Если что-то не работает — попробуй отправить видео меньшего размера или короче."
    )


@dp.message(F.text)
async def handle_text(message: Message):
    # Лёгкий ответ на текст
    if message.text.startswith("/"):
        # неизвестная команда
        await message.answer("Не знаю такую команду 🤔 Попробуй /start или просто пришли видео.")
    else:
        await message.answer("Пришли мне обычное видео — я сделаю из него кружочек 🟣")


@dp.message(F.video)
async def handle_video(message: Message):
    video = message.video

    if video.duration and video.duration > VIDEO_MAX_DURATION:
        await message.answer(
            f"Видео слишком длинное ({video.duration} сек). "
            f"Максимальная длительность — {VIDEO_MAX_DURATION} секунд ⏱️"
        )
        return

    status_msg = await message.answer("Принял видео, обрабатываю кружочек... 🔄")

    tmp_id = str(uuid.uuid4())
    workdir = Path("tmp")
    workdir.mkdir(exist_ok=True)
    input_path = workdir / f"input_{tmp_id}.mp4"
    output_path = workdir / f"circle_{tmp_id}.mp4"

    try:
        # 1. Скачиваем видео
        logger.info("Downloading video file_id=%s to %s", video.file_id, input_path)
        file = await bot.get_file(video.file_id)
        await bot.download(file, destination=input_path)

        # 2. Конвертируем
        cmd = build_ffmpeg_cmd(input_path, output_path)
        await run_ffmpeg(cmd)

        # 3. Отправляем как video_note
        logger.info("Sending video_note from %s", output_path)
        video_note = FSInputFile(output_path)
        await bot.send_video_note(
            chat_id=message.chat.id,
            video_note=video_note,
            length=720,
        )

        await status_msg.edit_text("Готово! Вот твой кружочек 🟣")

    except RuntimeError:
        await status_msg.edit_text(
            "Не получилось сконвертировать видео через ffmpeg 😢\n"
            "Если проблема повторяется — напиши разработчику бота."
        )
    except Exception as e:
        logger.exception("Unexpected error while handling video")
        try:
            await status_msg.edit_text(f"Что-то пошло не так: {e}")
        except Exception:
            # сообщение уже могло быть отредактировано/удалено
            pass
    finally:
        # Чистим временные файлы
        for path in (input_path, output_path):
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                logger.warning("Failed to remove temp file %s", path)


@dp.message(F.video_note)
async def handle_video_note(message: Message):
    await message.answer("Ты отправил уже кружочек 😊\nПришли обычное видео, чтобы я сделал кружок из него.")


@dp.message(Command("health"))
async def cmd_health(message: Message):
    # простейший хелсчек для тебя
    await message.answer("✅ Бот в строю и готов к работе.")


async def main():
    logger.info("Starting bot polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
