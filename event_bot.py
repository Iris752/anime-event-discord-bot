"""Anime event news RSS -> Discord. Python 3.11+. v2.5"""
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
    'テレビ東京アニメ': 'https://www.tv-tokyo.co.jp/contents/anime/topics/index.xml',
}
ORIGINAL_SOURCES = {'アニメ！アニメ！'}
MAX_ARTICLE_AGE_HOURS = 72
EVENT_END_GRACE_DAYS = 14
JST = timezone(timedelta(hours=9))
HEADERS = {'User-Agent': 'AnimeEventDiscordBot/2.5 (public RSS reader)'}
ARTICLE_HOSTS = {'animeanime.jp', 'www.animeanime.jp', 'natalie.mu', 'www.tv-tokyo.co.jp'}

CATEGORIES = [
    ('☕ コラボカフェ', 0xE6A23C, r'コラボカフェ|コラボ喫茶|コラボメニュー|テーマカフェ|コラボレストラン|コラボ喫茶店|コラボドリンク|カフェコラボ|カフェ(?:が|を|で)?(?:開催|オープン)'),
    ('🛍️ ポップアップ', 0x9B59B6, r'ポップアップ|POP[ -]?UP|期間限定ショップ|コラボショップ|期間限定ストア|ポップアップストア|POPUP|催事ショップ'),
    ('🖼️ 展示会・原画展', 0x3498DB, r'原画展|展覧会|展示会|企画展|特別展|アニメ展|作品展|原作展|イラスト展'),
    ('🎪 イベント', 0x2ECC71, r'コラボイベント|ファンミーティング|トークショー|スタンプラリー|上映会|リアルイベント|アニメイベント|コラボ開催|コラボ企画|先行上映|アニメフェス|コラボフェア|謎解きイベント|コラボキャンペーン|アニメコラボ'),
]
EXCLUDE = re.compile(r'開催中止|開催延期|払い戻し|返金|お詫び|訂正のお知らせ', re.I)
# A headline must contain an event category, OR a summary must have both a
# category and an event-action cue. This avoids posting unrelated anime news.
EVENT_ACTION = re.compile(r'開催|オープン|出店|登場|実施|販売|会場|会期|展開|実施決定|開催決定|実施中|開催中|スタート|出展|期間限定')
# Require concrete event context when the category appears only in a summary.
SUMMARY_EVENT_CONTEXT = re.compile(r'コラボ|イベント|カフェ|ショップ|展示|フェア|ポップアップ|上映|スタンプラリー')

# Anime/manga-focused edition. Event keywords alone are not sufficient.
# Japanese quoted work titles are strong evidence only when paired with an
# anime/manga-related term, avoiding generic brand collaboration announcements.
ANIME_MANGA = re.compile(
    r'TVアニメ|テレビアニメ|劇場版アニメ|アニメ映画|アニメ化|アニメ作品|'
    r'原作漫画|漫画作品|コミック作品|マンガ作品|少年ジャンプ|週刊少年|'
    r'描き下ろし|描きおろし|原作イラスト|キャラクターグッズ|'
    r'アニメイト|ジャンプショップ|JUMP SHOP|原画展|複製原画|'
    r'コミックス|単行本|原作者|漫画家|まんが|マンガ|漫画|アニメ', re.I)
WORK_TITLE = re.compile(r'『[^』]{2,65}』|「[^」]{2,65}」|\"[^\"]{2,65}\"')
MANGA_EVENT_CONTEXT = re.compile(r'原画展|原作展|コミックス|漫画家|複製原画|アニメイト|ジャンプショップ|JUMP SHOP', re.I)
NON_TARGET = re.compile(
    r'ビジネスセミナー|投資セミナー|不動産フェア|企業向け展示会|'
    r'就職説明会|転職フェア|採用イベント|政治集会|自動車展示会', re.I)
# Don't treat a mere anime mention in a long article as sufficient: prefer
# title/summary evidence and require an actual event category.
def is_anime_manga_event(title, summary, source):
    headline = f'{title} {summary[:500]}'
    if NON_TARGET.search(title):
        return False
    if ANIME_MANGA.search(title):
        return True
    if ANIME_MANGA.search(summary[:500]) and (WORK_TITLE.search(title) or MANGA_EVENT_CONTEXT.search(headline)):
        return True
    # Comic Natalie is manga-focused, but generic events still need a named
    # work or manga-specific event context.
    if source == 'コミックナタリー' and (WORK_TITLE.search(title) or MANGA_EVENT_CONTEXT.search(headline)):
        return True
    return False

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

