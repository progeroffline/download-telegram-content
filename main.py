import asyncio
import os
import re
import sys
from pathlib import Path

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
    if not API_ID or not API_HASH:
        logger.error(
            "Не заданы TELEGRAM_API_ID / TELEGRAM_API_HASH.\n"
            "Получи их на https://my.telegram.org/apps и добавь в файл .env "
            "(см. .env.example)."
        )
        sys.exit(1)

    client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
    await client.start()

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
