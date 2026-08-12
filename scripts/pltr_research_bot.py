#!/usr/bin/env python3
"""Daily Palantir (PLTR) news digest -> Telegram.

Pulls the last 24 hours of Palantir-related articles from Google News RSS
(no paid API involved) and sends a formatted digest to a Telegram chat.

Required environment variables:
  TELEGRAM_BOT_TOKEN  - Telegram bot token from BotFather
  TELEGRAM_CHAT_ID    - Target chat id (group or user)
"""
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

LOOKBACK_HOURS = 24
MAX_ITEMS = 10

QUERIES = [
    ("en", "https://news.google.com/rss/search?q=Palantir+OR+PLTR+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("ko", "https://news.google.com/rss/search?q=%ED%8C%94%EB%9E%80%ED%8B%B0%EC%96%B4&hl=ko&gl=KR&ceid=KR:ko"),
]


def fetch_recent_articles():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    seen_links = set()
    articles = []

    for _lang, url in QUERIES:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)

        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date_raw = item.findtext("pubDate")
            if not title or not link or not pub_date_raw:
                continue

            try:
                pub_date = parsedate_to_datetime(pub_date_raw)
            except (TypeError, ValueError):
                continue
            if pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=timezone.utc)

            if pub_date < cutoff:
                continue
            if link in seen_links:
                continue
            seen_links.add(link)

            articles.append((pub_date, title, link))

    articles.sort(key=lambda a: a[0], reverse=True)
    return articles[:MAX_ITEMS]


def build_message(articles) -> str:
    header = "[PLTR 24H 뉴스 다이제스트]\n\n"
    if not articles:
        return header + "지난 24시간 동안 새로 올라온 팔란티어 관련 뉴스가 없습니다."

    lines = [header.strip(), ""]
    for pub_date, title, link in articles:
        kst = pub_date.astimezone(timezone(timedelta(hours=9)))
        lines.append(f"- {title} ({kst.strftime('%m/%d %H:%M')})")
        lines.append(f"  {link}")
    return "\n".join(lines)


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [text]
    for chunk in chunks:
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }, timeout=30)
        resp.raise_for_status()


def main() -> int:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    missing = [name for name, val in [
        ("TELEGRAM_BOT_TOKEN", bot_token),
        ("TELEGRAM_CHAT_ID", chat_id),
    ] if not val]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        return 1

    articles = fetch_recent_articles()
    message = build_message(articles)
    send_telegram_message(bot_token, chat_id, message)
    print(f"Sent Telegram message with {len(articles)} article(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
