from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass
from datetime import timezone
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RpcError
from telethon.tl.types import Channel, Message

from config import AppConfig, can_use_telethon, load_config
from notifier.serverchan_send import ServerChanNotifier
from storage.state import StateStore
from utils.logging import setup_logging

logger = logging.getLogger(__name__)


@dataclass
class ChannelTarget:
    input_target: str
    key: str
    display_name: str
    username: Optional[str]
    entity: Optional[Channel] = None


@dataclass
class PublicMessage:
    message_id: int
    text: str
    media_type: str
    link: str
    date_text: str


def normalize_target(target: str) -> str:
    target = target.strip()
    if target.startswith("http://") or target.startswith("https://"):
        parsed = urlparse(target)
        path = parsed.path.strip("/")
        if not path:
            raise ValueError("Invalid t.me link: {0}".format(target))
        if path.startswith("s/"):
            path = path[2:]
        return "@{0}".format(path.split("/")[0])
    return target if target.startswith("@") else "@{0}".format(target)


def build_link(username: Optional[str], message_id: int) -> str:
    if username:
        return "https://t.me/{0}/{1}".format(username, message_id)
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
        self._stop_event = asyncio.Event()
        self.channel_targets: Dict[str, ChannelTarget] = {}
        self.client: Optional[TelegramClient] = None
        self.use_telethon = can_use_telethon(config.telegram)

        if self.use_telethon:
            self.client = TelegramClient(
                config.telegram.session_path,
                config.telegram.api_id,
                config.telegram.api_hash,
                device_model="telegram-channel-monitor",
            )

    async def start(self) -> None:
        await self._resolve_targets()
        await self._run_backfill()

        if self.use_telethon and self.client is not None:
            await self.client.connect()
            await self._ensure_authorized()
            self._register_handlers()
            asyncio.create_task(self._healthcheck_loop())
            logger.info("Telethon模式启动完成，开始监听", extra={"action": "service_started"})
            await self._stop_event.wait()
        else:
            logger.warning("未配置api_id/api_hash/phone_number，切换公开频道免登录轮询模式", extra={"action": "public_poll_mode"})
            asyncio.create_task(self._healthcheck_loop())
            await self._poll_public_loop()

    async def shutdown(self) -> None:
        logger.info("收到停止信号，准备退出", extra={"action": "shutdown"})
        self._stop_event.set()
        if self.client is not None:
            await self.client.disconnect()

    async def _ensure_authorized(self) -> None:
        if self.client is None:
            return
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
        if self.use_telethon and self.client is not None:
            for raw in self.config.channels.targets:
                normalized = normalize_target(raw)
                entity = await self.client.get_entity(normalized)
                if not isinstance(entity, Channel):
                    raise ValueError("Target is not a channel: {0}".format(raw))

                username = entity.username
                key = username.lower() if username else str(entity.id)
                display_name = "@{0}".format(username) if username else entity.title
                self.channel_targets[key] = ChannelTarget(
                    input_target=raw,
                    key=key,
                    display_name=display_name,
                    username=username,
                    entity=entity,
                )
                logger.info("已解析频道", extra={"action": "resolve_target", "target": raw, "channel": display_name})
            return

        for raw in self.config.channels.targets:
            normalized = normalize_target(raw)
            username = normalized.lstrip("@").lower()
            key = username
            display_name = "@{0}".format(username)
            self.channel_targets[key] = ChannelTarget(
                input_target=raw,
                key=key,
                display_name=display_name,
                username=username,
                entity=None,
            )
            logger.info("已解析公开频道", extra={"action": "resolve_target_public", "target": raw, "channel": display_name})

    async def _run_backfill(self) -> None:
        if self.use_telethon and self.client is not None:
            for target in self.channel_targets.values():
                try:
                    await self._backfill_channel_telethon(target)
                except Exception:
                    logger.exception("补偿拉取失败", extra={"action": "backfill_error", "channel": target.display_name})
        else:
            for target in self.channel_targets.values():
                try:
                    await self._backfill_channel_public(target)
                except Exception:
                    logger.exception("公开频道补偿拉取失败", extra={"action": "backfill_public_error", "channel": target.display_name})

        removed = self.state.cleanup_dedup(self.config.runtime.dedup_window_days)
        logger.info("去重窗口清理完成", extra={"action": "dedup_cleanup", "message_id": removed})

    async def _backfill_channel_telethon(self, target: ChannelTarget) -> None:
        if self.client is None or target.entity is None:
            return
        last_message_id = self.state.get_last_message_id(target.key)
        messages: List[Message] = []

        if last_message_id is not None:
            async for msg in self.client.iter_messages(
                target.entity,
                min_id=last_message_id,
                reverse=True,
                limit=self.config.runtime.backfill_limit,
            ):
                messages.append(msg)
        else:
            async for msg in self.client.iter_messages(target.entity, limit=self.config.runtime.backfill_limit):
                messages.append(msg)
            messages.reverse()

        for msg in messages:
            if msg.id is None:
                continue
            await self._process_message(target, int(msg.id), (msg.message or "").strip(), media_type_of(msg), build_link(target.username, int(msg.id)), msg.date.astimezone(timezone.utc).isoformat() if msg.date else "", "backfill")

    async def _backfill_channel_public(self, target: ChannelTarget) -> None:
        last_message_id = self.state.get_last_message_id(target.key)
        fetched = await asyncio.to_thread(self._fetch_public_messages, target.username, self.config.runtime.backfill_limit)
        messages = sorted(fetched, key=lambda x: x.message_id)
        for item in messages:
            if last_message_id is not None and item.message_id <= last_message_id:
                continue
            await self._process_message(target, item.message_id, item.text, item.media_type, item.link, item.date_text, "backfill_public")

    def _register_handlers(self) -> None:
        if self.client is None:
            return
        chat_entities = [t.entity for t in self.channel_targets.values() if t.entity is not None]

        @self.client.on(events.NewMessage(chats=chat_entities))
        async def handler(event: events.NewMessage.Event) -> None:
            msg = event.message
            if msg.id is None:
                return
            username = None
            chat = await event.get_chat()
            if hasattr(chat, "username"):
                username = getattr(chat, "username")
            key = str(username).lower() if username else str(event.chat_id)
            target = self.channel_targets.get(key)
            if not target and username:
                target = self.channel_targets.get(str(username).lower())
            if not target:
                return
            await self._process_message(
                target,
                int(msg.id),
                (msg.message or "").strip(),
                media_type_of(msg),
                build_link(target.username, int(msg.id)),
                msg.date.astimezone(timezone.utc).isoformat() if msg.date else "",
                "realtime",
            )

    async def _process_message(self, target: ChannelTarget, message_id: int, text: str, media_type: str, link: str, msg_time: str, source: str) -> None:
        if self.state.is_processed(target.key, message_id):
            logger.debug("消息已处理，跳过", extra={"action": "dedup_skip", "channel": target.display_name, "message_id": message_id})
            return

        max_len = self.config.notify.max_text_len
        summary = text[:max_len] + ("..." if len(text) > max_len else "")
        title = "{0}{1}".format(self.config.notify.title_prefix, target.display_name)
        desp = (
            "- 时间: {0}\n"
            "- 频道: {1}\n"
            "- 消息ID: {2}\n"
            "- 媒体类型: {3}\n"
            "- 链接: {4}\n\n"
            "摘要:\n{5}"
        ).format(msg_time, target.display_name, message_id, media_type, link, summary if summary else "(无文本)")

        await self._send_notification(title, desp, target.display_name, message_id)
        self.state.mark_processed(target.key, message_id)
        self.state.update_cursor(target.key, message_id)
        logger.info("消息处理完成", extra={"action": "processed_{0}".format(source), "channel": target.display_name, "message_id": message_id})

    async def _send_notification(self, title: str, desp: str, channel: str, message_id: int) -> None:
        max_retry = 3
        for attempt in range(1, max_retry + 1):
            try:
                ok = await asyncio.to_thread(self.notifier.send, title, desp)
                if ok:
                    return
            except Exception:
                logger.exception("通知发送异常", extra={"action": "notify_exception", "channel": channel, "message_id": message_id})

            wait = 2 ** (attempt - 1)
            logger.warning("通知发送失败，准备重试", extra={"action": "notify_retry", "channel": channel, "message_id": message_id})
            await asyncio.sleep(wait)

    async def _poll_public_loop(self) -> None:
        interval = max(5, self.config.runtime.public_poll_interval_sec)
        while not self._stop_event.is_set():
            for target in self.channel_targets.values():
                try:
                    await self._poll_one_public_target(target)
                except Exception:
                    logger.exception("公开频道轮询失败", extra={"action": "poll_error", "channel": target.display_name})
            await asyncio.sleep(interval)

    async def _poll_one_public_target(self, target: ChannelTarget) -> None:
        last_message_id = self.state.get_last_message_id(target.key)
        fetched = await asyncio.to_thread(self._fetch_public_messages, target.username, self.config.runtime.backfill_limit)
        messages = sorted(fetched, key=lambda x: x.message_id)
        for item in messages:
            if last_message_id is not None and item.message_id <= last_message_id:
                continue
            await self._process_message(target, item.message_id, item.text, item.media_type, item.link, item.date_text, "poll")
            last_message_id = item.message_id

    def _fetch_public_messages(self, username: Optional[str], limit: int) -> List[PublicMessage]:
        if not username:
            return []
        url = "https://t.me/s/{0}".format(username)
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        results: List[PublicMessage] = []
        for block in soup.select("div.tgme_widget_message"):
            data_post = block.get("data-post", "")
            if not data_post or "/" not in data_post:
                continue
            parts = data_post.split("/")
            mid_text = parts[-1]
            if not mid_text.isdigit():
                continue
            message_id = int(mid_text)

            text_elem = block.select_one("div.tgme_widget_message_text")
            text = text_elem.get_text("\n", strip=True) if text_elem else ""

            media_type = "none"
            if block.select_one("a.tgme_widget_message_photo_wrap"):
                media_type = "photo"
            elif block.select_one("video"):
                media_type = "video"
            elif block.select_one("a.tgme_widget_message_document_wrap"):
                media_type = "document"

            date_elem = block.select_one("time")
            date_text = date_elem.get("datetime", "") if date_elem else ""
            link = build_link(username, message_id)
            results.append(PublicMessage(message_id, text, media_type, link, date_text))

        return results[-max(1, limit):]

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
        except (ConnectionError, OSError, RpcError, requests.RequestException):
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
    try:
        install_signal_handlers(loop, service)
    except NotImplementedError:
        logger.warning("当前平台不支持add_signal_handler，已跳过", extra={"action": "signal_handler_skip"})
    await run_with_retry(service)


if __name__ == "__main__":
    asyncio.run(main())
