"""Параллельная загрузка файлов через несколько MTProto-соединений.

Обычный ``TelegramClient.download_media`` качает файл через одно
MTProto-соединение и упирается в ~0.3-0.5 МБ/с независимо от реальной
пропускной способности канала. Здесь используется тот же приём, что и в
mautrix-telegram / гисте FastTelethon: файл режется на части, части
качаются параллельно через несколько сендеров к тому же дата-центру и
дописываются в файл по мере получения (в порядке номера части).

Адаптировано из https://gist.github.com/painor/7e74de80ae0c819d3e9abcf9989a8dd6
(в оригинале — https://github.com/tulir/mautrix-telegram), урезано до
скачивания (без загрузки на сервер) под задачи этого проекта.
"""

import asyncio
import inspect
import math
from pathlib import Path
from typing import Awaitable, List, Optional

from loguru import logger
from telethon import TelegramClient, utils
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import (
    ExportAuthorizationRequest,
    ImportAuthorizationRequest,
)
from telethon.tl.functions.upload import GetFileRequest


class _DownloadSender:
    def __init__(self, client, sender, location, offset, limit, stride, count) -> None:
        self.client = client
        self.sender = sender
        self.request = GetFileRequest(location, offset=offset, limit=limit)
        self.stride = stride
        self.remaining = count

    async def next(self) -> Optional[bytes]:
        if not self.remaining:
            return None
        result = await self.client._call(self.sender, self.request)
        self.remaining -= 1
        self.request.offset += self.stride
        return result.bytes

    def disconnect(self) -> Awaitable[None]:
        return self.sender.disconnect()


class _ParallelTransferrer:
    def __init__(self, client: TelegramClient, dc_id: Optional[int] = None) -> None:
        self.client = client
        self.loop = client.loop
        self.dc_id = dc_id or client.session.dc_id
        self.auth_key = (
            None if dc_id and client.session.dc_id != dc_id else client.session.auth_key
        )
        self.senders: Optional[List[_DownloadSender]] = None

    async def _cleanup(self) -> None:
        await asyncio.gather(*[s.disconnect() for s in self.senders])
        self.senders = None

    @staticmethod
    def _get_connection_count(
        file_size: int, max_count: int = 20, full_size: int = 100 * 1024 * 1024
    ) -> int:
        if file_size > full_size:
            return max_count
        return max(1, math.ceil((file_size / full_size) * max_count))

    async def _create_sender(self) -> MTProtoSender:
        dc = await self.client._get_dc(self.dc_id)
        sender = MTProtoSender(self.auth_key, loggers=self.client._log)
        await sender.connect(
            self.client._connection(
                dc.ip_address,
                dc.port,
                dc.id,
                loggers=self.client._log,
                proxy=self.client._proxy,
            )
        )
        if not self.auth_key:
            # Кросс-DC загрузка: экспортируем текущую авторизацию в целевой DC.
            auth = await self.client(ExportAuthorizationRequest(self.dc_id))
            self.client._init_request.query = ImportAuthorizationRequest(
                id=auth.id, bytes=auth.bytes
            )
            req = InvokeWithLayerRequest(LAYER, self.client._init_request)
            await sender.send(req)
            self.auth_key = sender.auth_key
        return sender

    async def _create_download_sender(
        self, location, index, part_size, stride, part_count
    ) -> _DownloadSender:
        return _DownloadSender(
            self.client,
            await self._create_sender(),
            location,
            index * part_size,
            part_size,
            stride,
            part_count,
        )

    async def _init_download(self, connections, location, part_count, part_size) -> None:
        minimum, remainder = divmod(part_count, connections)

        def get_part_count() -> int:
            nonlocal remainder
            if remainder > 0:
                remainder -= 1
                return minimum + 1
            return minimum

        # Первый сендер создаётся отдельно — он экспортирует и импортирует
        # авторизацию для DC, остальные переиспользуют полученный auth_key.
        self.senders = [
            await self._create_download_sender(
                location, 0, part_size, connections * part_size, get_part_count()
            ),
            *await asyncio.gather(
                *[
                    self._create_download_sender(
                        location, i, part_size, connections * part_size, get_part_count()
                    )
                    for i in range(1, connections)
                ]
            ),
        ]

    async def download(self, location, file_size, part_size_kb=None, connection_count=None):
        connection_count = connection_count or self._get_connection_count(file_size)
        part_size = int((part_size_kb or utils.get_appropriated_part_size(file_size)) * 1024)
        part_count = math.ceil(file_size / part_size)
        logger.info(
            f"Параллельная загрузка: DC {self.dc_id}, "
            f"{connection_count} соединений, часть {part_size // 1024} КБ, "
            f"{part_count} частей"
        )
        await self._init_download(connection_count, location, part_count, part_size)

        part = 0
        while part < part_count:
            tasks = [self.loop.create_task(sender.next()) for sender in self.senders]
            for task in tasks:
                data = await task
                if not data:
                    break
                yield data
                part += 1

        await self._cleanup()


async def fast_download_to_file(
    client: TelegramClient, message, out_path: Path, progress_callback=None
) -> str:
    """Скачивает медиа сообщения ``message`` в ``out_path``, используя
    несколько параллельных MTProto-соединений вместо одного."""
    info = utils._get_file_info(message)
    downloader = _ParallelTransferrer(client, info.dc_id)

    with open(out_path, "wb") as out:
        async for chunk in downloader.download(info.location, info.size):
            out.write(chunk)
            if progress_callback:
                result = progress_callback(out.tell(), info.size)
                if inspect.isawaitable(result):
                    await result

    return str(out_path)
