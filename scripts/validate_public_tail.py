from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from typing import List, Optional

import requests
from bs4 import BeautifulSoup


@dataclass
class PublicMsg:
    message_id: int
    text: str


def _normalize_channel(value: str) -> str:
    value = value.strip()
    value = value.replace("https://t.me/s/", "")
    value = value.replace("https://t.me/", "")
    return value.lstrip("@").strip("/")


def _fetch_list_page_messages(channel: str) -> List[PublicMsg]:
    url = f"https://t.me/s/{channel}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    out: List[PublicMsg] = []
    for block in soup.select("div.tgme_widget_message"):
        data_post = block.get("data-post", "")
        if "/" not in data_post:
            continue
        mid = data_post.split("/")[-1]
        if not mid.isdigit():
            continue
        text_elem = block.select_one("div.tgme_widget_message_text")
        text = text_elem.get_text("\n", strip=True) if text_elem else ""
        out.append(PublicMsg(message_id=int(mid), text=text))

    out.sort(key=lambda x: x.message_id)
    return out


def _fetch_single_message_text(channel: str, message_id: int) -> Optional[str]:
    url = f"https://t.me/{channel}/{message_id}"
    r = requests.get(url, timeout=20)
    if r.status_code >= 400:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    text_elem = soup.select_one("div.tgme_widget_message_text")
    if text_elem is None:
        return ""
    return text_elem.get_text("\n", strip=True)


def _extract_id_from_link(text: str) -> Optional[int]:
    m = re.search(r"https://t\.me/[A-Za-z0-9_]+/(\d+)", text)
    if not m:
        return None
    return int(m.group(1))


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate whether public Telegram tail message is visible and complete")
    parser.add_argument("channel", help="@channel / https://t.me/s/channel / channel")
    parser.add_argument("--sample", type=int, default=5, help="how many tail messages to inspect (default: 5)")
    args = parser.parse_args()

    channel = _normalize_channel(args.channel)
    msgs = _fetch_list_page_messages(channel)
    if not msgs:
        print(json.dumps({"channel": channel, "error": "no messages parsed from list page"}, ensure_ascii=False))
        raise SystemExit(2)

    tail = msgs[-max(1, args.sample) :]
    report = []
    for item in tail:
        detail_text = _fetch_single_message_text(channel, item.message_id)
        report.append(
            {
                "message_id": item.message_id,
                "list_len": len(item.text),
                "detail_len": len(detail_text or "") if detail_text is not None else None,
                "detail_available": detail_text is not None,
                "list_equals_detail": (detail_text == item.text) if detail_text is not None else None,
                "possible_truncated": (detail_text is not None and len(item.text) < len(detail_text)),
            }
        )

    latest_id = tail[-1].message_id
    latest_detail = _fetch_single_message_text(channel, latest_id)

    result = {
        "channel": channel,
        "parsed_count": len(msgs),
        "latest_from_list": latest_id,
        "latest_detail_available": latest_detail is not None,
        "latest_detail_len": len(latest_detail or "") if latest_detail is not None else None,
        "tail_report": report,
        "hint": "If latest_detail_available=true but latest message never arrives in monitor logs, problem is monitor logic. If list/detail both miss latest, source page has delay/cache/rate-limit.",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
