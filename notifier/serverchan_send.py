from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ServerChanNotifier:
    sendkey: str
    timeout_sec: int = 10
    retry_times: int = 2

    def send(self, title: str, desp: str) -> bool:
        if not self.sendkey or self.sendkey == "YOUR_SENDKEY_HERE":
            logger.error("Server酱 SendKey 未配置，跳过发送", extra={"action": "notify_skip"})
            return False

        url = f"https://sctapi.ftqq.com/{self.sendkey}.send"
        data = {"title": title, "desp": desp}

        for attempt in range(1, self.retry_times + 2):
            try:
                response = requests.post(url, data=data, timeout=self.timeout_sec)
                response.raise_for_status()
                payload = response.json()
                if payload.get("code") == 0:
                    logger.info("Server酱发送成功", extra={"action": "notify_success"})
                    return True
                logger.error(
                    "Server酱返回非成功状态: %s",
                    payload,
                    extra={"action": "notify_failed"},
                )
            except Exception:
                logger.exception(
                    "Server酱发送异常 (attempt=%s)",
                    attempt,
                    extra={"action": "notify_error"},
                )
            if attempt < self.retry_times + 1:
                time.sleep(2 ** (attempt - 1))

        return False
