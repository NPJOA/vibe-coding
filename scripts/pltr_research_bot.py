#!/usr/bin/env python3
"""Daily Palantir (PLTR) news research bot -> Telegram.

Uses Claude's server-side web_search tool to gather the last 24 hours of
Palantir-related news, tweets, and LinkedIn posts, then sends a summary to
a Telegram chat.

Required environment variables:
  ANTHROPIC_API_KEY   - Anthropic API key
  TELEGRAM_BOT_TOKEN  - Telegram bot token from BotFather
  TELEGRAM_CHAT_ID    - Target chat id (group or user)
"""
import os
import sys
import textwrap

import anthropic
import requests

MODEL = "claude-sonnet-5"

RESEARCH_PROMPT = textwrap.dedent("""\
    You are a financial news research assistant. Using web search, find the
    most notable Palantir Technologies (PLTR) news, announcements, and
    discussion from roughly the last 24 hours. Cover:
    - Company news / press releases / SEC filings
    - Stock-moving analyst notes or price action
    - Notable posts on Twitter/X or LinkedIn that were indexed by search
      (only include ones you can find a real, working source link for)

    Only include items you found via search with a real source link - do not
    invent items or links. If nothing new happened in the last 24 hours, say
    so plainly instead of padding the summary.

    Write the final summary in Korean, formatted as plain text suitable for
    a Telegram message:
    - Start with a one-line headline summary
    - Then a bulleted list (using "- ") of up to 8 items, each with a short
      description and the source URL on its own line
    - No markdown bold/asterisks, no headers - plain text only, since this
      is sent without markdown parsing
    - Keep it concise and scannable
""")


def research_pltr_news(api_key: str) -> str:
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 8,
        }],
        messages=[{"role": "user", "content": RESEARCH_PROMPT}],
    )

    text_parts = [block.text for block in response.content if block.type == "text"]
    return "\n".join(text_parts).strip()


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    # Telegram messages are capped at 4096 chars; split if needed.
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [text]
    for chunk in chunks:
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }, timeout=30)
        resp.raise_for_status()


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    missing = [name for name, val in [
        ("ANTHROPIC_API_KEY", api_key),
        ("TELEGRAM_BOT_TOKEN", bot_token),
        ("TELEGRAM_CHAT_ID", chat_id),
    ] if not val]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        return 1

    summary = research_pltr_news(api_key)
    if not summary:
        summary = "팔란티어(PLTR) 리서치 결과가 비어있습니다. 스크립트 로그를 확인해주세요."

    header = "[PLTR 24H 리서치]\n\n"
    send_telegram_message(bot_token, chat_id, header + summary)
    print("Sent Telegram message successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
