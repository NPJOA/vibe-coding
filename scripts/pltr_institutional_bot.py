#!/usr/bin/env python3
"""Twice-daily Palantir (PLTR) 13F institutional ownership digest -> Telegram.

Uses SEC EDGAR's official full-text search plus the actual 13F info table
XML for each filing (no third-party scraping) to find newly filed
13F-HR/13F-HR-A filings that report a Palantir Technologies position, and
classifies each filer's move as a new entrant, increase, or decrease
(options positions are excluded). State is persisted to a JSON file
(committed back to the repo by the workflow) so re-runs don't duplicate
notifications and so future runs know each filer's last known share count.

NOTE: this version detects new/increase/decrease reliably. Detecting a
full EXIT (a filer that previously held PLTR simply drops it from their
next 13F, so the filing never mentions Palantir at all and full-text
search can't find it) needs a separate proactive check per known filer -
that's a planned follow-up, not implemented yet.

Required environment variables:
  TELEGRAM_BOT_TOKEN  - Telegram bot token from BotFather
  TELEGRAM_CHAT_ID    - Target chat id (group or user)
"""
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "pltr_institutions_state.json")
FTS_URL = "https://efts.sec.gov/LATEST/search-index"
SEC_HEADERS = {
    # SEC's fair-access policy asks for a descriptive User-Agent identifying the requester.
    "User-Agent": "vibe-coding PLTR-13F-bot pltr-bot@example.com",
    "Accept-Encoding": "gzip, deflate",
}
LOOKBACK_DAYS = 3  # search window for newly filed 13F-HR filings
REQUEST_DELAY = 0.3  # be polite to SEC's rate limits


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"holders": {}, "notified": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def strip_ns(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def search_recent_13f_filings(start_date, end_date):
    all_hits = []
    offset = 0
    page_size = 10

    while True:
        params = {
            "q": "\"Palantir Technologies\"",
            "forms": "13F-HR,13F-HR/A",
            "startdt": start_date,
            "enddt": end_date,
            "from": offset,
        }
        resp = requests.get(FTS_URL, params=params, headers=SEC_HEADERS, timeout=30)
        print(f"EDGAR full text search: HTTP {resp.status_code} for {params}")
        if resp.status_code != 200:
            print(resp.text[:1000], file=sys.stderr)
        resp.raise_for_status()
        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])
        total = data.get("hits", {}).get("total", {}).get("value", len(hits))
        all_hits.extend(hits)

        offset += page_size
        if offset >= total or not hits:
            break
        time.sleep(REQUEST_DELAY)

    print(f"Full text search returned {len(all_hits)} hit(s)")
    return all_hits


def parse_infotable_for_palantir(xml_bytes):
    root = ET.fromstring(xml_bytes)
    total_shares = 0.0
    found = False

    for elem in root.iter():
        if strip_ns(elem.tag) != "infoTable":
            continue
        name_el = next((c for c in elem.iter() if strip_ns(c.tag) == "nameOfIssuer"), None)
        if name_el is None or "palantir" not in (name_el.text or "").lower():
            continue

        found = True
        put_call_el = next((c for c in elem.iter() if strip_ns(c.tag) == "putCall"), None)
        if put_call_el is not None and (put_call_el.text or "").strip():
            continue  # options leg - excluded from the share total

        shares_el = next((c for c in elem.iter() if strip_ns(c.tag) == "sshPrnamt"), None)
        if shares_el is not None and shares_el.text:
            try:
                total_shares += float(shares_el.text.strip())
            except ValueError:
                pass

    return found, total_shares


def classify(previous_shares, current_shares, previously_known):
    if not previously_known:
        return "신규 진입" if current_shares > 0 else None
    if current_shares > previous_shares:
        return "증가"
    if current_shares < previous_shares:
        return "감소"
    return None  # unchanged


def build_item_message(entry):
    shares_text = f"{int(entry['shares']):,}주"
    return (
        f"[PLTR 기관 보유 변동]\n"
        f"{entry['investor']}: {entry['status']} - {shares_text} [{entry['date']}]\n"
        f"출처: {entry['source_url']}"
    )


def send_telegram_message(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [text]
    for chunk in chunks:
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }, timeout=30)
        resp.raise_for_status()


def main():
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    missing = [n for n, v in [("TELEGRAM_BOT_TOKEN", bot_token), ("TELEGRAM_CHAT_ID", chat_id)] if not v]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        return 1

    state = load_state()
    holders = state.get("holders", {})  # cik -> {"shares": float, "investor": str, "date": str}
    notified = set(state.get("notified", []))

    now = datetime.now(timezone.utc)
    start_date = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    hits = search_recent_13f_filings(start_date, end_date)
    # Process oldest reporting quarter first so that same-day restatements of
    # several past quarters (common with 13F-HR/A amendments) get compared in
    # chronological order instead of whatever order the search API returned.
    hits.sort(key=lambda h: h.get("_source", {}).get("period_ending", ""))
    new_items = []

    for hit in hits:
        source = hit.get("_source", {})
        cik_list = source.get("ciks", [])
        cik = cik_list[0] if cik_list else None
        accession_and_file = hit.get("_id", "")
        if ":" not in accession_and_file:
            print(f"Skipping hit with unexpected _id format: {hit}")
            continue
        accession_dashed, doc_name = accession_and_file.split(":", 1)
        accession_nodash = accession_dashed.replace("-", "")
        file_date = source.get("file_date", "")
        period_ending = source.get("period_ending", "")
        display_names = source.get("display_names", [])
        investor = display_names[0] if display_names else f"CIK {cik}"

        if not cik or not accession_nodash or not doc_name:
            print(f"Skipping hit with missing cik/accession/doc: {hit}")
            continue

        dedupe_key = accession_nodash
        if dedupe_key in notified:
            continue

        previous = holders.get(cik)
        if previous and period_ending and previous.get("period_ending", "") >= period_ending:
            # A restatement of a quarter we already have a newer (or equal)
            # data point for - not a real change, just historical bookkeeping.
            notified.add(dedupe_key)
            continue

        doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_nodash}/{doc_name}"
        time.sleep(REQUEST_DELAY)
        doc_resp = requests.get(doc_url, headers=SEC_HEADERS, timeout=30)
        if doc_resp.status_code != 200:
            print(f"Could not fetch info table {doc_url}: HTTP {doc_resp.status_code}")
            continue

        try:
            found, shares = parse_infotable_for_palantir(doc_resp.content)
        except ET.ParseError as e:
            print(f"Failed to parse info table {doc_url}: {e}")
            continue

        if not found:
            print(f"Palantir not found in info table for CIK {cik} (accession {accession_nodash})")
            continue

        previous_shares = previous["shares"] if previous else 0.0
        status = classify(previous_shares, shares, previous is not None)

        notified.add(dedupe_key)
        holders[cik] = {
            "shares": shares,
            "investor": investor,
            "date": file_date,
            "period_ending": period_ending,
        }

        if status is None:
            continue

        source_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_nodash}/{accession_dashed}-index.htm"
        new_items.append({
            "investor": investor,
            "status": status,
            "shares": shares,
            "date": file_date,
            "source_url": source_url,
        })

    state["notified"] = list(notified)[-3000:]
    state["holders"] = holders
    save_state(state)

    if not new_items:
        print("No new PLTR institutional ownership changes found.")
        return 0

    for entry in new_items:
        send_telegram_message(bot_token, chat_id, build_item_message(entry))
        time.sleep(REQUEST_DELAY)

    print(f"Sent {len(new_items)} individual Telegram message(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
