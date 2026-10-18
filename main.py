import logging
import os
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
    return title[:64] or "Аудио"


# Format a byte count into a compact human-readable size value for logs.
def format_file_size(size_bytes: int) -> str:
    return f"{size_bytes / 1_000_000:.2f} МБ"


# Send a message back to the user without crashing the bot if Telegram is unavailable.
def notify(chat_id: int, text: str) -> None:
    try:
        bot.send_message(chat_id, text)
    except Exception:
        logger.exception("Не удалось отправить уведомление")


# Try M4A audio formats from highest to lowest bitrate until one fits Telegram.
def download_m4a(
    url: str,
    output_dir: Path,
) -> Tuple[Path, str, Optional[int]]:
    output_dir.mkdir(parents=True, exist_ok=True)

    base_options = {
        "format": "bestaudio[ext=m4a][vcodec=none]",
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "postprocessors": [],
    }

    with yt_dlp.YoutubeDL(base_options) as downloader:
        info = downloader.extract_info(url, download=False)

        if not info:
            raise RuntimeError("Не удалось получить информацию о видео.")
        if info.get("_type") in {"playlist", "multi_video"}:
            raise ValueError(
                "Отправьте ссылку на отдельное видео, а не на плейлист.")

        title = safe_title(info.get("title"))
        duration_value = info.get("duration")
        duration = (
            max(0, int(duration_value))
            if isinstance(duration_value, (int, float))
            else None
        )

        audio_formats = [
            format_info
            for format_info in info.get("formats", [])
            if format_info.get("ext") == "m4a"
            and format_info.get("vcodec") == "none"
            and format_info.get("format_id")
        ]
        audio_formats.sort(
            key=lambda format_info: (
                format_info.get("abr") or 0,
                format_info.get("tbr") or 0,
            ),
            reverse=True,
        )

    if not audio_formats:
        raise RuntimeError("YouTube не предоставил готовую дорожку M4A.")

    logger.info(
        "Доступно %d форматов M4A для видео %s: %s",
        len(audio_formats),
        title,
        ", ".join(format_info["format_id"] for format_info in audio_formats)
    )

    audio_path: Optional[Path] = None
    for format_info in audio_formats:
        options = {**base_options, "format": str(format_info["format_id"])}
        with yt_dlp.YoutubeDL(options) as downloader:
            downloaded_info = downloader.extract_info(url, download=True)

            if not downloaded_info:
                raise RuntimeError("Не удалось скачать аудиодорожку.")
            audio_path = Path(downloader.prepare_filename(downloaded_info))

        if not audio_path.is_file():
            raise RuntimeError("Аудиофайл не был загружен.")
        if audio_path.suffix.lower() != ".m4a":
            raise RuntimeError("YouTube не предоставил готовую дорожку M4A.")

        file_size = audio_path.stat().st_size
        if file_size <= 0:
            raise RuntimeError("Загруженный аудиофайл оказался пустым.")
        if file_size <= MAX_AUDIO_BYTES:
            break

        logger.info(
            "Формат %s превышает лимит (%s); пробую меньший битрейт",
            format_info["format_id"],
            format_file_size(file_size),
        )
        if format_info is not audio_formats[-1]:
            audio_path.unlink()

    if audio_path is None:
        raise RuntimeError("Не удалось скачать аудиодорожку.")
    return audio_path, title, duration


# Show a short help message for the bot and explain the file-size limit.
@bot.message_handler(commands=["start", "help"])
def start(message) -> None:
    bot.send_message(
        message.chat.id,
        "Отправьте ссылку на видео YouTube — я скачаю готовую "
        "аудиодорожку M4A и пришлю её в чат.\n\n"
        "Если дорожка окажется больше 49 МБ, я попробую скачать её "
        "в меньшем битрейте.\n\n"
        "Отправляйте одну ссылку за раз.",
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
            message.chat.id, "Скачиваю аудиодорожку M4A. Подождите…")

        with TemporaryDirectory(prefix="youtube_audio_") as temp_dir:
            temp_path = Path(temp_dir)
            audio_path, title, duration = download_m4a(url, temp_path)
            original_size = audio_path.stat().st_size

            logger.info(
                "Загружен %s, размер %s",
                audio_path.name,
                format_file_size(original_size),
            )

            if original_size > MAX_AUDIO_BYTES:
                notify(
                    message.chat.id,
                    "Видео слишком большое: аудиодорожка превышает 49 МБ. "
                    "Отправьте ссылку на более короткое видео.",
                )
                return

            final_size = audio_path.stat().st_size
            if final_size <= 0:
                raise RuntimeError("Файл не соответствует лимиту Telegram.")

            bot.send_chat_action(message.chat.id, "upload_audio")

            with audio_path.open("rb") as audio_file:
                send_options = {
                    "chat_id": message.chat.id,
                    "audio": audio_file,
                    "title": title,
                    "caption": "Аудиодорожка M4A из видео YouTube",
                    "timeout": 300,
                }
                if duration is not None:
                    send_options["duration"] = duration

                bot.send_audio(**send_options)

            logger.info(
                "Аудио размером %s отправлено в чат %s",
                format_file_size(final_size),
                message.chat.id,
            )

    except DownloadError:
        logger.exception("Ошибка yt-dlp")
        notify(
            message.chat.id,
            "Не удалось скачать M4A. Видео может быть недоступно "
            "или не иметь подходящей дорожки.",
        )

    except (ValueError, RuntimeError) as error:
        logger.exception("Ошибка обработки")
        notify(message.chat.id, f"Ошибка: {error}")
    except Exception:
        logger.exception("Непредвиденная ошибка")
        notify(
            message.chat.id,
            "Произошла непредвиденная ошибка. Подробности записаны в логах.",
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
