"""RSS -> Discord anime event notifications. Python 3.11+."""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import feedparser
import requests

FEEDS = {"アニメ！アニメ！": "https://animeanime.jp/rss/index.rdf"}
STATE = Path("sent_articles.json")
MAX_POSTS = 12
HISTORY_LIMIT = 3000
# Require a specific real-world event signal, not generic "開催決定" alone.
PATTERNS = {
    "コラボカフェ": r"コラボカフェ|コラボ喫茶|コラボメニュー|テーマカフェ",
    "ポップアップ": r"ポップアップ(?:ストア|ショップ)?|POP[ -]?UP(?: STORE| SHOP)?|期間限定ショップ",
    "展示会・原画展": r"原画展|展覧会|展示会|企画展|特別展|アニメ展",
    "イベント": r"コラボイベント|ファンミーティング|トークショー|スタンプラリー|上映会|リアルイベント|アニメイベント|コラボ開催|コラボ企画",
}


def load_state():
    if not STATE.exists():
        return None
    data = json.loads(STATE.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ValueError("sent_articles.json must be a JSON string array")
    return data


def save_state(history):
    STATE.write_text(json.dumps(history[-HISTORY_LIMIT:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def valid_link(link):
    parsed = urlsplit(link)
    return parsed.scheme == "https" and parsed.hostname in {"animeanime.jp", "www.animeanime.jp"}


def get_items():
    found = {}
    for source, url in FEEDS.items():
        response = requests.get(url, timeout=25, headers={"User-Agent": "AnimeEventDiscordBot/1.0 (RSS reader)"})
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        if feed.bozo and not feed.entries:
            raise RuntimeError(f"RSS parsing failed: {url}: {feed.bozo_exception}")
        if not feed.entries:
            raise RuntimeError(f"RSS contains no entries: {url}")
        for entry in feed.entries:
            title = re.sub(r"\s+", " ", entry.get("title", "")).strip()
            link = entry.get("link", "").strip()
            if not title or not valid_link(link):
                continue
            category = next((name for name, pattern in PATTERNS.items() if re.search(pattern, title, re.I)), None)
            if category:
                found[link] = {"title": title, "link": link, "source": source, "category": category}
    return list(found.values())


def post(item, webhook):
    payload = {
        "username": "アニメコラボ・イベント速報",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": item["title"][:256],
            "url": item["link"],
            "description": f'🎪 **{item["category"]}** の新着記事',
            "color": 5793266,
            "footer": {"text": f'情報元：{item["source"]}｜記事公開情報（開催日とは異なります）'},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    for attempt in range(5):
        response = requests.post(webhook, json=payload, timeout=25)
        if response.status_code in (200, 204):
            return
        if response.status_code == 429:
            try:
                delay = float(response.json().get("retry_after", 3))
            except (ValueError, TypeError):
                delay = 3
            time.sleep(max(1, min(delay, 60)))
            continue
        response.raise_for_status()
    raise RuntimeError("Discord rate limit retries exhausted")


def main():
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not dry_run and not webhook.startswith("https://discord.com/api/webhooks/"):
        raise ValueError("Set DISCORD_WEBHOOK_URL as a GitHub Actions secret")
    items = get_items()  # Fail closed: never initialize state if RSS fetch fails.
    history = load_state()
    print(f"Matched RSS articles: {len(items)}")
    if history is None:
        # Baseline all currently visible items; don't send historical alerts.
        if not dry_run:
            save_state([x["link"] for x in items])
        print("First run: baseline initialized, no Discord messages sent")
        return
    known = set(history)
    unseen = [x for x in reversed(items) if x["link"] not in known]
    print(f"New matched articles: {len(unseen)}")
    for item in unseen[:MAX_POSTS]:
        if dry_run:
            print(f'[DRY RUN] {item["category"]}: {item["title"]} | {item["link"]}')
        else:
            post(item, webhook)
            history.append(item["link"])
            save_state(history)  # Save only after successful delivery.
            print(f'Sent: {item["title"]}')
            time.sleep(1)
    if len(unseen) > MAX_POSTS:
        print(f"Remaining articles for next run: {len(unseen) - MAX_POSTS}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
