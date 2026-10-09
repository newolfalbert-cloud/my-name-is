import logging
import threading
from pathlib import Path
import os
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

import telebot
import yt_dlp
from yt_dlp.utils import DownloadError


# Оставляем запас относительно лимита Telegram в 50 МБ.
MAX_AUDIO_BYTES = 49_000_000


# ---------------------------------------------------------
# Логирование
# ---------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger(__name__)


TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

if not TOKEN:
    raise SystemExit(
        "Не задана обязательная переменная окружения "
        "TELEGRAM_BOT_TOKEN."
    )


# ---------------------------------------------------------
# Инициализация бота
# ---------------------------------------------------------

bot = telebot.TeleBot(
    TOKEN,
    threaded=True,
    num_threads=4,
)

# Бот будет обрабатывать только одну загрузку одновременно.
download_lock = threading.Lock()


# ---------------------------------------------------------
# Проверка ссылки
# ---------------------------------------------------------

def validate_youtube_url(text: str) -> str:
    """Проверяет, что сообщение содержит одну ссылку YouTube."""

    if not isinstance(text, str):
        raise ValueError(
            "Отправьте ссылку текстовым сообщением."
        )

    url = text.strip()

    if not url:
        raise ValueError("Ссылка не указана.")

    if any(character.isspace() for character in url):
        raise ValueError(
            "Отправьте только одну ссылку "
            "без дополнительного текста."
        )

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
        raise ValueError(
            "Ссылка должна начинаться с https:// или http://."
        )

    if hostname not in allowed_hosts:
        raise ValueError(
            "Отправьте корректную ссылку на YouTube."
        )

    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Некорректная ссылка на YouTube.")

    if not parsed.path or parsed.path == "/":
        raise ValueError(
            "В ссылке не указан идентификатор видео."
        )

    return url


# ---------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------

def safe_title(value: object) -> str:
    """Подготавливает название для отправки в Telegram."""

    title = str(value or "Аудио").strip()

    if not title:
        return "Аудио"

    # Убираем управляющие символы.
    title = "".join(
        character
        for character in title
        if character.isprintable()
    )

    return title[:64] or "Аудио"


def format_file_size(size_bytes: int) -> str:
    """Возвращает размер файла в удобном виде."""

    return f"{size_bytes / 1_000_000:.2f} МБ"


def notify(chat_id: int, text: str) -> None:
    """Отправляет уведомление, не прерывая обработчик."""

    try:
        bot.send_message(chat_id, text)
    except Exception:
        logger.exception(
            "Не удалось отправить пользователю уведомление"
        )


# ---------------------------------------------------------
# Скачивание готового M4A
# ---------------------------------------------------------

def download_m4a(
    url: str,
    output_dir: Path,
) -> tuple[Path, str, int | None]:
    """
    Скачивает готовый поток M4A без конвертации.

    Возвращает:
    - путь к файлу;
    - название;
    - длительность в секундах, если она известна.
    """

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    options = {
        # Загружаем только готовый аудиопоток M4A.
        # Конвертация и объединение потоков не выполняются.
        "format": "bestaudio[ext=m4a][vcodec=none]",

        "outtmpl": str(
            output_dir / "%(id)s.%(ext)s"
        ),

        # Запрещаем скачивание плейлиста.
        "noplaylist": True,

        "quiet": True,
        "no_warnings": True,

        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,

        # Не используем внешние постпроцессоры.
        "postprocessors": [],
    }

    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(
            url,
            download=True,
        )

        if not info:
            raise RuntimeError(
                "Не удалось получить информацию о видео."
            )

        if info.get("_type") in {
            "playlist",
            "multi_video",
        }:
            raise ValueError(
                "Отправьте ссылку на отдельное видео, "
                "а не на плейлист."
            )

        audio_path = Path(
            downloader.prepare_filename(info)
        )

        title = safe_title(info.get("title"))

        duration_value = info.get("duration")

        if isinstance(duration_value, (int, float)):
            duration = max(0, int(duration_value))
        else:
            duration = None

    if not audio_path.is_file():
        raise RuntimeError(
            "Аудиофайл не был загружен."
        )

    if audio_path.suffix.lower() != ".m4a":
        raise RuntimeError(
            "YouTube не предоставил аудиодорожку "
            "в формате M4A."
        )

    if audio_path.stat().st_size <= 0:
        raise RuntimeError(
            "Загруженный аудиофайл оказался пустым."
        )

    return audio_path, title, duration


# ---------------------------------------------------------
# Команды /start и /help
# ---------------------------------------------------------

@bot.message_handler(commands=["start", "help"])
def start(message) -> None:
    bot.reply_to(
        message,
        "Отправьте ссылку на видео YouTube — "
        "я скачаю готовую аудиодорожку M4A "
        "и пришлю её в этот чат.\n\n"
        "Пример:\n"
        "https://www.youtube.com/watch?v=VIDEO_ID\n\n"
        "Отправляйте одну ссылку за раз.\n"
        "Максимальный размер аудиофайла — 49 МБ.",
    )


# ---------------------------------------------------------
# Обработчик текстовых сообщений
# ---------------------------------------------------------

@bot.message_handler(content_types=["text"])
def handle_link(message) -> None:
    try:
        url = validate_youtube_url(message.text)
    except ValueError as error:
        bot.reply_to(
            message,
            str(error),
        )
        return

    lock_acquired = download_lock.acquire(
        blocking=False
    )

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
            "Скачиваю готовую аудиодорожку M4A. "
            "Подождите…",
        )

        with TemporaryDirectory(
            prefix="youtube_audio_"
        ) as temp_dir:
            audio_path, title, duration = download_m4a(
                url=url,
                output_dir=Path(temp_dir),
            )

            file_size = audio_path.stat().st_size

            logger.info(
                "Загружен файл %s, размер: %s",
                audio_path.name,
                format_file_size(file_size),
            )

            if file_size > MAX_AUDIO_BYTES:
                notify(
                    message.chat.id,
                    "Не удалось отправить аудио: размер файла "
                    f"{format_file_size(file_size)}, "
                    "а допустимый размер — 49 МБ.\n\n"
                    "Без FFmpeg бот не может сжать готовый M4A. "
                    "Попробуйте выбрать более короткое видео.",
                )
                return

            bot.send_chat_action(
                chat_id=message.chat.id,
                action="upload_audio",
            )

            with audio_path.open("rb") as audio_file:
                send_options = {
                    "chat_id": message.chat.id,
                    "audio": audio_file,
                    "title": title,
                    "caption": (
                        "Готовая аудиодорожка M4A "
                        "из видео YouTube"
                    ),
                    "timeout": 180,
                }

                # Добавляем длительность только тогда,
                # когда yt-dlp смог её определить.
                if duration is not None:
                    send_options["duration"] = duration

                bot.send_audio(**send_options)

            logger.info(
                "Аудио успешно отправлено в чат %s",
                message.chat.id,
            )

    except DownloadError as error:
        logger.exception(
            "Ошибка загрузки через yt-dlp: %s",
            error,
        )

        notify(
            message.chat.id,
            "Не удалось скачать аудиодорожку M4A.\n\n"
            "Возможные причины:\n"
            "• видео недоступно или удалено;\n"
            "• видео требует авторизацию;\n"
            "• YouTube не предоставил готовый поток M4A;\n"
            "• произошла сетевая ошибка.",
        )

    except (ValueError, RuntimeError) as error:
        logger.exception(
            "Ошибка обработки аудио"
        )

        notify(
            message.chat.id,
            f"Ошибка: {error}",
        )

    except Exception:
        logger.exception(
            "Непредвиденная ошибка обработки "
            "или отправки аудио"
        )

        notify(
            message.chat.id,
            "Произошла непредвиденная ошибка при "
            "обработке или отправке аудио. "
            "Подробности записаны в консоль бота.",
        )

    finally:
        if lock_acquired:
            download_lock.release()


# ---------------------------------------------------------
# Обработчик остальных типов сообщений
# ---------------------------------------------------------

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
    bot.reply_to(
        message,
        "Отправьте текстовую ссылку на видео YouTube.",
    )


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
        logger.info(
            "Бот остановлен пользователем"
        )
    except Exception:
        logger.exception(
            "Критическая ошибка polling"
        )
        raise
