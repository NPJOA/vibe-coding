#!/usr/bin/env python3
"""Twice-daily Palantir (PLTR) 13F institutional ownership digest -> Telegram.

Scrapes fintel.io's public PLTR ownership table for 13F/13F-A filings,
classifies each filer as a new entrant, increase, decrease, or full exit
(options positions are excluded), and sends anything not already reported
to Telegram. A small JSON state file (committed back to the repo by the
workflow) tracks what's already been sent so re-runs don't duplicate.

Required environment variables:
  TELEGRAM_BOT_TOKEN  - Telegram bot token from BotFather
  TELEGRAM_CHAT_ID    - Target chat id (group or user)
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

FINTEL_URL = "https://fintel.io/so/us/pltr"
STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "pltr_institutions_state.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.google.com/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}
LOOKBACK_DAYS = 2  # covers overnight gap between the 8pm and next 8am run


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"notified": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def parse_number(text):
    text = (text or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def fetch_rows():
    resp = requests.get(FINTEL_URL, headers=HEADERS, timeout=30)
    if resp.status_code != 200:
        print(f"fintel.io returned HTTP {resp.status_code}. Body preview:\n{resp.text[:1000]}", file=sys.stderr)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 8:
            continue
        if "table-date" not in (tds[0].get("class") or []):
            continue

        investor_link = tds[2].find("a")
        investor = investor_link.get_text(strip=True) if investor_link else tds[2].get_text(strip=True)

        rows.append({
            "date": tds[0].get_text(strip=True),
            "source": tds[1].get_text(strip=True),
            "investor": investor,
            "type": tds[4].get_text(strip=True),
            "shares": parse_number(tds[6].get_text()),
            "delta_pct_text": tds[7].get_text(strip=True),
        })
    return rows


def classify(row):
    if row["shares"] is None:
        return None
    if row["shares"] == 0:
        return "EXIT"
    if not row["delta_pct_text"]:
        return "신규 진입"
    delta = parse_number(row["delta_pct_text"])
    if delta is None:
        return None
    if delta > 0:
        return "증가"
    if delta < 0:
        return "감소"
    return None  # unchanged refiling - not interesting


def build_message(items):
    lines = ["[PLTR 기관 보유 변동]", ""]
    for row, status in items:
        shares_text = f"{int(row['shares']):,}주"
        delta_text = f" ({row['delta_pct_text']}%)" if row["delta_pct_text"] else ""
        lines.append(f"- {row['investor']}: {status} - {shares_text}{delta_text} [{row['date']}]")
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

    state = load_state()
    notified = set(state.get("notified", []))

    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst)
    target_dates = {(now_kst - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(LOOKBACK_DAYS)}

    rows = fetch_rows()
    print(f"Fetched {len(rows)} total ownership rows from fintel.io")

    new_items = []
    for row in rows:
        if row["date"] not in target_dates:
            continue
        if row["source"] not in ("13F", "13F/A"):
            continue
        if row["type"]:  # non-empty Type cell means Put/Call - an options position
            continue

        key = f"{row['date']}|{row['investor']}|{row['shares']}"
        if key in notified:
            continue

        status = classify(row)
        if status is None:
            continue

        new_items.append((row, status))
        notified.add(key)

    if not new_items:
        print("No new PLTR institutional ownership changes found.")
        return 0

    message = build_message(new_items)
    send_telegram_message(bot_token, chat_id, message)
    print(f"Sent Telegram message with {len(new_items)} item(s).")

    state["notified"] = list(notified)[-2000:]  # cap unbounded growth
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
