import logging
import math
import shutil
import subprocess
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Tuple
from urllib.parse import urlparse

import telebot
import yt_dlp
from dotenv import dotenv_values
from yt_dlp.utils import DownloadError


# Безопасный размер для отправки файла через Telegram.
# Оставляем запас относительно лимита 50 МБ.
MAX_AUDIO_BYTES = 49_000_000

# Минимальный допустимый битрейт при сжатии.
MIN_BITRATE_KBPS = 16


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# Загрузка настроек
# ---------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


if not ENV_PATH.is_file():
    raise SystemExit(
        f"Файл .env не найден: {ENV_PATH}\n"
        "Создайте файл .env рядом со скриптом и добавьте:\n"
        "TELEGRAM_BOT_TOKEN=ваш_токен"
    )


config = dotenv_values(ENV_PATH)
TOKEN = (config.get("TELEGRAM_BOT_TOKEN") or "").strip()


if not TOKEN:
    raise SystemExit(
        "В файле .env отсутствует TELEGRAM_BOT_TOKEN "
        "или его значение пустое."
    )


# ---------------------------------------------------------
# Проверка FFmpeg
# ---------------------------------------------------------

missing_programs = [
    program
    for program in ("ffmpeg", "ffprobe")
    if shutil.which(program) is None
]


if missing_programs:
    raise SystemExit(
        f"Не найдены программы: {', '.join(missing_programs)}.\n"
        "Установите FFmpeg и добавьте папку bin в PATH."
    )


# ---------------------------------------------------------
# Инициализация бота
# ---------------------------------------------------------

bot = telebot.TeleBot(
    TOKEN,
    threaded=True,
    num_threads=4,
)

# Одновременно разрешаем только одну загрузку.
download_lock = threading.Lock()


# ---------------------------------------------------------
# Проверка ссылки
# ---------------------------------------------------------

def validate_youtube_url(text: str) -> str:
    """Проверяет, что сообщение содержит одну ссылку YouTube."""

    if not isinstance(text, str):
        raise ValueError("Отправьте ссылку текстовым сообщением.")

    url = text.strip()

    if not url:
        raise ValueError("Ссылка не указана.")

    if any(character.isspace() for character in url):
        raise ValueError(
            "Отправьте только одну ссылку без дополнительного текста."
        )

    parsed = urlparse(url)

    allowed_hosts = {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }

    hostname = (parsed.hostname or "").lower()

    if parsed.scheme not in {"http", "https"}:
        raise ValueError(
            "Ссылка должна начинаться с https:// или http://."
        )

    if hostname not in allowed_hosts:
        raise ValueError("Отправьте корректную ссылку на YouTube.")

    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Некорректная ссылка на YouTube.")

    if not parsed.path or parsed.path == "/":
        raise ValueError("В ссылке не указан идентификатор видео.")

    return url


# ---------------------------------------------------------
# Скачивание и преобразование
# ---------------------------------------------------------

def download_mp3(
    url: str,
    output_dir: Path,
) -> Tuple[Path, str]:
    """Скачивает аудио и преобразует его в MP3."""

    output_dir.mkdir(parents=True, exist_ok=True)

    options = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }

    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=True)

        if not info:
            raise RuntimeError(
                "Не удалось получить информацию о видео."
            )

        if info.get("_type") in {"playlist", "multi_video"}:
            raise ValueError(
                "Отправьте ссылку на отдельное видео, "
                "а не на плейлист."
            )

        original_path = Path(downloader.prepare_filename(info))
        audio_path = original_path.with_suffix(".mp3")

        title = str(info.get("title") or "Аудио").strip()

    if not audio_path.is_file():
        raise RuntimeError(
            "MP3-файл не был создан после обработки FFmpeg."
        )

    if audio_path.stat().st_size == 0:
        raise RuntimeError("Созданный MP3-файл оказался пустым.")

    return audio_path, title


# ---------------------------------------------------------
# Получение длительности
# ---------------------------------------------------------

def get_audio_duration(source: Path) -> float:
    """Возвращает длительность аудиофайла в секундах."""

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            "FFprobe превысил время ожидания."
        ) from error
    except OSError as error:
        raise RuntimeError(
            "Не удалось запустить FFprobe."
        ) from error

    if result.returncode != 0:
        logger.error("Ошибка FFprobe: %s", result.stderr)

        raise RuntimeError(
            "Не удалось определить длительность аудио."
        )

    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "FFprobe вернул некорректную длительность."
        ) from error

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(
            "Получена некорректная длительность аудио."
        )

    return duration


# ---------------------------------------------------------
# Сжатие MP3
# ---------------------------------------------------------

def compress_mp3(
    source: Path,
    max_bytes: int = MAX_AUDIO_BYTES,
) -> Path:
    """Сжимает MP3 до размера, не превышающего max_bytes."""

    if not source.is_file():
        raise RuntimeError("Исходный аудиофайл не найден.")

    if source.stat().st_size <= max_bytes:
        return source

    duration = get_audio_duration(source)

    # Приблизительная формула:
    # размер = длительность * битрейт / 8.
    # Коэффициент 0.95 оставляет запас на метаданные.
    target_kbps = (
        max_bytes * 0.95 * 8 / duration / 1000
    )

    available_bitrates = [
        192,
        160,
        128,
        112,
        96,
        80,
        64,
        56,
        48,
        40,
        32,
        24,
        16,
    ]

    candidates = [
        bitrate
        for bitrate in available_bitrates
        if bitrate <= target_kbps
    ]

    if not candidates:
        raise RuntimeError(
            "Аудио слишком длинное: даже при минимальном "
            f"битрейте {MIN_BITRATE_KBPS} кбит/с файл не "
            "поместится в лимит. Его нужно разделить на части."
        )

    destination = source.with_name(
        f"{source.stem}_compressed.mp3"
    )

    for bitrate in candidates:
        command = [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-map_metadata",
            "-1",
            "-c:a",
            "libmp3lame",
            "-b:a",
            f"{bitrate}k",
        ]

        # При очень низком битрейте переводим звук в моно.
        if bitrate < 64:
            command.extend(
                [
                    "-ac",
                    "1",
                    "-ar",
                    "22050",
                ]
            )

        command.append(str(destination))

        logger.info(
            "Попытка сжатия с битрейтом %s кбит/с",
            bitrate,
        )

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3600,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                "Сжатие аудио превысило время ожидания."
            ) from error
        except OSError as error:
            raise RuntimeError(
                "Не удалось запустить FFmpeg."
            ) from error

        if result.returncode != 0:
            logger.error(
                "Ошибка FFmpeg при битрейте %s: %s",
                bitrate,
                result.stderr,
            )

            raise RuntimeError(
                "FFmpeg не смог сжать аудиофайл."
            )

        if not destination.is_file():
            continue

        compressed_size = destination.stat().st_size

        logger.info(
            "Размер после сжатия: %.2f МБ",
            compressed_size / 1_000_000,
        )

        if 0 < compressed_size <= max_bytes:
            logger.info(
                "Аудио успешно сжато до %.2f МБ, битрейт: %s кбит/с",
                compressed_size / 1_000_000,
                bitrate,
            )

            return destination

    raise RuntimeError(
        "Не удалось уменьшить файл до 49 МБ. "
        "Необходимо разделить аудио на части."
    )


# ---------------------------------------------------------
# Безопасная отправка уведомлений
# ---------------------------------------------------------

def notify(chat_id: int, text: str) -> None:
    """Отправляет уведомление, не прерывая обработчик."""

    try:
        bot.send_message(chat_id, text)
    except Exception:
        logger.exception(
            "Не удалось отправить пользователю уведомление"
        )


# ---------------------------------------------------------
# Команды бота
# ---------------------------------------------------------

@bot.message_handler(commands=["start", "help"])
def start(message) -> None:
    bot.reply_to(
        message,
        "Отправьте ссылку на видео YouTube — "
        "я скачаю аудиодорожку и пришлю MP3.\n\n"
        "Пример:\n"
        "https://www.youtube.com/watch?v=VIDEO_ID\n\n"
        "Отправляйте одну ссылку за раз. "
        "Если файл окажется больше 49 МБ, "
        "бот попробует его сжать.",
    )


# ---------------------------------------------------------
# Обработчик ссылок
# ---------------------------------------------------------

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
            "Сейчас уже обрабатывается другая загрузка. "
            "Попробуйте отправить ссылку немного позже.",
        )
        return

    try:
        bot.reply_to(
            message,
            "Скачиваю аудио и преобразую его в MP3. "
            "Подождите…",
        )

        with TemporaryDirectory(
            prefix="youtube_audio_"
        ) as temp_dir:
            audio_path, title = download_mp3(
                url,
                Path(temp_dir),
            )

            original_size = audio_path.stat().st_size

            logger.info(
                "Загружен файл: %s, размер: %.2f МБ",
                audio_path.name,
                original_size / 1_000_000,
            )

            if original_size > MAX_AUDIO_BYTES:
                bot.send_message(
                    message.chat.id,
                    "Размер аудио превышает 49 МБ. "
                    "Сжимаю файл для отправки…",
                )

                audio_path = compress_mp3(
                    audio_path,
                    MAX_AUDIO_BYTES,
                )

            final_size = audio_path.stat().st_size

            if final_size <= 0:
                raise RuntimeError(
                    "Подготовленный аудиофайл оказался пустым."
                )

            if final_size > MAX_AUDIO_BYTES:
                raise RuntimeError(
                    "Размер аудиофайла всё ещё превышает 49 МБ."
                )

            bot.send_chat_action(
                message.chat.id,
                "upload_audio",
            )

            with audio_path.open("rb") as audio_file:
                bot.send_audio(
                    chat_id=message.chat.id,
                    audio=audio_file,
                    title=title[:64],
                    caption="Аудиодорожка из видео YouTube",
                    timeout=180,
                )

            logger.info(
                "Аудио успешно отправлено в чат %s",
                message.chat.id,
            )

    except DownloadError:
        logger.exception("Ошибка загрузки через yt-dlp")

        notify(
            message.chat.id,
            "Не удалось скачать видео. Оно может быть "
            "недоступно, удалено или требовать авторизацию.",
        )

    except (ValueError, RuntimeError) as error:
        logger.exception("Ошибка обработки аудио")

        notify(
            message.chat.id,
            f"Ошибка: {error}",
        )

    except Exception:
        logger.exception(
            "Непредвиденная ошибка обработки или отправки"
        )

        notify(
            message.chat.id,
            "Произошла непредвиденная ошибка при обработке "
            "или отправке аудио. Подробности записаны "
            "в консоль бота.",
        )

    finally:
        if lock_acquired:
            download_lock.release()


# ---------------------------------------------------------
# Запуск
# ---------------------------------------------------------

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