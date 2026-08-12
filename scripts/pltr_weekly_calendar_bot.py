#!/usr/bin/env python3
"""Weekly US market calendar digest -> Telegram.

Sent every Monday morning (KST). Combines:
  - This week's notable US earnings releases (Finnhub, free tier)
  - This week's major US economic indicator release dates (FRED, official)
  - This week's FOMC meeting, if any (federalreserve.gov, best effort)
  - This week's US market holidays (computed - no API needed)
  - Next PLTR 13F filing deadline (computed - no API needed)

NOTE: the FOMC section is a best-effort scrape of the Fed's public
calendar page and may need adjustment once we see real output. The
earnings section is Finnhub's full weekly calendar filtered down to
companies that have an analyst EPS estimate on file (a rough proxy for
"notable enough to be covered"), capped at 20 - it won't exactly match a
hand-curated list.

Required environment variables:
  FRED_API_KEY
  FINNHUB_API_KEY
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""
import os
import re
import sys
from datetime import date, datetime, timedelta

import requests

HEADERS = {"User-Agent": "vibe-coding weekly-calendar-bot (github.com/NPJOA/vibe-coding)"}

FRED_KEYWORDS = [
    "Consumer Price Index",
    "Producer Price Index",
    "Employment Situation",
    "Retail Sales",
    "Gross Domestic Product",
    "Personal Income",
    "Industrial Production",
    "Housing Starts",
    "Existing Home Sales",
    "New Residential Sales",
]

WEEKDAYS_KR = ["월", "화", "수", "목", "금", "토", "일"]


def format_date_kr(d):
    return f"{d.month}월 {d.day}일({WEEKDAYS_KR[d.weekday()]})"


def week_range(today):
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


# ---- Holidays (computed - no API) ----

def nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    d += timedelta(days=offset + 7 * (n - 1))
    return d


def last_weekday(year, month, weekday):
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)


def easter_sunday(year):
    # Anonymous Gregorian algorithm.
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def observed(d):
    if d.weekday() == 5:  # Saturday -> observed Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday -> observed Monday
        return d + timedelta(days=1)
    return d


def nyse_holidays(year):
    return {
        observed(date(year, 1, 1)): "신정",
        nth_weekday(year, 1, 0, 3): "마틴 루터 킹 데이",
        nth_weekday(year, 2, 0, 3): "대통령의 날",
        easter_sunday(year) - timedelta(days=2): "성금요일",
        last_weekday(year, 5, 0): "메모리얼 데이",
        observed(date(year, 6, 19)): "준틴스",
        observed(date(year, 7, 4)): "독립기념일",
        nth_weekday(year, 9, 0, 1): "노동절",
        nth_weekday(year, 11, 3, 4): "추수감사절",
        observed(date(year, 12, 25)): "크리스마스",
    }


def this_week_holidays(monday, sunday):
    merged = {}
    for y in {monday.year, sunday.year}:
        merged.update(nyse_holidays(y))
    return {d: name for d, name in merged.items() if monday <= d <= sunday}


def next_13f_deadline(today):
    for y in (today.year, today.year + 1):
        for q_end in (date(y, 3, 31), date(y, 6, 30), date(y, 9, 30), date(y, 12, 31)):
            deadline = q_end + timedelta(days=45)
            if deadline < today:
                continue
            holidays = nyse_holidays(deadline.year)
            while deadline.weekday() >= 5 or deadline in holidays:
                deadline += timedelta(days=1)
                holidays = nyse_holidays(deadline.year)
            return q_end, deadline
    return None, None


# ---- Earnings (Finnhub) ----

def fetch_earnings(api_key, start, end):
    url = "https://finnhub.io/api/v1/calendar/earnings"
    resp = requests.get(url, params={
        "from": start.isoformat(),
        "to": end.isoformat(),
        "token": api_key,
    }, headers=HEADERS, timeout=30)
    print(f"Finnhub earnings calendar: HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:500], file=sys.stderr)
        return []
    data = resp.json().get("earningsCalendar", [])
    notable = [e for e in data if e.get("epsEstimate") is not None]
    notable.sort(key=lambda e: (e.get("date", ""), e.get("symbol", "")))
    print(f"Finnhub returned {len(data)} total, {len(notable)} with analyst estimates")
    return notable[:20]


# ---- Economic indicators (FRED) ----

def fetch_fred_releases(api_key, start, end):
    url = "https://api.stlouisfed.org/fred/releases/dates"
    resp = requests.get(url, params={
        "api_key": api_key,
        "file_type": "json",
        "realtime_start": start.isoformat(),
        "realtime_end": end.isoformat(),
        "include_release_dates_with_no_data": "false",
    }, headers=HEADERS, timeout=30)
    print(f"FRED release dates: HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:500], file=sys.stderr)
        return []
    data = resp.json().get("release_dates", [])
    matched = [
        item for item in data
        if any(kw.lower() in item.get("release_name", "").lower() for kw in FRED_KEYWORDS)
    ]
    matched.sort(key=lambda i: i.get("date", ""))
    print(f"FRED returned {len(data)} total releases, {len(matched)} matched keywords")
    return matched


# ---- FOMC (federalreserve.gov, best effort) ----

def fetch_fomc_dates(monday, sunday):
    months = {
        "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
        "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
    }
    pattern = re.compile(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2})(?:-(\d{1,2}))?"
    )
    try:
        url = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
        resp = requests.get(url, headers=HEADERS, timeout=30)
        print(f"Fed FOMC calendar page: HTTP {resp.status_code}")
        if resp.status_code != 200:
            print(resp.text[:500], file=sys.stderr)
            return []

        found = set()
        for year in {monday.year, sunday.year}:
            for m in pattern.finditer(resp.text):
                month = months[m.group(1)]
                day = int(m.group(3) or m.group(2))
                try:
                    d = date(year, month, day)
                except ValueError:
                    continue
                if monday <= d <= sunday:
                    found.add(d)
        print(f"FOMC parse found {len(found)} date(s) in this week's range")
        return sorted(found)
    except requests.RequestException as e:
        print(f"FOMC fetch failed: {e}", file=sys.stderr)
        return []


def build_message(monday, sunday, earnings, fred_releases, fomc_dates, holidays, q_end, deadline):
    lines = [f"[이번주 미국 시장 일정] {format_date_kr(monday)} ~ {format_date_kr(sunday)}", ""]

    if holidays:
        lines.append("휴장일:")
        for d in sorted(holidays):
            lines.append(f"- {format_date_kr(d)} {holidays[d]}")
        lines.append("")

    if fomc_dates:
        lines.append("FOMC:")
        for d in fomc_dates:
            lines.append(f"- {format_date_kr(d)} FOMC 회의")
        lines.append("")

    if fred_releases:
        lines.append("주요 경제지표 발표:")
        for item in fred_releases:
            d = datetime.strptime(item["date"], "%Y-%m-%d").date()
            lines.append(f"- {format_date_kr(d)} {item['release_name']}")
        lines.append("")

    if earnings:
        lines.append(f"주요 실적 발표 ({len(earnings)}개, 애널리스트 추정치 있는 종목만):")
        hour_label = {"bmo": "장전", "amc": "장후"}
        for e in earnings:
            d = datetime.strptime(e["date"], "%Y-%m-%d").date()
            lines.append(f"- {format_date_kr(d)} {e['symbol']} {hour_label.get(e.get('hour', ''), '')}")
        lines.append("")

    if deadline:
        d_left = (deadline - date.today()).days
        lines.append(f"다음 팔란티어 13F 제출 마감: {format_date_kr(deadline)} ({q_end.year}Q{(q_end.month - 1) // 3 + 1} 분기, D-{d_left})")

    return "\n".join(lines).strip()


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
    fred_key = os.environ.get("FRED_API_KEY")
    finnhub_key = os.environ.get("FINNHUB_API_KEY")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    missing = [n for n, v in [
        ("FRED_API_KEY", fred_key),
        ("FINNHUB_API_KEY", finnhub_key),
        ("TELEGRAM_BOT_TOKEN", bot_token),
        ("TELEGRAM_CHAT_ID", chat_id),
    ] if not v]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        return 1

    today = date.today()
    monday, sunday = week_range(today)

    earnings = fetch_earnings(finnhub_key, monday, sunday)
    fred_releases = fetch_fred_releases(fred_key, monday, sunday)
    fomc_dates = fetch_fomc_dates(monday, sunday)
    holidays = this_week_holidays(monday, sunday)
    q_end, deadline = next_13f_deadline(today)

    message = build_message(monday, sunday, earnings, fred_releases, fomc_dates, holidays, q_end, deadline)
    send_telegram_message(bot_token, chat_id, message)
    print("Sent weekly calendar digest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
