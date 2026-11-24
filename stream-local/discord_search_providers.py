"""Bounded, query-only searches for the Discord voice bridge."""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import aiohttp


MAX_RESULTS = 5
MAX_BYTES = 512 * 1024
REQUEST_SECONDS = 3.5
NEWS_URL = 'https://news.google.com/rss/search'
SERPER_URL = 'https://google.serper.dev/search'
LITE_URL = 'https://lite.duckduckgo.com/lite/'
KST = timezone(timedelta(hours=9))
NEWS_META = re.compile(
    r'(?:(?:오늘|최근|최신|요즘|현재|주요|관련|실시간|속보|헤드라인|뉴스|소식|보도)+'
    r'(?:의|를|은|는)?|today|latest|current|recent|news|headlines?)$', re.I)
HISTORICAL = re.compile(r'\d{4}\s*년?|\d{1,2}\s*[월일]|과거|당시|작년|지난\s*(?:주|달|해|분기)|last\s+(?:week|month|year)', re.I)
CALENDAR = re.compile(r'무슨\s*일|주요\s*일정|일정표|캘린더|캘린다|달력|주간\s*일정')


def _text(value, limit):
    if not isinstance(value, str):
        return ''
    return ' '.join(''.join(ch if ch.isprintable() else ' ' for ch in value).split())[:limit]


def _host(url):
    if not isinstance(url, str) or len(url) > 2048 or any(ch.isspace() or not ch.isprintable() for ch in url):
        return ''
    try:
        parts = urlsplit(url)
        if parts.scheme not in ('https', 'http') or parts.username is not None or parts.password is not None:
            return ''
        host = (parts.hostname or '').encode('idna').decode('ascii').lower()
        if not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}', host):
            return ''
        return host
    except (UnicodeError, ValueError):
        return ''


def _result(title, snippet, url):
    title = _text(title, 200)
    domain = _host(url)
    if domain and title:
        return {'title': title, 'snippet': _text(snippet, 450), 'source': domain, 'url': url}
    return None


def _distinct(rows, *, news=False):
    seen = set()
    results = []
    for row in rows:
        key = (row['source'], row['title']) if news else row['url']
        if key not in seen:
            seen.add(key)
            results.append(row)
        if len(results) == MAX_RESULTS:
            break
    return results


def _news_query(query, now):
    terms = [word for word in query.split() if not NEWS_META.fullmatch(word)]
    search = ' '.join(terms) or query
    if HISTORICAL.search(query):
        return search, terms, None
    # The RSS service can return archival stories even for current questions.
    # Check dates locally as well as requesting a recent window upstream.
    if '오늘' in query or re.search(r'\btoday\b', query, re.I):
        since = now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
        return search + ' when:1d', terms, since
    return search + ' when:7d', terms, now - timedelta(days=7)


def _hits(title, terms):
    compact = re.sub(r'[\W_]+', '', title.casefold())
    # Korean financial headlines conventionally abbreviate the Bank of Korea.
    if '한국은행' in terms and '한은' in compact:
        compact += '한국은행'
    return sum(term in compact for term in terms)


def _news_title(title, terms):
    intro, _, listing = title.replace('｜', '|').partition('|')
    if not terms or not listing or not CALENDAR.search(intro):
        return title
    # Calendar editors use a middle dot both inside names (한·미) and
    # between events. Only split before a dated event or a new full word.
    events = re.split(r'·(?=\s*(?:\d{1,2}일|[가-힣A-Za-z]{2,}))', listing)
    if len(events) < 2:
        return title
    scored = [(_hits(event, terms), event.strip()) for event in events]
    most = max(score for score, _ in scored)
    return ' · '.join(event for score, event in scored if score == most) if most else title


def _news(body, words, since, now):
    if b'<!doctype' in body.lower() or b'<!entity' in body.lower():
        return []
    ranked = []
    terms = {re.sub(r'[\W_]+', '', word.casefold()) for word in words} - {''}
    for item in ElementTree.fromstring(body).findall('./channel/item'):
        source = item.find('source')
        if source is None or not _host(source.get('url')):
            continue
        try:
            published = parsedate_to_datetime(item.findtext('pubDate', ''))
            if published.tzinfo is None:
                continue
            if published > now or (since is not None and published < since):
                continue
            date = published.astimezone(KST).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            continue
        publisher = _text(source.text, 80) or _host(source.get('url'))
        title = item.findtext('title', '').removesuffix(' - ' + publisher)
        # News RSS descriptions often contain a cluster of other publishers'
        # headlines. The item's own headline and date are attributable evidence.
        row = _result(_news_title(title, terms), '', item.findtext('link'))
        if row and row['source'] == 'news.google.com':
            row.update(source=publisher, date=date)
            ranked.append((_hits(row['title'], terms), published, row))
    ranked.sort(key=lambda entry: entry[1], reverse=True)
    best = max((score for score, _, _ in ranked), default=0)
    return _distinct((row for score, _, row in ranked if score == best), news=True)


def _serper(body):
    document = json.loads(body)
    if not isinstance(document, dict) or not isinstance(document.get('organic'), list):
        return []
    rows = []
    for item in document['organic']:
        if not isinstance(item, dict):
            continue
        row = _result(item.get('title'), item.get('snippet'), item.get('link'))
        if row:
            if date := _text(item.get('date'), 60):
                row['date'] = date
            rows.append(row)
    return _distinct(rows)


class LiteParser(HTMLParser):
    """Extract DuckDuckGo Lite result-link/result-snippet pairs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.current = None
        self.field = self.url = ''
        self.parts = []
        self.challenge = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.challenge |= any(
            marker in (attrs.get(key) or '').lower()
            for key in ('id', 'class', 'action') for marker in ('captcha', 'anomaly')
        )
        classes = (attrs.get('class') or '').split()
        if tag == 'a' and 'result-link' in classes:
            self.current = None
            self.field, self.parts = 'title', []
            self.url = attrs.get('href') or ''
        elif tag == 'td' and 'result-snippet' in classes:
            self.field, self.parts = 'snippet', []

    def handle_data(self, data):
        if self.field:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if self.field == 'title' and tag == 'a':
            url = self.url
            if url.startswith('//'):
                url = 'https:' + url
            if _host(url) in ('lite.duckduckgo.com', 'duckduckgo.com', 'www.duckduckgo.com'):
                parts = urlsplit(url)
                if parts.path == '/l/':
                    url = parse_qs(parts.query).get('uddg', [''])[0]
            self.current = _result(''.join(self.parts), '', url)
            if self.current:
                self.rows.append(self.current)
            self.field = ''
        elif self.field == 'snippet' and tag == 'td':
            if self.current:
                self.current['snippet'] = _text(''.join(self.parts), 450)
            self.field = ''
            self.current = None


def _lite(body):
    parser = LiteParser()
    parser.feed(body.decode('utf-8'))
    parser.close()
    return [] if parser.challenge else _distinct(parser.rows)


def _serper_key():
    key = os.environ.get('SERPER_API_KEY', '').strip()
    repo = os.environ.get('HARU_REPO')
    if not key and repo:
        try:
            with (Path(repo) / '.runtime' / 'serper.key').open(encoding='ascii') as handle:
                key = handle.read(514).strip()
        except (OSError, UnicodeError):
            return ''
    return key if key.isascii() and key.isprintable() and not any(c.isspace() for c in key) and len(key) <= 512 else ''


async def _request(method, url, media_types, **kwargs):
    timeout = aiohttp.ClientTimeout(total=REQUEST_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar(),
                                     trust_env=False, headers={'User-Agent': 'HaruVoice/1.0'}) as session:
        async with session.request(method, url, allow_redirects=False, **kwargs) as response:
            if response.status != 200 or response.content_type not in media_types:
                return b''
            body = bytearray()
            async for chunk in response.content.iter_chunked(16 * 1024):
                if len(body) + len(chunk) > MAX_BYTES:
                    return b''
                body.extend(chunk)
            return bytes(body)


async def lookup(query: str, *, news: bool) -> tuple[str, list[dict[str, str]]]:
    key = '' if news else _serper_key()
    provider = 'google_news_rss' if news else ('google_serper' if key else 'duckduckgo_lite')
    if not isinstance(query, str) or not query.strip() or len(query) > 160 or not query.isprintable():
        return provider, []
    query = query.strip()
    try:
        if news:
            now = datetime.now(timezone.utc)
            search, terms, since = _news_query(query, now)
            body = await _request('GET', NEWS_URL, {'application/rss+xml', 'text/xml', 'application/xml'},
                                  params={'q': search, 'hl': 'ko', 'gl': 'KR', 'ceid': 'KR:ko'})
            return provider, _news(body, terms, since, now) if body else []
        elif key:
            body = await _request('POST', SERPER_URL, {'application/json'}, headers={'X-API-KEY': key},
                                  json={'q': query, 'gl': 'kr', 'hl': 'ko', 'num': MAX_RESULTS})
            parse = _serper
        else:
            body = await _request('GET', LITE_URL, {'text/html'}, params={'q': query})
            parse = _lite
        return provider, parse(body) if body else []
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, ElementTree.ParseError):
        return provider, []
