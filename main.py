import logging
import math
import os
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional, Tuple
from urllib.parse import urlparse

import cloudconvert
import requests
import telebot
import yt_dlp
from yt_dlp.utils import DownloadError


MAX_AUDIO_BYTES = 49_000_000
DOWNLOAD_TIMEOUT = (30, 300)
CLOUDCONVERT_TIMEOUT_SECONDS = 1800

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(
            f"Не задана обязательная переменная окружения {name}."
        )
    return value


TELEGRAM_BOT_TOKEN = get_required_env("TELEGRAM_BOT_TOKEN")
CLOUDCONVERT_API_KEY = get_required_env("CLOUDCONVERT_API_KEY")

cloudconvert.configure(
    api_key=CLOUDCONVERT_API_KEY,
    sandbox=False,
)

bot = telebot.TeleBot(
    TELEGRAM_BOT_TOKEN,
    threaded=True,
    num_threads=4,
)
download_lock = threading.Lock()


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


def safe_title(value: object) -> str:
    title = str(value or "Аудио").strip()
    title = "".join(character for character in title if character.isprintable())
    return title[:64] or "Аудио"


def format_file_size(size_bytes: int) -> str:
    return f"{size_bytes / 1_000_000:.2f} МБ"


def notify(chat_id: int, text: str) -> None:
    try:
        bot.send_message(chat_id, text)
    except Exception:
        logger.exception("Не удалось отправить уведомление")


def download_m4a(
    url: str,
    output_dir: Path,
) -> Tuple[Path, str, Optional[int]]:
    output_dir.mkdir(parents=True, exist_ok=True)

    options = {
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

    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=True)
        if not info:
            raise RuntimeError("Не удалось получить информацию о видео.")
        if info.get("_type") in {"playlist", "multi_video"}:
            raise ValueError("Отправьте ссылку на отдельное видео, а не на плейлист.")

        audio_path = Path(downloader.prepare_filename(info))
        title = safe_title(info.get("title"))
        duration_value = info.get("duration")
        duration = (
            max(0, int(duration_value))
            if isinstance(duration_value, (int, float))
            else None
        )

    if not audio_path.is_file():
        raise RuntimeError("Аудиофайл не был загружен.")
    if audio_path.suffix.lower() != ".m4a":
        raise RuntimeError("YouTube не предоставил готовую дорожку M4A.")
    if audio_path.stat().st_size <= 0:
        raise RuntimeError("Загруженный аудиофайл оказался пустым.")

    return audio_path, title, duration


def calculate_target_bitrate(duration: Optional[int]) -> int:
    if not duration or duration <= 0:
        return 64

    target_kbps = math.floor(
        MAX_AUDIO_BYTES * 0.90 * 8 / duration / 1000
    )

    allowed_bitrates = [128, 112, 96, 80, 64, 56, 48, 40, 32, 24]
    suitable = [value for value in allowed_bitrates if value <= target_kbps]

    if not suitable:
        raise RuntimeError(
            "Запись слишком длинная: даже сжатие до 24 кбит/с "
            "может не уложить её в лимит Telegram."
        )

    return suitable[0]


def find_task(job: dict, name: str) -> dict:
    for task in job.get("tasks", []):
        if task.get("name") == name:
            return task
    raise RuntimeError(f"CloudConvert не вернул задачу {name}.")


def download_cloudconvert_result(url: str, destination: Path) -> None:
    try:
        with requests.get(
            url,
            stream=True,
            timeout=DOWNLOAD_TIMEOUT,
        ) as response:
            response.raise_for_status()

            with destination.open("wb") as output_file:
                downloaded = 0
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > MAX_AUDIO_BYTES:
                        raise RuntimeError(
                            "После сжатия файл всё ещё превышает 49 МБ."
                        )
                    output_file.write(chunk)
    except requests.RequestException as error:
        raise RuntimeError(
            "Не удалось скачать результат из CloudConvert."
        ) from error

    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError("CloudConvert вернул пустой файл.")


def compress_m4a_cloudconvert(
    source: Path,
    output_dir: Path,
    duration: Optional[int],
) -> Tuple[Path, int]:
    if not source.is_file():
        raise RuntimeError("Исходный M4A-файл не найден.")

    bitrate = calculate_target_bitrate(duration)
    channels = 1 if bitrate <= 48 else 2
    frequency = 22050 if bitrate <= 48 else 44100

    logger.info(
        "CloudConvert: сжатие до %s кбит/с, каналов: %s",
        bitrate,
        channels,
    )

    try:
        job = cloudconvert.Job.create(
            payload={
                "tasks": {
                    "upload-audio": {
                        "operation": "import/upload",
                    },
                    "compress-audio": {
                        "operation": "convert",
                        "input": "upload-audio",
                        "input_format": "m4a",
                        "output_format": "m4a",
                        "filename": "compressed.m4a",
                        "audio_codec": "aac",
                        "audio_bitrate": bitrate,
                        "sample_rate": frequency,
                        "channels": channels,
                        "timeout": CLOUDCONVERT_TIMEOUT_SECONDS,
                    },
                    "export-audio": {
                        "operation": "export/url",
                        "input": "compress-audio",
                        "inline": False,
                        "archive_multiple_files": False,
                    },
                }
            }
        )

        upload_task_data = find_task(job, "upload-audio")
        upload_task = cloudconvert.Task.find(id=upload_task_data["id"])
        cloudconvert.Task.upload(
            file_name=str(source),
            task=upload_task,
        )

        finished_job = cloudconvert.Job.wait(id=job["id"])
    except Exception as error:
        logger.exception("Ошибка CloudConvert")
        raise RuntimeError(
            "CloudConvert не смог обработать аудиофайл. "
            "Проверьте API-ключ, квоту и журнал приложения."
        ) from error

    if finished_job.get("status") != "finished":
        failed_messages = [
            task.get("message")
            for task in finished_job.get("tasks", [])
            if task.get("status") == "error" and task.get("message")
        ]
        details = "; ".join(failed_messages) or "неизвестная ошибка"
        raise RuntimeError(f"Ошибка CloudConvert: {details}")

    export_task = find_task(finished_job, "export-audio")
    files = (export_task.get("result") or {}).get("files") or []
    if not files or not files[0].get("url"):
        raise RuntimeError("CloudConvert не вернул ссылку на результат.")

    destination = output_dir / "compressed.m4a"
    download_cloudconvert_result(files[0]["url"], destination)

    if destination.stat().st_size > MAX_AUDIO_BYTES:
        raise RuntimeError("После сжатия файл всё ещё превышает 49 МБ.")

    logger.info(
        "CloudConvert: готово, размер %s",
        format_file_size(destination.stat().st_size),
    )
    return destination, bitrate


@bot.message_handler(commands=["start", "help"])
def start(message) -> None:
    bot.reply_to(
        message,
        "Отправьте ссылку на видео YouTube — я скачаю готовую "
        "аудиодорожку M4A и пришлю её в чат.\n\n"
        "Если файл окажется больше 49 МБ, бот автоматически "
        "сожмёт его через CloudConvert.\n\n"
        "Отправляйте одну ссылку за раз.",
    )


@bot.message_handler(content_types=["text"])
def handle_link(message) -> None:
    try:
        url = validate_youtube_url(message.text)
    except ValueError as error:
        bot.reply_to(message, str(error))
        return

    lock_acquired = download_lock.acquire(blocking=False)
    if not lock_acquired:
        bot.reply_to(
            message,
            "Сейчас уже обрабатывается другая загрузка. Попробуйте позже.",
        )
        return

    try:
        bot.reply_to(message, "Скачиваю аудиодорожку M4A. Подождите…")

        with TemporaryDirectory(prefix="youtube_audio_") as temp_dir:
            temp_path = Path(temp_dir)
            audio_path, title, duration = download_m4a(url, temp_path)
            original_size = audio_path.stat().st_size

            logger.info(
                "Загружен %s, размер %s",
                audio_path.name,
                format_file_size(original_size),
            )

            caption = "Аудиодорожка M4A из видео YouTube"

            if original_size > MAX_AUDIO_BYTES:
                notify(
                    message.chat.id,
                    "Файл превышает 49 МБ. Отправляю его в "
                    "CloudConvert для сжатия…",
                )
                audio_path, bitrate = compress_m4a_cloudconvert(
                    source=audio_path,
                    output_dir=temp_path,
                    duration=duration,
                )
                caption = f"Сжатая аудиодорожка M4A, {bitrate} кбит/с"

            final_size = audio_path.stat().st_size
            if final_size <= 0 or final_size > MAX_AUDIO_BYTES:
                raise RuntimeError("Файл не соответствует лимиту Telegram.")

            bot.send_chat_action(message.chat.id, "upload_audio")

            with audio_path.open("rb") as audio_file:
                send_options = {
                    "chat_id": message.chat.id,
                    "audio": audio_file,
                    "title": title,
                    "caption": caption,
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
            "Не удалось скачать M4A. Видео может быть недоступно, "
            "требовать авторизацию или не иметь подходящей дорожки.",
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


@bot.message_handler(
    content_types=[
        "audio",
        "document",
        "photo",
        "video",
        "voice",
        "sticker",
        "animation",
    ]
)
def handle_other_messages(message) -> None:
    bot.reply_to(message, "Отправьте текстовую ссылку на видео YouTube.")


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
