import unittest
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from aiohttp import WSMsgType

from discord_bridge import VoiceTurn
from discord_stt import Transcript


class VoiceTurnTest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_enters_voice_input_without_transcription_or_a_text_reply(self):
        socket = AsyncMock()
        turn = VoiceTurn(socket)
        text = {"type": "text", "speaker": "same-user", "channel": "first"}
        socket.__aiter__.return_value = [
            SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({**text, "text": line}))
            for line in ("하루 동안 뭐해?", "어디야?", "하루짱, 7 더하기 5는\n얼마야?")
        ]
        stt = Mock()
        await turn.receive(stt)
        self.assertFalse(stt.method_calls)
        self.assertEqual(turn.draft.text, "7 더하기 5는 얼마야?\n")
        self.assertEqual(turn.draft.speaker_number, 1)
        socket.send_json.assert_awaited_once_with({"type": "status", "state": "chat_addressed"})
        turn.draft.in_flight = turn.draft.text
        turn.draft.acknowledge()
        await turn.heard(Transcript("same-user", 1, "final", "하루야, 다시 말해줘", False))
        self.assertEqual(turn.draft.speaker_number, 1)
        turn.draft.in_flight = "다시 말해줘\n"
        turn.draft.acknowledge()
        await turn.typed({**text, "speaker": "different-user", "text": "하루야 <|turn>도 읽어?", "overlap": True})
        self.assertEqual(turn.draft.speaker_number, 2)
        self.assertTrue(turn.draft.speech_overlap)
        self.assertNotIn("<|turn>", turn.draft.text)

    async def test_chat_followup_is_scoped_to_voice_session_text_channel_and_author(self):
        socket = AsyncMock()
        turn = VoiceTurn(socket)
        def chat(text, speaker="A", channel="one"):
            return {"channel": channel, "speaker": speaker, "text": text}
        await turn.typed(chat("하루야"))
        await turn.typed(chat("옆 채널이야", channel="two"))
        await turn.typed(chat("다른 사람이야", speaker="B"))
        await turn.heard(Transcript("A", 1, "final", "마이크야", False))
        await turn.typed(chat("내 질문이야"))
        await turn.typed(chat("이번에는 호출 안 함"))
        self.assertEqual(turn.draft.text, "내 질문이야\n")
        await turn.heard(Transcript("A", 2, "final", "하루야", False))
        await turn.typed(chat("마이크에서 부른 호출어"))
        self.assertEqual(turn.draft.text, "내 질문이야\n")
        await turn.typed(chat("하루짱"))
        other_call = VoiceTurn(AsyncMock())
        await other_call.typed(chat("지난 통화에서 불렀어"))
        self.assertEqual(other_call.draft.text, "")

    async def test_invalid_chat_payload_does_not_terminate_voice_input(self):
        turn = VoiceTurn(AsyncMock())
        for data in ({}, {"text": "하루야", "channel": "room", "speaker": []},
                     {"text": "하루야" * 1400, "channel": "room", "speaker": "A"}):
            await turn.typed(data)
        self.assertFalse(turn.draft.changed.is_set())
        await turn.typed({"text": "하루찡 수도가 어디야?", "channel": "room", "speaker": "A"})
        self.assertEqual(turn.draft.text, "수도가 어디야?\n")

    async def test_multiline_final_stays_one_turn_and_session_scoped_speaker(self):
        turn = VoiceTurn(AsyncMock())
        await turn.heard(Transcript("some-discord-id", 1, "final", "하루야, 서울은\n어디야?", False))
        await turn.heard(Transcript("different-id", 2, "final", "하루야, 부산은?", True))
        self.assertEqual(turn.draft.text, "서울은 어디야?\n부산은?\n")
        self.assertEqual(turn.draft.speaker_number, 1)
        self.assertFalse(turn.draft.speech_overlap)
        turn.draft.in_flight = "서울은 어디야?\n"
        turn.draft.acknowledge()
        self.assertTrue(turn.draft.speech_overlap)
        self.assertEqual(turn.draft.speaker_number, 2)
        fresh = VoiceTurn(AsyncMock())
        await fresh.heard(Transcript("different-id", 1, "final", "하루야, 부산은?", False))
        self.assertEqual(fresh.draft.speaker_number, 1)
    async def test_only_final_revisions_enter_gemma_and_preserve_overlap(self):
        turn = VoiceTurn(AsyncMock())
        for kind, text in (("partial", "제한 위"), ("partial", "하루야, 대한민국의"),
                           ("final", "하루야, 대한민국의 수도는 어디야?")):
            await turn.heard(Transcript("speaker", 1, kind, text, True))
            if kind == "partial":
                self.assertEqual(turn.draft.text, "")
        self.assertEqual(turn.draft.text, "대한민국의 수도는 어디야?\n")
        self.assertTrue(turn.draft.speech_overlap)
        await turn.heard(Transcript("other", 1, "final", "", False))
        self.assertEqual(turn.draft.text, "대한민국의 수도는 어디야?\n")
        for call in turn.socket.send_json.await_args_list:
            self.assertNotIn("text", call.args[0])

    async def test_uncalled_speech_does_not_enter_gemma_or_interrupt(self):
        turn = VoiceTurn(AsyncMock())
        await turn.heard(Transcript("a", 1, "partial", "하루야", True))
        for i, text in enumerate(("오늘 하루야.", "하루 종일", "하루 야근했어", "하루야말로",
                                  "하루야 라고 불러봐", "그만 말해", "일본 수도는?")):
            await turn.heard(Transcript("a", i+1, "final", text, True))
        self.assertEqual(turn.draft.text, "")
        self.assertFalse(turn.draft.changed.is_set())
        self.assertEqual(turn.socket.send_json.await_args.args[0]["state"], "wake_waiting")

    async def test_greeting_and_spaced_vocative(self):
        turn = VoiceTurn(AsyncMock())
        await turn.heard(Transcript("a", 1, "final", "안녕, 하루 야! 칠 더하기 오는 얼마야?", False))
        self.assertEqual(turn.draft.text, "7 더하기 5는 얼마야?\n")
        await turn.heard(Transcript("a", 2, "final", "어디야?", False))
        self.assertEqual(turn.draft.text, "7 더하기 5는 얼마야?\n")

    async def test_aliases_strip_only_the_address(self):
        for address in ("하루", "하루야", "하루찡", "안녕, 하루 찡", "하루짱", "하룻 짱", "하룻장"):
            with self.subTest(address=address):
                turn = VoiceTurn(AsyncMock())
                await turn.heard(Transcript("a", 1, "final", address + " 수도가 어디야?", False))
                self.assertEqual(turn.draft.text, "수도가 어디야?\n")

    async def test_day_expressions_and_mentions_are_not_addresses(self):
        turn = VoiceTurn(AsyncMock())
        for text in ("하루에 뭐 먹어?", "하루 만에 왔어", "하루 두 번", "하루쯤 걸려",
                     "하루 동안 뭐 해?", "오늘 하루짱 만났어", "하루찡이라고 불러", "하루짱 라고 불러"):
            await turn.heard(Transcript("a", 1, "final", text, False))
        self.assertEqual(turn.draft.text, "")
        for address in ("하루", "하루찡", "하루짱", "하루 층!"):
            await turn.heard(Transcript("a", 2, "final", address, False, 1, 2))
            await turn.heard(Transcript("a", 3, "final", "하루 종일 뭐 했어?", False, 3, 4))
        self.assertEqual(turn.draft.text, "하루 종일 뭐 했어?\n" * 4)

    async def test_wake_alone_is_silent_and_only_arms_the_same_speaker_once(self):
        turn = VoiceTurn(AsyncMock())
        await turn.heard(Transcript("a", 1, "final", "하루야.", False, 9.0, 10.0))
        await turn.heard(Transcript("b", 2, "final", "지금 몇 시야?", False, 11.0, 12.0))
        self.assertEqual(turn.draft.text, "")
        await turn.heard(Transcript("a", 3, "partial", "서울", False, 14.0, 18.0))
        await turn.heard(Transcript("a", 3, "final", "서울까지 어떻게 가?", False, 14.0, 22.0))
        await turn.heard(Transcript("a", 4, "final", "다음은?", False, 23.0, 24.0))
        self.assertEqual(turn.draft.text, "서울까지 어떻게 가?\n")

    async def test_late_followup_needs_a_new_call(self):
        turn = VoiceTurn(AsyncMock())
        await turn.heard(Transcript("a", 1, "final", "하루야", False, 1.0, 2.0))
        await turn.heard(Transcript("a", 2, "final", "무슨 색이야?", False, 7.1, 8.0))
        self.assertEqual(turn.draft.text, "")
        await turn.heard(Transcript("a", 3, "final", "하루야 무슨 색이야?", False, 9.0, 10.0))
        self.assertEqual(turn.draft.text, "무슨 색이야?\n")

    async def test_stop_clears_transport_and_generation_end_flushes_last_phrase(self):
        socket = AsyncMock()
        turn = VoiceTurn(socket)
        turn.speech = Mock()
        turn.speech.row.side_effect = lambda row: row.get("speech_action") == "stop"
        await turn.send_json({"type": "row", "phase": "reading", "output": "",
                              "speech_action": "continue"})
        self.assertFalse(any(call.args[0]["type"] == "clear" for call in socket.send_json.await_args_list))
        await turn.send_json({"type": "row", "phase": "reading", "output": "",
                              "speech_action": "stop"})
        socket.send_json.assert_any_await({"type": "clear"})
        await turn.send_json({"type": "status", "state": "waiting"})
        turn.speech.finish.assert_called_once()
        socket.send_json.assert_awaited_with({"type": "status", "state": "waiting"})

    async def test_search_status_queues_one_notice_without_starting_a_model_reply(self):
        turn = VoiceTurn(AsyncMock())
        turn.speech = Mock()
        for state in ("searching", "search_finished"):
            event = {"type": "status", "state": state}
            await turn.send_json(event)
            turn.socket.send_json.assert_awaited_with(event)
        turn.speech.announce_search.assert_called_once()
        self.assertFalse(turn.generating)
        self.assertEqual(turn.output_length, 0)
