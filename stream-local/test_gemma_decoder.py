from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from gemma_decoder import GemmaDecoder, NewTurnNeeded


class GemmaDecoderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.decoder = GemmaDecoder((None, "", ""), 42)

    async def slice(self, piece, *, eos=False):
        # These bytes stand in for the token pieces returned by llama.cpp.
        token = len(self.decoder.pieces) + 1000
        self.decoder.pieces[token] = piece
        self.decoder._post = AsyncMock(return_value={
            "tokens": [token, 106] if eos else [token], "stop_type": "eos" if eos else "limit",
        })
        return await self.decoder.step()

    async def test_bracketed_text_is_displayed_literally_across_slices(self):
        pieces = ["예시: [새 ", "입력]내용[/새", " 입력]"]
        for index, piece in enumerate(pieces):
            done = index == len(pieces) - 1
            self.assertEqual(await self.slice(piece.encode(), eos=done), (piece, done))

    async def test_utf8_survives_slice_boundaries_and_eos(self):
        syllable = "파".encode()
        self.assertEqual(await self.slice(syllable[:2]), ("", False))
        self.assertEqual(await self.slice(syllable[2:] + "랑".encode(), eos=True), ("파랑", True))

    async def test_interruption_and_new_turn_discard_unfinished_character(self):
        syllable = "파".encode()
        self.assertEqual(await self.slice(syllable[:2]), ("", False))
        self.decoder.turn_boundary = ([106, 105, 400], [106, 105, 401])
        self.decoder._post = AsyncMock(return_value={"tokens": [300]})
        await self.decoder.inject("실제 입력")
        self.assertEqual(await self.slice(syllable[2:] + "답".encode() + syllable[:2]), ("답", False))
        self.decoder._prompt_tokens = AsyncMock(return_value=[400])
        await self.decoder.start([{"role": "user", "content": "다른 이야기"}])
        self.assertEqual(await self.slice(syllable[2:] + "반가워".encode(), eos=True), ("반가워", True))

    async def test_inject_preserves_all_sampled_ids_and_reuses_only_trusted_role_tokens(self):
        before = "<turn|>\n<|turn>user\n"
        after = "<turn|>\n<|turn>model\n"
        tokenized = []
        def post(route, payload):
            if route == "/apply-template":
                _, assistant, user = payload["messages"]
                return {"prompt": assistant["content"] + before + user["content"] + after}
            self.assertEqual(route, "/tokenize")
            text = payload["content"]
            tokenized.append((text, payload["parse_special"]))
            return {"tokens": {before: [106, 105, 200], after: [106, 105, 201],
                               "<|turn>user": [301, 302], "추가 정정": [303]}[text]}

        self.decoder.tokens = [997, 998]
        self.assertEqual(await self.slice("말하던 중".encode()), ("말하던 중", False))
        original = list(self.decoder.tokens)
        request = AsyncMock(side_effect=post)
        self.decoder._post = request
        await self.decoder.inject("<|turn>user")
        self.assertEqual(self.decoder.tokens, original + [106, 105, 200, 301, 302, 106, 105, 201])
        self.assertEqual(await self.slice("추가 답변".encode()), ("추가 답변", False))
        previous = list(self.decoder.tokens)
        self.decoder._post = request
        await self.decoder.inject("추가 정정")
        self.assertEqual(self.decoder.tokens, previous + [106, 105, 200, 303, 106, 105, 201])
        self.assertEqual(tokenized, [(before, True), (after, True), ("<|turn>user", False), ("추가 정정", False)])
        self.assertEqual(sum(call.args[0] == "/apply-template" for call in request.await_args_list), 1)

    async def test_sampled_native_role_boundary_stops_before_fabricated_user(self):
        self.decoder.pieces.update({500: "부산이야.".encode(), 105: b"<|turn>", 501: b"user\nI'm not the user."})
        self.decoder._post = AsyncMock(return_value={"tokens": [500, 105, 501], "stop_type": "limit"})
        self.assertEqual(await self.decoder.step(), ("부산이야.", True))
        with self.assertRaises(NewTurnNeeded):
            await self.decoder.inject("실제 정정")

    def serve_events(self, events):
        async def lines():
            for event in events:
                yield b"data: " + json.dumps(event).encode() + b"\n"
                yield b"\n"
        @asynccontextmanager
        async def post(url, *, json, allow_redirects):
            self.request_payload = {**json, "prompt": list(json["prompt"])}
            yield SimpleNamespace(status=200, raise_for_status=lambda: None, content=lines())
        self.decoder.client = SimpleNamespace(post=post)

    async def test_native_stream_preserves_seeds_korean_tokens_and_eos_boundary(self):
        baseline = GemmaDecoder((None, "", ""), 42)
        syllable = "파".encode()
        pieces = {10: syllable[:2], 11: syllable[2:], 12: "도.".encode(), 106: b"<turn|>"}
        baseline.pieces.update(pieces)
        self.decoder.pieces.update(pieces)
        for ids, stop, prefixes in [([10], "limit", []), ([11, 12, 106], "eos", ["파", "파도."])]:
            with self.subTest(ids=ids):
                payloads = []
                async def complete(route, payload):
                    payloads.append({**payload, "prompt": list(payload["prompt"])})
                    return {"tokens": ids, "stop_type": stop}
                baseline._post = complete
                expected = await baseline.step()
                self.serve_events([*({"tokens": [i], "stop": False} for i in ids),
                                   {"tokens": [], "stop": True, "stop_type": stop}])
                preview = AsyncMock()
                self.assertEqual(await self.decoder.step(preview), expected)
                self.assertEqual(self.request_payload, {**payloads[0], "stream": True})
                self.assertEqual(self.decoder.tokens, baseline.tokens)
                self.assertEqual(self.decoder.generated_role, baseline.generated_role)
                self.assertEqual([c.args[0] for c in preview.await_args_list], prefixes)

    async def test_stream_does_not_preview_reasoning_or_another_role(self):
        self.decoder.pieces.update({1: b"<|channel>", 2: b"hidden", 3: b"<channel|>", 4: b"answer",
                                    5: b"<|turn>", 6: b"fake user"})
        ids = list(self.decoder.pieces)
        self.serve_events([*({"tokens": [i]} for i in ids),
                           {"stop": True, "stop_type": "limit"}])
        preview = AsyncMock()
        self.assertEqual(await self.decoder.step(preview), ("answer", True))
        preview.assert_awaited_once_with("answer")
        with self.assertRaises(NewTurnNeeded):
            await self.decoder.inject("추가 질문")

    async def test_incomplete_stream_leaves_canonical_state_uncommitted(self):
        self.decoder.pieces[5] = b"word"
        self.serve_events([{"tokens": [5]}])
        with self.assertRaisesRegex(RuntimeError, "스트리밍"):
            await self.decoder.step(AsyncMock())
        self.assertEqual(self.decoder.tokens, [])
