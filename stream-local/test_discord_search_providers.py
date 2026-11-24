import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

import discord_search_providers as providers


def news_item(title, publisher, date, key):
    return (f'<item><title>{title} - {publisher}</title>'
            f'<link>https://news.google.com/rss/articles/{key}</link>'
            f'<source url="https://paper.example">{publisher}</source>'
            f'<pubDate>{date}</pubDate><description>Other headlines</description></item>')


class SearchProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_news_sorts_before_limiting_and_requires_date_and_publisher(self):
        items = [news_item(f'item {day}', '신문', f'{day} Jan 2026 00:00 +0900', day)
                 for day in range(1, 9)]
        items.extend([items[-1], news_item('item 8', '신문', '8 Jan 2026 00:00 +0900', 101),
                      news_item('undated', '신문', 'unknown', 99),
                      news_item('invalid source', '신문', '10 Jan 2026 00:00 +0000', 100)
                      .replace('https://paper.example', 'file:///tmp/input')])
        body = ('<rss><channel>' + ''.join(items) + '</channel></rss>').encode()
        with patch.object(providers, '_request', AsyncMock(return_value=body)) as request:
            provider, rows = await providers.lookup('2026년 1월 한국은행 기준금리 뉴스', news=True)
        self.assertEqual(provider, 'google_news_rss')
        self.assertEqual([row['title'] for row in rows], [f'item {day}' for day in range(8, 3, -1)])
        self.assertEqual(rows[0]['date'], '2026-01-08')
        self.assertEqual(rows[0]['source'], '신문')
        self.assertEqual(rows[0]['snippet'], '')
        self.assertEqual(request.await_args.kwargs['params']['q'], '2026년 1월 한국은행 기준금리')
        self.assertNotIn('headers', request.await_args.kwargs)

    def test_news_matches_topics_and_preserves_claim_and_publication_date(self):
        now = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
        query, terms, since = providers._news_query('기준금리 관련 최신 뉴스', now)
        self.assertEqual(query, '기준금리 when:7d')
        items = [
            (' unrelated', '23 Nov 2026 11:00 +0000'),
            ('다음 주에 무슨 일 있나?｜23일 비·26일 기준금리 결정·강릉 단단단페스티벌', '22 Nov 2026 15:15 +0000'),
            ('한·미 기준금리 차 2.5%p…전문가 “줄어들 수 있다”', '23 Nov 2026 08:25 +0000'),
            ('기준금리 인상 확정', '02 Nov 2026 00:00 +0000'),
            ('기준금리 동결 확정', '30 Nov 2026 00:00 +0000'),
        ]
        body = ('<rss><channel>' + ''.join(news_item(title, '신문', date, i)
                for i, (title, date) in enumerate(items)) + '</channel></rss>').encode()
        rows = providers._news(body, terms, since, now)
        self.assertEqual([r['title'] for r in rows], [items[2][0], '26일 기준금리 결정'])
        self.assertEqual([r['date'] for r in rows], ['2026-11-23'] * 2)
        self.assertEqual(providers._news_title(
            '주요 일정｜23일 한·미 기준금리 전망 2.5%·26일 축제', {'기준금리'}),
            '23일 한·미 기준금리 전망 2.5%')
        self.assertEqual(providers._hits('한은, 기준금리 인상 가능성', {'한국은행', '기준금리'}), 2)

    def test_unfocused_today_query_retains_topic_and_korean_calendar_boundary(self):
        now = datetime(2026, 11, 23, 0, tzinfo=timezone.utc)
        search, terms, since = providers._news_query('오늘 주요 뉴스', now)
        self.assertEqual(search, '오늘 주요 뉴스 when:1d')
        self.assertEqual(terms, [])
        self.assertEqual(since.astimezone(timezone.utc), datetime(2026, 11, 22, 15, tzinfo=timezone.utc))

    async def test_lite_pairs_snippets_and_decodes_result_links(self):
        body = b'''<html><a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2Fko">Python <b>docs</b></a>
                   <td class="result-snippet">Read <b>here</b>.</td>
                   <a class="result-link" href="file:///tmp/a">local</a>
                   <td class="result-snippet">incorrect snippet</td></html>'''
        with patch.dict(os.environ, {'SERPER_API_KEY': '', 'HARU_REPO': ''}), \
                patch.object(providers, '_request', AsyncMock(return_value=body)) as request:
            engine, rows = await providers.lookup('파이썬 문서', news=False)
        self.assertEqual(engine, 'duckduckgo_lite')
        self.assertEqual(rows, [{'title': 'Python docs', 'source': 'docs.python.org',
                                 'url': 'https://docs.python.org/ko', 'snippet': 'Read here.'}])
        self.assertEqual(request.await_args.kwargs, {'params': {'q': '파이썬 문서'}})
        self.assertEqual(providers._lite(body + b'<form id="anomaly-modal"></form>'), [])

    async def test_serper_reads_dedicated_key_and_sends_only_query_and_locale(self):
        response = json.dumps({'organic': [
            {'title': 'docs', 'link': 'https://docs.python.org', 'snippet': 'Python', 'date': '2026'},
            {'title': 'credential link', 'link': 'https://hidden@sample.org'},
            {'title': 'duplicate', 'link': 'https://docs.python.org'},
        ]}).encode()
        with tempfile.TemporaryDirectory() as repo:
            key_file = Path(repo) / '.runtime' / 'serper.key'
            key_file.parent.mkdir()
            key_file.write_text('fixture-only\n')
            with patch.dict(os.environ, {'HARU_REPO': repo, 'SERPER_API_KEY': ''}), \
                    patch.object(providers, '_request', AsyncMock(return_value=response)) as request:
                engine, rows = await providers.lookup('파이썬 문서', news=False)
                self.assertEqual(request.await_args.args[:2], ('POST', providers.SERPER_URL))
                self.assertEqual(request.await_args.kwargs, {'headers': {'X-API-KEY': 'fixture-only'},
                                 'json': {'q': '파이썬 문서', 'gl': 'kr', 'hl': 'ko', 'num': 5}})
                with patch.dict(os.environ, {'SERPER_API_KEY': 'env-override'}):
                    self.assertEqual(providers._serper_key(), 'env-override')
        self.assertEqual(engine, 'google_serper')
        self.assertEqual(rows, [{'source': 'docs.python.org', 'title': 'docs', 'snippet': 'Python',
                                 'url': 'https://docs.python.org', 'date': '2026'}])

    async def test_invalid_query_failures_and_cancellation(self):
        with patch.object(providers, '_request', AsyncMock()) as request:
            for q in ('', '  ', 'a\nsecret', 'a' * 161):
                self.assertEqual(await providers.lookup(q, news=True), ('google_news_rss', []))
            request.assert_not_awaited()
            for malformed in (b'<bad xml', b'<!DOCTYPE rss><rss/>'):
                request.return_value = malformed
                self.assertEqual(await providers.lookup('공개 검색', news=True), ('google_news_rss', []))
            for error in (TimeoutError(), aiohttp.ClientConnectionError()):
                request.side_effect = error
                self.assertEqual(await providers.lookup('공개 검색', news=True), ('google_news_rss', []))
            request.side_effect = asyncio.CancelledError()
            with self.assertRaises(asyncio.CancelledError):
                await providers.lookup('공개 검색', news=True)

    async def test_transport_rejects_redirect_type_and_oversize_chunked_body(self):
        response = MagicMock(status=200, content_type='text/html')
        session = MagicMock()
        session.request.return_value.__aenter__.return_value = response
        with patch.object(aiohttp, 'ClientSession') as make_session:
            make_session.return_value.__aenter__.return_value = session

            async def chunks(size):
                self.assertLessEqual(size, providers.MAX_BYTES)
                yield b'a' * providers.MAX_BYTES
                yield b'b'

            response.content.iter_chunked = chunks
            for status, mime in ((200, 'text/html'), (302, 'text/html'), (200, 'text/plain')):
                response.status = status
                response.content_type = mime
                self.assertEqual(await providers._request('GET', providers.LITE_URL, {'text/html'}), b'')
        self.assertFalse(session.request.call_args.kwargs['allow_redirects'])
        self.assertFalse(make_session.call_args.kwargs['trust_env'])
        self.assertIsInstance(make_session.call_args.kwargs['cookie_jar'], aiohttp.DummyCookieJar)
        self.assertLessEqual(make_session.call_args.kwargs['timeout'].total, 4)


if __name__ == '__main__':
    unittest.main()
