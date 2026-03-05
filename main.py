from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass
from datetime import timezone
from typing import Any
from urllib.parse import urlparse

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RpcError
from telethon.tl.types import Channel, Message, User

from config import AppConfig, load_config
from notifier.serverchan_send import ServerChanNotifier
from storage.state import StateStore
from utils.logging import setup_logging

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ChannelTarget:
    input_target: str
    key: str
    display_name: str
    username: str | None
    entity: Channel


def normalize_target(target: str) -> str:
    target = target.strip()
    if target.startswith("http://") or target.startswith("https://"):
        parsed = urlparse(target)
        path = parsed.path.strip("/")
        if not path:
            raise ValueError(f"Invalid t.me link: {target}")
        return f"@{path.split('/')[0]}"
    return target if target.startswith("@") else f"@{target}"


def build_link(username: str | None, message_id: int) -> str:
    if username:
        return f"https://t.me/{username}/{message_id}"
    return "(private or no public username link)"


def media_type_of(msg: Message) -> str:
    if not msg.media:
        return "none"
    if msg.photo:
        return "photo"
    if msg.video:
        return "video"
    if msg.document:
        return "document"
    return "other"


class MonitorService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.state = StateStore(config.runtime.state_db_path)
        self.notifier = ServerChanNotifier(sendkey=config.notify.serverchan_sendkey)
        self.client = TelegramClient(
            config.telegram.session_path,
            config.telegram.api_id,
            config.telegram.api_hash,
            device_model="telegram-channel-monitor",
        )
        self.channel_targets: dict[int, ChannelTarget] = {}
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        await self.client.connect()
        await self._ensure_authorized()
        await self._resolve_targets()
        await self._run_backfill()
        self._register_handlers()

        asyncio.create_task(self._healthcheck_loop())
        logger.info("服务启动完成，开始监听", extra={"action": "service_started"})
        await self._stop_event.wait()

    async def shutdown(self) -> None:
        logger.info("收到停止信号，准备退出", extra={"action": "shutdown"})
        self._stop_event.set()
        await self.client.disconnect()

    async def _ensure_authorized(self) -> None:
        if await self.client.is_user_authorized():
            return

        await self.client.send_code_request(self.config.telegram.phone_number)
        code = input("请输入 Telegram 验证码: ").strip()
        try:
            await self.client.sign_in(self.config.telegram.phone_number, code)
        except RpcError:
            if self.config.telegram.twofa_password:
                await self.client.sign_in(password=self.config.telegram.twofa_password)
            else:
                raise

    async def _resolve_targets(self) -> None:
        for raw in self.config.channels.targets:
            normalized = normalize_target(raw)
            entity = await self.client.get_entity(normalized)
            if not isinstance(entity, Channel):
                raise ValueError(f"Target is not a channel: {raw}")

            username = entity.username
            key = username.lower() if username else str(entity.id)
            display_name = f"@{username}" if username else entity.title
            self.channel_targets[entity.id] = ChannelTarget(
                input_target=raw,
                key=key,
                display_name=display_name,
                username=username,
                entity=entity,
            )
            logger.info(
                "已解析频道",
                extra={"action": "resolve_target", "target": raw, "channel": display_name},
            )

    async def _run_backfill(self) -> None:
        for target in self.channel_targets.values():
            try:
                await self._backfill_channel(target)
            except Exception:
                logger.exception(
                    "补偿拉取失败",
                    extra={"action": "backfill_error", "channel": target.display_name},
                )

        removed = self.state.cleanup_dedup(self.config.runtime.dedup_window_days)
        logger.info(
            "去重窗口清理完成",
            extra={"action": "dedup_cleanup", "message_id": removed},
        )

    async def _backfill_channel(self, target: ChannelTarget) -> None:
        last_message_id = self.state.get_last_message_id(target.key)
        messages: list[Message] = []

        if last_message_id is not None:
            async for msg in self.client.iter_messages(
                target.entity,
                min_id=last_message_id,
                reverse=True,
                limit=self.config.runtime.backfill_limit,
            ):
                messages.append(msg)
        else:
            async for msg in self.client.iter_messages(
                target.entity,
                limit=self.config.runtime.backfill_limit,
            ):
                messages.append(msg)
            messages.reverse()

        for msg in messages:
            if msg.id is None:
                continue
            await self._process_message(target, msg, source="backfill")

    def _register_handlers(self) -> None:
        channel_ids = list(self.channel_targets.keys())

        @self.client.on(events.NewMessage(chats=channel_ids))
        async def handler(event: events.NewMessage.Event) -> None:
            msg = event.message
            if msg.id is None or event.chat_id is None:
                return
            target = self.channel_targets.get(event.chat_id)
            if not target:
                return
            await self._process_message(target, msg, source="realtime")

    async def _process_message(self, target: ChannelTarget, msg: Message, source: str) -> None:
        message_id = int(msg.id)
        if self.state.is_processed(target.key, message_id):
            logger.debug(
                "消息已处理，跳过",
                extra={
                    "action": "dedup_skip",
                    "channel": target.display_name,
                    "message_id": message_id,
                },
            )
            return

        text = (msg.message or "").strip()
        max_len = self.config.notify.max_text_len
        summary = text[:max_len] + ("..." if len(text) > max_len else "")
        msg_time = msg.date.astimezone(timezone.utc).isoformat() if msg.date else ""
        media_type = media_type_of(msg)
        link = build_link(target.username, message_id)

        title = f"{self.config.notify.title_prefix}{target.display_name}"
        desp = (
            f"- 时间(UTC): {msg_time}\n"
            f"- 频道: {target.display_name}\n"
            f"- 消息ID: {message_id}\n"
            f"- 媒体类型: {media_type}\n"
            f"- 链接: {link}\n\n"
            f"摘要:\n{summary if summary else '(无文本)'}"
        )

        await self._send_notification(title, desp, target.display_name, message_id)
        self.state.mark_processed(target.key, message_id)
        self.state.update_cursor(target.key, message_id)

        logger.info(
            "消息处理完成",
            extra={
                "action": f"processed_{source}",
                "channel": target.display_name,
                "message_id": message_id,
            },
        )

    async def _send_notification(
        self, title: str, desp: str, channel: str, message_id: int
    ) -> None:
        max_retry = 3
        for attempt in range(1, max_retry + 1):
            try:
                ok = await asyncio.to_thread(self.notifier.send, title, desp)
                if ok:
                    return
            except Exception:
                logger.exception(
                    "通知发送异常",
                    extra={"action": "notify_exception", "channel": channel, "message_id": message_id},
                )

            wait = 2 ** (attempt - 1)
            logger.warning(
                "通知发送失败，准备重试",
                extra={
                    "action": "notify_retry",
                    "channel": channel,
                    "message_id": message_id,
                },
            )
            await asyncio.sleep(wait)

    async def _healthcheck_loop(self) -> None:
        interval = max(10, self.config.runtime.healthcheck_interval_sec)
        while not self._stop_event.is_set():
            logger.info("服务健康检查", extra={"action": "healthcheck"})
            await asyncio.sleep(interval)


async def run_with_retry(service: MonitorService) -> None:
    delay = 1
    while True:
        try:
            await service.start()
            break
        except FloodWaitError as exc:
            wait = int(exc.seconds)
            logger.warning("触发 FloodWait，等待后重试: %ss", wait, extra={"action": "flood_wait"})
            await asyncio.sleep(wait)
        except (ConnectionError, OSError, RpcError):
            logger.exception("网络/Telegram错误，指数退避重连", extra={"action": "reconnect"})
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


def install_signal_handlers(loop: asyncio.AbstractEventLoop, service: MonitorService) -> None:
    def _handler() -> None:
        loop.create_task(service.shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handler)


async def main() -> None:
    config = load_config("config.yaml")
    setup_logging(config.runtime.log_level)
    service = MonitorService(config)
    loop = asyncio.get_running_loop()
    install_signal_handlers(loop, service)
    await run_with_retry(service)


if __name__ == "__main__":
    asyncio.run(main())
