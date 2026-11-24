import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from discord_search import OPT_OUT, PRIVATE_REQUEST, UNCERTAIN, UNAVAILABLE, search_context


class SearchRoutingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = MagicMock()
        self.backend = (self.client, "http://localhost", "local")
        self.response = self.client.post.return_value.__aenter__.return_value
        self.response.raise_for_status = MagicMock()
        self.lookup = self.enterContext(patch("discord_search_providers.lookup", new_callable=AsyncMock))
        self.notice = AsyncMock()

    def decision(self, content, finish="stop"):
        self.response.json.return_value = {"choices": [{"finish_reason": finish, "message": {"content": content}}]}

    async def ask(self, text):
        return await search_context(text, self.backend, on_search=self.notice)

    async def test_model_can_select_web_without_an_intent_keyword(self):
        request = "마침 쉬는 중인데 국립중앙박물관 월요일에도 열어?"
        self.decision("WEB 국립중앙박물관 월요일")
        events = []

        async def announce():
            events.append("notice")

        async def lookup(query, *, news):
            events.append("lookup")
            return "duckduckgo_lite", [{"title": "관람 안내", "source": "museum.go.kr"}]

        self.lookup.side_effect = lookup
        evidence = await search_context(request, self.backend, on_search=announce)
        self.assertEqual(events, ["notice", "lookup"])
        self.lookup.assert_awaited_once_with("국립중앙박물관 월요일", news=False)
        self.assertIn("museum.go.kr", evidence)
        self.client.post.assert_called_once()
        local_request = self.client.post.call_args
        self.assertEqual(local_request.args, ("http://localhost/v1/chat/completions",))
        self.assertEqual(local_request.kwargs["json"]["messages"][-1]["content"], request)

    async def test_model_selects_news_and_preserves_spoken_korean_spacing(self):
        self.decision("NEWS 한국은행 기준금리")
        self.lookup.return_value = ("google_news_rss", [])
        self.assertEqual(await self.ask("한국은행 기준 금리 관련해서 무슨 일이 보도됐어?"), UNAVAILABLE)
        self.notice.assert_awaited_once_with()
        self.lookup.assert_awaited_once_with("한국은행 기준금리", news=True)

    async def test_implicit_public_field_is_allowed_but_a_new_place_or_time_is_not(self):
        self.decision("WEB 서울 날씨")
        self.lookup.return_value = ("duckduckgo_lite", [])
        await self.ask("오늘 서울에서 우산 챙겨야 할까?")
        self.lookup.assert_awaited_once_with("서울 날씨", news=False)
        self.decision("WEB 국립중앙박물관 입장 가능 여부")
        await self.ask("지금 국립중앙박물관 가면 들어갈 수 있을까?")
        self.lookup.assert_awaited_with("국립중앙박물관 입장 가능 여부", news=False)
        for reply in ("WEB 부산 날씨", "WEB 서울 내일 날씨"):
            self.decision(reply)
            self.assertEqual(await self.ask("오늘 서울에서 우산 챙겨야 할까?"), UNCERTAIN)
        self.assertEqual(self.lookup.await_count, 2)

    async def test_todays_news_retains_calendar_day_even_when_model_only_extracts_topic(self):
        self.decision("NEWS 한국은행")
        self.lookup.return_value = ("google_news_rss", [])
        await self.ask("오늘 한국은행 관련 뉴스 알려줘")
        self.lookup.assert_awaited_with("한국은행 오늘", news=True)
        await self.ask("오늘 힘들었는데, 한국은행 최신 뉴스 알려줘")
        self.lookup.assert_awaited_with("한국은행", news=True)

    async def test_model_can_answer_directly_even_with_news_or_dates(self):
        self.decision("DIRECT")
        for request in ("오늘 뉴스를 보다가 슬펐어.", "7 더하기 5는 얼마야?", "기사 제목처럼 시 한 편 써줘"):
            self.assertEqual(await self.ask(request), "")
        self.assertEqual(self.client.post.call_count, 3)
        self.lookup.assert_not_awaited()
        self.notice.assert_not_awaited()

    async def test_opt_out_and_private_requests_never_leave_the_mac(self):
        for request in ("검색하지 말고 오늘 환율 알려줘", "구글링 없이 알려줘", "찾아보지 마",
                        "인터넷 쓰지 말고 알려줘", "웹 검색 없이 답해", "로컬로만 답해"):
            self.assertEqual(await self.ask(request), OPT_OUT)
        for request in ("내 이메일 주소를 검색해줘", "010-1234-5678 검색해 줘", "alice@example.com 검색해줘"):
            self.assertEqual(await self.ask(request), PRIVATE_REQUEST)
        self.client.post.assert_not_called()
        self.lookup.assert_not_awaited()
        self.notice.assert_not_awaited()

    async def test_invalid_decisions_do_not_leak_a_query_or_trigger_a_notice(self):
        for content, finish in (("WEB 파이썬 홍길동", "stop"), ("WEB test@example.com", "stop"),
                                ("WEB 구글에서! 파이썬", "stop"), ("NEWS 파이썬", "length"),
                                ("DIRECT 추가 설명", "stop"), ("NONE", "stop"), (None, "stop")):
            with self.subTest(content=content, finish=finish):
                self.decision(content, finish)
                self.assertEqual(await self.ask("구글에서 파이썬 문서 찾아줘"), UNCERTAIN)
        self.lookup.assert_not_awaited()
        self.notice.assert_not_awaited()

    async def test_cancelled_decision_does_not_start_a_search(self):
        self.response.json.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.ask("한국은행 새 소식 전해줘")
        self.notice.assert_not_awaited()
        self.lookup.assert_not_awaited()

    async def test_cancelled_announcement_does_not_start_a_search(self):
        self.decision("WEB 한국은행")
        self.notice.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.ask("한국은행 새 소식 전해줘")
        self.lookup.assert_not_awaited()
