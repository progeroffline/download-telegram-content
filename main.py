import argparse
import asyncio
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from loguru import logger
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from telethon import TelegramClient
from telethon.tl.types import PeerChannel

load_dotenv()

API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "download_telegram_content")
DOWNLOADS_DIR = Path(os.getenv("DOWNLOADS_DIR", "downloads"))
UKRAINE_TIMEZONE = ZoneInfo("Europe/Kyiv")

console = Console()

logger.remove()
logger.add(
    lambda msg: console.print(msg, end="", markup=False, highlight=False),
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    colorize=True,
    level="INFO",
)

# Matches t.me/username/123, t.me/c/1234567890/123, with an optional
# trailing /456 used by comment-thread links.
LINK_RE = re.compile(
    r"(?:https?://)?t\.me/"
    r"(?:c/(?P<channel_id>\d+)|(?P<username>[A-Za-z0-9_]+))"
    r"/(?P<message_id>\d+)"
    r"(?:/(?P<comment_id>\d+))?"
)

CHANNEL_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/"
    r"(?:c/(?P<channel_id>\d+)|(?P<username>[A-Za-z0-9_]+))"
    r"(?:/.*)?$"
)
USERNAME_RE = re.compile(r"[A-Za-z0-9_]+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Скачивает медиа из сообщений Telegram."
    )
    parser.add_argument(
        "--channel",
        metavar="CHANNEL",
        help=(
            "скачать все видео канала от старых к новым; можно передать "
            "username, @username или ссылку t.me"
        ),
    )
    return parser.parse_args()


def parse_link(link: str):
    match = LINK_RE.search(link.strip())
    if not match:
        raise ValueError(f"Не удалось распознать ссылку на сообщение: {link}")

    channel_id = match.group("channel_id")
    username = match.group("username")
    message_id = int(match.group("comment_id") or match.group("message_id"))

    if channel_id:
        return PeerChannel(int(channel_id)), message_id
    return username, message_id


def parse_channel(channel: str):
    value = channel.strip()
    if value.startswith("@"):
        value = value[1:]

    if USERNAME_RE.fullmatch(value):
        return value

    match = CHANNEL_LINK_RE.fullmatch(value.rstrip("/"))
    if not match:
        raise ValueError(f"Не удалось распознать канал: {channel}")

    channel_id = match.group("channel_id")
    if channel_id:
        return PeerChannel(int(channel_id))
    return match.group("username")


async def resolve_entity(client: TelegramClient, entity_ref):
    try:
        return await client.get_entity(entity_ref)
    except ValueError:
        if isinstance(entity_ref, PeerChannel):
            # Private channels only resolve once they're in the local
            # dialog cache, so refresh it and retry once.
            await client.get_dialogs()
            return await client.get_entity(entity_ref)
        raise


def make_progress() -> Progress:
    return Progress(
        TextColumn("[bold]{task.description}", justify="right"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    )


def original_video_name(message) -> str:
    name = message.file.name if message.file else None
    if name:
        # Telegram filenames are external input and must not escape the
        # destination directory if they contain path separators.
        safe_name = Path(name.replace("\\", "/")).name
        if safe_name:
            return safe_name

    extension = message.file.ext if message.file else ""
    return f"message_{message.id}{extension or '.mp4'}"


def video_destination(message) -> Path:
    published_at: datetime = message.date.astimezone(UKRAINE_TIMEZONE)
    return (
        DOWNLOADS_DIR
        / published_at.strftime("%Y")
        / published_at.strftime("%m")
        / published_at.strftime("%d")
        / published_at.strftime("%H%M")
        / original_video_name(message)
    )


def is_video_message(message) -> bool:
    mime_type = getattr(getattr(message, "document", None), "mime_type", "")
    return bool(mime_type and mime_type.startswith("video/"))


async def download_channel_videos(client: TelegramClient, channel: str) -> None:
    entity_ref = parse_channel(channel)
    entity = await resolve_entity(client, entity_ref)

    downloaded = 0
    skipped = 0
    logger.info("Просматриваю канал от старых сообщений к новым...")

    with make_progress() as progress:
        async for message in client.iter_messages(entity, reverse=True):
            if not is_video_message(message):
                continue

            destination = video_destination(message)
            if destination.exists():
                skipped += 1
                logger.info(f"Уже скачано: {destination}")
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)
            partial_destination = destination.with_name(f"{destination.name}.part")
            total = message.file.size if message.file else None
            task_id = progress.add_task(destination.name, total=total)

            def on_progress(current: int, total: int, task_id=task_id) -> None:
                progress.update(task_id, completed=current, total=total)

            try:
                with partial_destination.open("wb") as partial_file:
                    result = await client.download_media(
                        message,
                        file=partial_file,
                        progress_callback=on_progress,
                    )
                if result is not None:
                    partial_destination.replace(destination)
                    downloaded += 1
                    logger.success(f"Скачано: {destination}")
                else:
                    logger.error(f"Не удалось скачать видео из сообщения {message.id}")
            except Exception:
                logger.exception(f"Не удалось скачать видео из сообщения {message.id}")
            finally:
                progress.remove_task(task_id)

    logger.info(f"Готово. Скачано: {downloaded}, уже было на диске: {skipped}.")


async def download_message_media(client: TelegramClient, link: str) -> None:
    entity_ref, message_id = parse_link(link)
    entity = await resolve_entity(client, entity_ref)

    message = await client.get_messages(entity, ids=message_id)
    if message is None:
        logger.warning("Сообщение не найдено (нет доступа или оно удалено).")
        return

    messages = [message]
    if message.grouped_id is not None:
        # Message is part of an album — pull its siblings too.
        around = await client.get_messages(
            entity,
            limit=20,
            min_id=message_id - 10,
            max_id=message_id + 10,
        )
        messages = sorted(
            (m for m in around if m.grouped_id == message.grouped_id),
            key=lambda m: m.id,
        )

    media_messages = [m for m in messages if m.media]
    if not media_messages:
        logger.warning("В этом сообщении нет медиа.")
        return

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    with make_progress() as progress:
        for m in media_messages:
            name = (m.file.name if m.file else None) or f"message_{m.id}"
            total = m.file.size if m.file else None
            task_id = progress.add_task(name, total=total)

            def on_progress(current: int, total: int, task_id=task_id) -> None:
                progress.update(task_id, completed=current, total=total)

            path = await client.download_media(
                m, file=str(DOWNLOADS_DIR) + os.sep, progress_callback=on_progress
            )
            if path:
                logger.success(f"Скачано: {path}")
            else:
                logger.error(f"Не удалось скачать медиа из сообщения {m.id}")


async def main() -> None:
    args = parse_args()

    if not API_ID or not API_HASH:
        logger.error(
            "Не заданы TELEGRAM_API_ID / TELEGRAM_API_HASH.\n"
            "Получи их на https://my.telegram.org/apps и добавь в файл .env "
            "(см. .env.example)."
        )
        sys.exit(1)

    client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
    await client.start()

    if args.channel:
        try:
            await download_channel_videos(client, args.channel)
        except ValueError as exc:
            logger.error(str(exc))
        except Exception:
            logger.exception("Ошибка при скачивании видео канала")
        finally:
            await client.disconnect()
        return

    console.print(
        "[bold cyan]Готово.[/bold cyan] Вставляй ссылку на сообщение (Ctrl+C для выхода)."
    )
    while True:
        try:
            link = console.input("[bold]> [/bold]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break

        if not link:
            continue

        try:
            await download_message_media(client, link)
        except ValueError as exc:
            logger.error(str(exc))
        except Exception:
            logger.exception("Ошибка при обработке ссылки")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
