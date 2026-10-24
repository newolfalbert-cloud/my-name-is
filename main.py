import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional, Tuple
from urllib.parse import urlparse

import telebot
import yt_dlp
from yt_dlp.utils import DownloadError


# Maximum file size allowed by Telegram for an audio attachment.
MAX_AUDIO_BYTES = 49_000_000
TARGET_COMPRESSED_BYTES = 48_000_000
MP3_BITRATES_KBPS = (128, 112, 96, 80, 64, 56, 48, 40, 32, 24, 16, 8)


# Logging configuration.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


# Read a required environment variable and terminate early if it is missing.
def get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(
            f"Не задана обязательная переменная окружения {name}."
        )
    return value


TELEGRAM_BOT_TOKEN = get_required_env("TELEGRAM_BOT_TOKEN")
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES")
# Create the Telegram bot client.
bot = telebot.TeleBot(
    TELEGRAM_BOT_TOKEN,
    threaded=True,
    num_threads=4,
)
download_lock = threading.Lock()


# Validate the input as a single YouTube URL and reject malformed or unsupported links.
def validate_youtube_url(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("Отправьте ссылку текстовым сообщением.")

    url = text.strip()
    if not url:
        raise ValueError("Ссылка не указана.")
    if any(character.isspace() for character in url):
        raise ValueError("Отправьте одну ссылку без дополнительного текста.")

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    allowed_hosts = {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Ссылка должна начинаться с https:// или http://.")
    if hostname not in allowed_hosts:
        raise ValueError("Отправьте корректную ссылку на YouTube.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Некорректная ссылка на YouTube.")
    if not parsed.path or parsed.path == "/":
        raise ValueError("В ссылке не указан идентификатор видео.")

    return url


# Keep the file name safe for Telegram and the filesystem.
def safe_title(value: object) -> str:
    title = str(value or "Аудио").strip()
    title = "".join(
        character for character in title if character.isprintable())
    if len(title) > 64:
        truncated_title = title[:64]
        if not truncated_title[-1].isspace() and not title[64].isspace():
            words = truncated_title.split()
            truncated_title = " ".join(words[:-1]) if len(words) > 1 else ""
        title = truncated_title.strip()
    return title or "Аудио"


# Format a byte count into a compact human-readable size value for logs.
def format_file_size(size_bytes: int) -> str:
    return f"{size_bytes / 1_000_000:.2f} МБ"


# Re-encode oversized audio as MP3 and verify that it fits Telegram's limit.
def compress_audio(audio_path: Path, duration: Optional[int]) -> Path:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        raise RuntimeError(
            "Для сжатия аудио требуется установленный ffmpeg, "
            "добавленный в PATH."
        )

    output_path = audio_path.with_name(f"{audio_path.stem}_compressed.mp3")
    max_bitrate = 128
    if duration is not None and duration > 0:
        max_bitrate = min(
            max_bitrate,
            int(TARGET_COMPRESSED_BYTES * 8 / duration / 1000),
        )
    bitrates = [
        bitrate for bitrate in MP3_BITRATES_KBPS if bitrate <= max_bitrate
    ]
    if not bitrates:
        bitrates = [MP3_BITRATES_KBPS[-1]]

    for bitrate in bitrates:
        result = subprocess.run(
            [
                ffmpeg_path,
                "-y",
                "-i",
                str(audio_path),
                "-vn",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                f"{bitrate}k",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.error("Ошибка ffmpeg: %s", result.stderr[-1000:])
            raise RuntimeError("Не удалось сжать аудиодорожку.")

        compressed_size = output_path.stat().st_size
        logger.info(
            "Сжатие аудио: битрейт %s кбит/с, размер %s",
            bitrate,
            format_file_size(compressed_size),
        )
        if compressed_size <= MAX_AUDIO_BYTES:
            return output_path

    raise RuntimeError(
        "Не удалось сжать аудиодорожку до размера не более 49 МБ."
    )


# Send a message back to the user without crashing the bot if Telegram is unavailable.
def notify(chat_id: int, text: str) -> None:
    try:
        bot.send_message(chat_id, text)
    except Exception:
        logger.exception("Не удалось отправить уведомление")


# Download the lowest-quality audio-only format.
def download_audio(
    url: str,
    output_dir: Path,
    cookie_data: Optional[str] = None,
) -> Tuple[Path, str, Optional[int]]:
    output_dir.mkdir(parents=True, exist_ok=True)

    options = {
        "format": "worst*[vcodec=none]",
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "logger": logger,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "postprocessors": [],
        "extractor_args": {
            "youtube": {
                "player_client": [
                    "default",
                    "web_embedded",
                    "-tv_downgraded",
                ],
            },
        },
    }
    if cookie_data and cookie_data.strip():
        cookie_path = output_dir / "cookies.txt"
        cookie_path.write_text(cookie_data, encoding="utf-8")
        options["cookiefile"] = str(cookie_path)
        logger.debug("Используются cookies из YOUTUBE_COOKIES")

    logger.info("Начинаю загрузку аудиодорожки из видео: %s", url)
    with yt_dlp.YoutubeDL(options) as downloader:
        video_info = downloader.extract_info(url, download=True)

        if not video_info:
            raise RuntimeError("Не удалось скачать аудиодорожку.")
        if video_info.get("_type") in {"playlist", "multi_video"}:
            raise ValueError(
                "Отправьте ссылку на отдельное видео, а не на плейлист.")

        audio_path = Path(downloader.prepare_filename(video_info))

    logger.info(
        "Загружена аудиодорожка из видео: %s, размер %s",
        url,
        format_file_size(audio_path.stat().st_size),
    )

    title = safe_title(video_info.get("title"))
    duration_value = video_info.get("duration")
    duration = (
        max(0, int(duration_value))
        if isinstance(duration_value, (int, float))
        else None
    )

    if not audio_path.is_file():
        raise RuntimeError("Аудиофайл не был загружен.")
    if audio_path.stat().st_size <= 0:
        raise RuntimeError("Загруженный аудиофайл оказался пустым.")

    return audio_path, title, duration


# Show a short help message for the bot and explain oversized audio compression.
@bot.message_handler(commands=["start", "help"])
def start(message) -> None:
    bot.send_message(
        message.chat.id,
        "Отправьте ссылку на видео YouTube — я скачаю готовую "
        "аудиодорожку и пришлю её в чат.\n\n"
        "Если дорожка окажется больше 49 МБ, я попробую сжать её перед "
        "отправкой.\n\n"
        "Отправляйте одну ссылку за раз.\n\n",
    )


# Process text messages, validate the provided YouTube link, and send the audio back.
@bot.message_handler(content_types=["text"])
def handle_link(message) -> None:
    try:
        url = validate_youtube_url(message.text)
    except ValueError as error:
        bot.send_message(message.chat.id, str(error))
        return

    lock_acquired = download_lock.acquire(blocking=False)
    if not lock_acquired:
        bot.send_message(
            message.chat.id,
            "Сейчас уже обрабатывается другая загрузка. Попробуйте позже.",
        )
        return

    try:
        bot.send_message(
            message.chat.id, "Скачиваю аудиодорожку. Подождите…")

        with TemporaryDirectory(prefix="youtube_audio_") as temp_dir:
            temp_path = Path(temp_dir)
            audio_path, title, duration = download_audio(
                url,
                temp_path,
                cookie_data=YOUTUBE_COOKIES,
            )
            original_size = audio_path.stat().st_size

            if original_size > MAX_AUDIO_BYTES:
                notify(
                    message.chat.id,
                    "Аудиодорожка больше 49 МБ. Сжимаю перед отправкой…",
                )
                audio_path = compress_audio(audio_path, duration)

            bot.send_chat_action(message.chat.id, "upload_audio")

            with audio_path.open("rb") as audio_file:
                send_options = {
                    "chat_id": message.chat.id,
                    "audio": audio_file,
                    "title": title,
                    "caption": "Аудиодорожка из видео YouTube",
                    "timeout": 300,
                }
                if duration is not None:
                    send_options["duration"] = duration

                bot.send_audio(**send_options)

            logger.info(
                "Аудиодорожка из видео: %s, отправлена в чат: %s",
                url,
                message.chat.id,
            )

    except DownloadError:
        logger.exception("Ошибка yt-dlp")
        notify(
            message.chat.id,
            "Не удалось скачать аудио. Видео может быть недоступно "
            "или не иметь аудиодорожки.",
        )

    except (ValueError, RuntimeError) as error:
        logger.exception("Ошибка обработки")
        notify(message.chat.id, f"Ошибка: {error}")
    except Exception:
        logger.exception("Непредвиденная ошибка")
        notify(
            message.chat.id,
            "Произошла непредвиденная ошибка.",
        )
    finally:
        if lock_acquired:
            download_lock.release()


def handle_other_messages(message) -> None:
    bot.send_message(
        message.chat.id, "Отправьте текстовую ссылку на видео YouTube.")


# Start the polling loop when the script is executed directly.
if __name__ == "__main__":
    logger.info("Бот запущен")
    try:
        bot.infinity_polling(
            skip_pending=True,
            timeout=20,
            long_polling_timeout=20,
        )
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception:
        logger.exception("Критическая ошибка polling")
        raise
