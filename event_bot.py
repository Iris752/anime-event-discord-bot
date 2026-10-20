"""Anime event news RSS -> Discord. Python 3.11+. v2.3"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import feedparser
import requests
from bs4 import BeautifulSoup

STATE = Path('sent_articles.json')
SOURCE_STATE = Path('active_sources.json')
MAX_POSTS = 12
HISTORY_LIMIT = 5000
FEEDS = {
    'アニメ！アニメ！': 'https://animeanime.jp/rss/index.rdf',
    'コミックナタリー': 'https://natalie.mu/comic/feed/news',
}
ORIGINAL_SOURCES = {'アニメ！アニメ！'}
MAX_ARTICLE_AGE_HOURS = 72
EVENT_END_GRACE_DAYS = 14
JST = timezone(timedelta(hours=9))
HEADERS = {'User-Agent': 'AnimeEventDiscordBot/2.3 (public RSS reader)'}
ARTICLE_HOSTS = {'animeanime.jp', 'www.animeanime.jp', 'natalie.mu'}

CATEGORIES = [
    ('☕ コラボカフェ', 0xE6A23C, r'コラボカフェ|コラボ喫茶|コラボメニュー|テーマカフェ|コラボレストラン|カフェコラボ|カフェ(?:が|を|で)?(?:開催|オープン)'),
    ('🛍️ ポップアップ', 0x9B59B6, r'ポップアップ|POP[ -]?UP|期間限定ショップ|コラボショップ|期間限定ストア'),
    ('🖼️ 展示会・原画展', 0x3498DB, r'原画展|展覧会|展示会|企画展|特別展|アニメ展|作品展'),
    ('🎪 イベント', 0x2ECC71, r'コラボイベント|ファンミーティング|トークショー|スタンプラリー|上映会|リアルイベント|アニメイベント|コラボ開催|コラボ企画|先行上映|アニメフェス|コラボフェア'),
]
EXCLUDE = re.compile(r'開催中止|開催延期|払い戻し|返金|お詫び|訂正のお知らせ', re.I)
# A headline must contain an event category, OR a summary must have both a
# category and an event-action cue. This avoids posting unrelated anime news.
EVENT_ACTION = re.compile(r'開催|オープン|出店|登場|実施|販売|会場|会期|展開|実施決定|開催決定')

DATE = r'(?P<year>20\d{2})年\s*(?P<month>1[0-2]|0?[1-9])月\s*(?P<day>3[01]|[12]\d|0?[1-9])日'
RANGE = re.compile(
    r'(?:開催期間|会期|実施期間|販売期間|開催日程|開催日時)[：:\s]{0,12}'
    + DATE + r'.{0,55}?(?:～|〜|~|から|－|–|―|\-|より).{0,12}?(?:(?P<endyear>20\d{2})年\s*)?'
    + r'(?P<endmonth>1[0-2]|0?[1-9])月\s*(?P<endday>3[01]|[12]\d|0?[1-9])日', re.I)
ENDS = re.compile(r'(?:終了日|最終日|会期終了|開催終了)[：:\s]*' + DATE, re.I)
END_WITH_UNTIL = re.compile(DATE + r'.{0,8}?(?:まで|をもって終了)', re.I)


def load_state():
    if not STATE.exists():
        return None
    data = json.loads(STATE.read_text(encoding='utf-8'))
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ValueError('sent_articles.json must be a JSON string array')
    return data


def save_state(history):
    STATE.write_text(json.dumps(history[-HISTORY_LIMIT:], ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def load_sources():
    if not SOURCE_STATE.exists():
        return set(ORIGINAL_SOURCES)
    data = json.loads(SOURCE_STATE.read_text(encoding='utf-8'))
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ValueError('active_sources.json must be a JSON string array')
    return set(data) | ORIGINAL_SOURCES


def save_sources(sources):
    SOURCE_STATE.write_text(json.dumps(sorted(sources), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def is_safe_link(link):
    p = urlsplit(link)
    return p.scheme == 'https' and p.hostname in ARTICLE_HOSTS and not p.username and not p.password


def published_iso(entry):
    for attr in ('published_parsed', 'updated_parsed'):
        value = entry.get(attr)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc).isoformat()
    return None


def parse_event_end(text):
    """Extract only explicit year-grounded end dates; never use publication date."""
    candidates = []
    for match in RANGE.finditer(text):
        year = int(match['endyear'] or match['year'])
        if not match['endyear'] and int(match['endmonth']) < int(match['month']):
            year += 1
        try:
            candidates.append(datetime(year, int(match['endmonth']), int(match['endday']), tzinfo=JST).date())
        except ValueError:
            pass
    for match in ENDS.finditer(text):
        try:
            candidates.append(datetime(int(match['year']), int(match['month']), int(match['day']), tzinfo=JST).date())
        except ValueError:
            pass
    for match in END_WITH_UNTIL.finditer(text):
        context = text[max(0, match.start()-65):match.start()]
        if re.search(r'開催|会期|コラボ|カフェ|ショップ|展示|イベント|販売|営業', context):
            try:
                candidates.append(datetime(int(match['year']), int(match['month']), int(match['day']), tzinfo=JST).date())
            except ValueError:
                pass
    return max(candidates) if candidates else None


def event_text(entry, link):
    summary = BeautifulSoup(entry.get('summary', ''), 'html.parser').get_text(' ', strip=True)
    pieces = [summary]
    try:
        # Avoid following redirects to unrelated hosts.
        response = requests.get(link, timeout=12, headers=HEADERS, allow_redirects=False)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'html.parser')
            for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
                tag.decompose()
            article = soup.find('article') or soup.find('main')
            if article:
                pieces.append(article.get_text(' ', strip=True)[:15000])
        else:
            print(f'WARNING: article HTTP {response.status_code}: {link}', flush=True)
    except requests.RequestException as exc:
        print(f'WARNING: article fetch failed: {link}: {exc}', file=sys.stderr, flush=True)
    return re.sub(r'\s+', ' ', ' '.join(pieces))


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
            accepted = skipped_old = unknown_included = skipped_stale = 0
            for entry in feed.entries:
                title = re.sub(r'\s+', ' ', html.unescape(entry.get('title', ''))).strip()
                link = entry.get('link', '').strip()
                if not title or not is_safe_link(link) or EXCLUDE.search(title):
                    continue
                summary = BeautifulSoup(entry.get('summary', ''), 'html.parser').get_text(' ', strip=True)[:1000]
                match = next(((label, color) for label, color, pattern in CATEGORIES
                              if re.search(pattern, title, re.I)), None)
                if not match and EVENT_ACTION.search(summary):
                    match = next(((label, color) for label, color, pattern in CATEGORIES
                                  if re.search(pattern, summary, re.I)), None)
                if not match:
                    continue
                published = published_iso(entry)
                # Unknown or old publication dates must not bypass the safety
                # window when the event end date is unknown.
                published_dt = datetime.fromisoformat(published) if published else None
                if published_dt and published_dt > now + timedelta(hours=1):
                    skipped_stale += 1
                    continue
                if published_dt and published_dt < now - timedelta(hours=MAX_ARTICLE_AGE_HOURS):
                    skipped_stale += 1
                    continue
                content = event_text(entry, link)
                event_end = parse_event_end(content)
                if event_end is not None and event_end <= (now.astimezone(JST).date() - timedelta(days=EVENT_END_GRACE_DAYS)):
                    skipped_old += 1
                    continue
                if event_end is None:
                    if published_dt is None:
                        # No trustworthy freshness signal: avoid historic floods.
                        skipped_stale += 1
                        continue
                    unknown_included += 1
                key = link.split('#', 1)[0]
                if key not in items:
                    items[key] = dict(key=key, title=title, link=link, source=source,
                                      category=match[0], color=match[1], published=published,
                                      event_end=str(event_end) if event_end else None)
                    accepted += 1
            print(f'RSS OK: {source}: total={len(feed.entries)} matched={accepted} '
                  f'old_event_excluded={skipped_old} unknown_date_included={unknown_included} '
                  f'stale_or_undated_excluded={skipped_stale}', flush=True)
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
    if item.get('event_end'):
        embed['fields'].append({'name': '📅 確認できた終了日', 'value': item['event_end'], 'inline': True})
    else:
        embed['fields'].append({'name': '📅 終了日', 'value': '未確認（記事本文をご確認ください）', 'inline': True})
    if item['published']:
        embed['timestamp'] = item['published']
    payload = {'username': 'アニメコラボ・イベント速報',
               'allowed_mentions': {'parse': []}, 'embeds': [embed]}
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