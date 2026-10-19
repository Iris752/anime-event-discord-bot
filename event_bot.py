"""Anime event news RSS -> Discord. Python 3.11+."""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import feedparser
import requests

STATE = Path('sent_articles.json')
MAX_POSTS = 12
HISTORY_LIMIT = 5000
FEEDS = {
    'アニメ！アニメ！': 'https://animeanime.jp/rss/index.rdf',
    'コミックナタリー': 'https://natalie.mu/comic/feed/news',
}
SOURCE_STATE = Path('active_sources.json')
# The original v1 source is already active. New sources are baselined on their first successful read.
ORIGINAL_SOURCES = {'アニメ！アニメ！'}
MAX_ARTICLE_AGE_HOURS = 72

CATEGORIES = [
    ('☕ コラボカフェ', 0xE6A23C, r'コラボカフェ|コラボ喫茶|コラボメニュー|テーマカフェ|コラボレストラン|カフェコラボ|カフェ(?:が|を|で)?(?:開催|オープン)'),
    ('🛍️ ポップアップ', 0x9B59B6, r'ポップアップ|POP[ -]?UP|期間限定ショップ|コラボショップ|期間限定ストア'),
    ('🖼️ 展示会・原画展', 0x3498DB, r'原画展|展覧会|展示会|企画展|特別展|アニメ展|作品展'),
    ('🎪 イベント', 0x2ECC71, r'コラボイベント|ファンミーティング|トークショー|スタンプラリー|上映会|リアルイベント|アニメイベント|コラボ開催|コラボ企画|先行上映|アニメフェス|コラボフェア'),
]
EXCLUDE = re.compile(r'開催中止|開催延期|払い戻し|返金|お詫び|訂正のお知らせ', re.I)
HEADERS = {'User-Agent': 'AnimeEventDiscordBot/2.1 (public RSS reader)'}


def load_state():
    if not STATE.exists():
        return None
    data = json.loads(STATE.read_text(encoding='utf-8'))
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ValueError('sent_articles.json must be a JSON string array')
    return data


def save_state(history):
    STATE.write_text(json.dumps(history[-HISTORY_LIMIT:], ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def is_safe_link(link):
    p = urlsplit(link)
    return p.scheme == 'https' and bool(p.hostname) and not p.username and not p.password


def published_iso(entry):
    for attr in ('published_parsed', 'updated_parsed'):
        value = entry.get(attr)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc).isoformat()
    return None


def load_sources():
    if not SOURCE_STATE.exists():
        return set(ORIGINAL_SOURCES)
    data = json.loads(SOURCE_STATE.read_text(encoding='utf-8'))
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ValueError('active_sources.json must be a JSON string array')
    return set(data) | ORIGINAL_SOURCES


def save_sources(sources):
    SOURCE_STATE.write_text(json.dumps(sorted(sources), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def collect():
    items = {}
    successful_sources = set()
    now = datetime.now(timezone.utc)
    for source, url in FEEDS.items():
        try:
            response = requests.get(url, timeout=25, headers=HEADERS)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            if not feed.entries:
                raise RuntimeError('RSS contains no entries')
            successful_sources.add(source)
            accepted = 0
            for entry in feed.entries:
                title = re.sub(r'\s+', ' ', entry.get('title', '')).strip()
                link = entry.get('link', '').strip()
                if not title or not is_safe_link(link) or EXCLUDE.search(title):
                    continue
                summary = re.sub(r'<[^>]+>', ' ', entry.get('summary', ''))
                summary = re.sub(r'\s+', ' ', summary).strip()[:600]
                # Prefer explicit event terms in the headline; only use the summary
                # when it also contains an event-action cue.
                match = next(((label, color) for label, color, pattern in CATEGORIES
                              if re.search(pattern, title, re.I)), None)
                if not match and re.search(r'開催|オープン|出店|登場|実施|販売|会場|会期|展開', summary):
                    match = next(((label, color) for label, color, pattern in CATEGORIES
                                  if re.search(pattern, summary, re.I)), None)
                if not match:
                    continue
                published = published_iso(entry)
                if published:
                    published_dt = datetime.fromisoformat(published)
                    if published_dt < now - timedelta(hours=MAX_ARTICLE_AGE_HOURS):
                        continue
                key = link.split('#', 1)[0]
                if key not in items:
                    items[key] = dict(key=key, title=title, link=link, source=source,
                                      category=match[0], color=match[1], published=published)
                    accepted += 1
            print(f'RSS OK: {source}: total={len(feed.entries)} matched={accepted}', flush=True)
        except (requests.RequestException, RuntimeError) as exc:
            print(f'WARNING: RSS unavailable: {source}: {exc}', file=sys.stderr, flush=True)
    if not successful_sources:
        raise RuntimeError('All RSS sources failed; state was not changed')
    return list(items.values()), successful_sources


def send(webhook, item):
    embed = {
        'title': item['title'][:256],
        'url': item['link'],
        'description': f"**{item['category']}**\n[記事を読む]({item['link']})",
        'color': item['color'],
        'fields': [{'name': '📰 情報源', 'value': item['source'], 'inline': True}],
        'footer': {'text': '記事公開情報です。開催日時・会場はリンク先でご確認ください'},
    }
    if item['published']:
        embed['timestamp'] = item['published']
    payload = {'username': 'アニメコラボ・イベント速報', 'allowed_mentions': {'parse': []}, 'embeds': [embed]}
    for _ in range(5):
        response = requests.post(webhook, json=payload, timeout=25)
        if response.status_code in (200, 204):
            return
        if response.status_code == 429:
            try:
                wait = float(response.json().get('retry_after', 3))
            except (ValueError, TypeError):
                wait = 3
            time.sleep(max(1, min(wait, 60)))
            continue
        response.raise_for_status()
    raise RuntimeError('Discord rate limit retries exhausted')


def main():
    dry_run = os.getenv('DRY_RUN', 'false').lower() == 'true'
    webhook = os.getenv('DISCORD_WEBHOOK_URL', '')
    if not dry_run and not webhook.startswith('https://discord.com/api/webhooks/'):
        raise ValueError('Set DISCORD_WEBHOOK_URL in GitHub Actions secrets')
    items, successful_sources = collect()
    history = load_state()
    active_sources = load_sources()
    print(f'Matched RSS articles: {len(items)}', flush=True)
    if history is None:
        if not dry_run:
            save_state([x['key'] for x in items])
            save_sources(active_sources | successful_sources)
        print('First run: baseline initialized; no Discord messages sent', flush=True)
        return
    if os.getenv('BASELINE_NOW', 'false').lower() == 'true':
        if not dry_run:
            save_state(history + [x['key'] for x in items if x['key'] not in set(history)])
            save_sources(active_sources | successful_sources)
        print('Migration baseline updated; no Discord messages sent', flush=True)
        return
    # A newly added source is automatically baselined when it first works.
    # This also protects against sources recovering from an earlier outage.
    new_sources = successful_sources - active_sources
    baseline_keys = [x['key'] for x in items if x['source'] in new_sources]
    if new_sources:
        print(f'New RSS source baseline (no posts): {sorted(new_sources)} / {len(baseline_keys)} articles', flush=True)
        if not dry_run:
            history.extend(k for k in baseline_keys if k not in set(history))
            save_state(history)
            save_sources(active_sources | new_sources)
    known = set(history) | set(baseline_keys)
    unseen = [x for x in reversed(items) if x['key'] not in known]
    print(f'New matched articles: {len(unseen)}', flush=True)
    for item in unseen[:MAX_POSTS]:
        if dry_run:
            print(f"[DRY RUN] {item['category']}: {item['title']}", flush=True)
        else:
            send(webhook, item)
            history.append(item['key'])
            save_state(history)
            print(f"Sent: {item['title']}", flush=True)
            time.sleep(1)
    if len(unseen) > MAX_POSTS:
        print(f'Pending for next run: {len(unseen) - MAX_POSTS}', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
