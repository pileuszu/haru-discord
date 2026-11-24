"""Exercise the chat WebSocket handler without loading model weights or opening a port."""

import asyncio
import itertools
import json
import unittest
from functools import partial
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import WSMsgType

import server


class Socket:
    def __init__(self):
        self.closed = False
        self.requests = asyncio.Queue()
        self.responses = asyncio.Queue()

    async def prepare(self, request):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.requests.get()
        if message is None:
            self.closed = True
            raise StopAsyncIteration
        return SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(message))

    async def send_json(self, message):
        self.responses.put_nowait(message)


class Engine:
    silence_id = 0
    tokenizer = SimpleNamespace(
        encode=lambda text: list(map(ord, text)), decode=lambda ids: "".join(map(chr, ids))
    )

    def __init__(self):
        self.sessions = 0
        self.inputs = []
        self.onset_biases = []
        self.outputs = iter(())

    def new_session(self, seed):
        self.sessions += 1
        return self

    def step(self, user_token, onset_bias=0):
        self.inputs.append(user_token)
        self.onset_biases.append(onset_bias)
        return next(self.outputs, self.silence_id)


async def call_model(fn, *args):
    # A real GPU call yields to the WebSocket reader between rows.
    await asyncio.sleep(0)
    return fn(*args)


class IdleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ws = Socket()
        self.engine = Engine()
        self.runtime = SimpleNamespace(selected=server.QWEN, get_model=lambda name: self.engine,
                                       owner=None, run=call_model)
        request = SimpleNamespace(headers={}, app={server.RUNTIME: self.runtime})
        ws_factory = patch.object(server.web, "WebSocketResponse", return_value=self.ws)
        ws_factory.start()
        self.addCleanup(ws_factory.stop)
        draft_factory = patch.object(server, "LiveInput", partial(server.LiveInput, settle_seconds=0.12))
        draft_factory.start()
        self.addCleanup(draft_factory.stop)
        self.handler = asyncio.create_task(server.websocket(request))
        self.last_row = {}
        await self.wait_for_state("ready")
        self.send("start", seed=42)
        await self.wait_for_state("waiting")

    async def asyncTearDown(self):
        if not self.handler.done():
            self.ws.requests.put_nowait(None)
        await asyncio.wait_for(self.handler, timeout=1)

    def send(self, kind, **fields):
        self.ws.requests.put_nowait({"type": kind, **fields})

    async def wait_for(self, predicate):
        async with asyncio.timeout(2):
            while True:
                message = await self.ws.responses.get()
                self.assertNotEqual(message["type"], "error", message)
                if message["type"] == "row":
                    self.last_row = message
                if predicate(message):
                    return message
                self.assertNotEqual(message.get("state"), "stopped", message)

    async def wait_for_state(self, state):
        return await self.wait_for(lambda msg: msg.get("state") == state)

    async def assert_parked(self):
        steps = len(self.engine.inputs)
        # Give the coroutine chances to run, including after duplicate input events.
        await asyncio.sleep(0.02)
        self.assertEqual(len(self.engine.inputs), steps)
        self.assertTrue(self.ws.responses.empty())

    async def test_park_stop_and_release_session(self):
        self.send("input", text="")
        self.send("input", text=" \n　")
        await self.assert_parked()
        self.assertEqual(self.engine.inputs, [])
        self.send("stop")
        await self.wait_for_state("stopped")
        self.assertIsNone(self.runtime.owner)

    async def test_disconnect_while_parked_after_answer(self):
        self.engine.outputs = iter(map(ord, "Hi!"))
        self.send("input", text="A")
        await self.wait_for_state("waiting")
        self.ws.requests.put_nowait(None)
        await asyncio.wait_for(self.handler, timeout=1)
        self.assertIsNone(self.runtime.owner)

    async def test_delayed_reply_and_short_pause_between_sentences(self):
        self.engine.outputs = itertools.chain(
            [0] * 10, map(ord, "Here is one."), [0] * 3, map(ord, " Here is another.")
        )
        self.send("input", text="A")
        await self.wait_for_state("waiting")
        self.assertEqual(self.last_row["output"], "Here is one. Here is another.")

    async def test_typing_partial_words_does_not_run_empty_gpu_steps(self):
        self.send("input", text="W")
        await self.wait_for_state("running")
        await asyncio.sleep(0.015)
        self.send("input", text="Wh")
        await asyncio.sleep(0.015)
        self.assertEqual(self.engine.inputs, [])
        self.send("input", text="What is ")
        await self.wait_for(lambda m: m.get("input") == "What is ")
        await asyncio.sleep(0.015)
        self.assertEqual(self.engine.inputs, list(map(ord, " What is")))
        self.send("input", text="What is it?")
        await self.wait_for_state("waiting")
        self.assertEqual(self.last_row["input"], "What is it?")
        self.assertEqual([i for i in self.engine.inputs if i is not None],
                         list(map(ord, " What is it?")))

    async def test_greeting_and_completed_answer_do_not_bias_output(self):
        self.send("input", text="안녕!")
        await self.wait_for_state("waiting")
        self.assertFalse(any(self.engine.onset_biases))
        previous_steps = len(self.engine.inputs)
        self.engine.outputs = itertools.chain([0] * 10, map(ord, "Four."))
        self.send("input", text="안녕! 2+2?")
        await self.wait_for_state("waiting")
        self.assertEqual(self.last_row["output"], "Four.")
        biases = self.engine.onset_biases[previous_steps:]
        self.assertTrue(any(b > 0 for b in biases[:11]))
        self.assertFalse(any(biases[11:]))  # First visible character has been produced.

    async def test_unanswered_question_only_gets_short_onset_window(self):
        self.send("input", text="왜？")
        await self.wait_for_state("waiting")
        biases = self.engine.onset_biases
        self.assertFalse(any(b for b, token in zip(biases, self.engine.inputs) if token is not None))
        silence = [b for b, token in zip(biases, self.engine.inputs) if token is None]
        self.assertFalse(any(silence[:4]))
        self.assertTrue(all(0 < b <= 5 for b in silence[4:16]))
        self.assertGreater(len(silence), 16)
        self.assertFalse(any(silence[16:]))

    async def test_followup_without_punctuation_reports_no_new_visible_answer(self):
        self.engine.outputs = itertools.chain(map(ord, "Hi!"), itertools.repeat(0))
        self.send("input", text="A")
        answered = await self.wait_for_state("waiting")
        self.assertEqual(answered["reason"], "answered")
        self.send("input", text="A 한국 수도 어디야")
        await self.wait_for(lambda m: m.get("phase") == "reading")
        awaiting = await self.wait_for(lambda m: m.get("phase") == "awaiting")
        self.assertEqual(awaiting["output"], "Hi!")
        waiting = await self.wait_for_state("waiting")
        self.assertEqual(waiting["reason"], "no_output")
        self.assertTrue(any(self.engine.onset_biases))
        self.assertTrue(server.requests_reply("what is two plus two"))

    async def test_answer_then_followup_without_new_session(self):
        self.engine.outputs = iter(map(ord, "Hi!"))
        self.send("input", text="A")
        await self.wait_for_state("waiting")
        self.assertEqual(self.last_row["output"].strip(), "Hi!")
        self.assertLessEqual(len(self.engine.inputs), 3 + 8)
        self.send("input", text="A")
        await self.assert_parked()
        self.engine.outputs = iter(map(ord, "Yes."))
        self.send("input", text="A B")
        await self.wait_for_state("waiting")
        self.assertEqual(self.last_row["input"], "A B")
        self.assertTrue(self.last_row["output"].endswith("Yes."))
        self.assertEqual(self.engine.sessions, 1)
        self.assertEqual([i for i in self.engine.inputs if i is not None], list(map(ord, " A B")))

    async def test_all_silent_and_incessant_outputs_both_park(self):
        self.send("input", text="A")
        await self.wait_for_state("waiting")
        self.assertLessEqual(len(self.engine.inputs), len(" A") + 24)
        self.assertEqual(self.last_row["output"], "")
        self.engine.outputs = itertools.cycle(map(ord, " What's on your mind? "))
        self.send("input", text="AB")
        await self.wait_for_state("waiting")
        self.assertIn("What's on your mind?", self.last_row["output"])
        self.assertLess(self.last_row["row"], server.MAX_ROWS)
        await self.assert_parked()

    async def test_mid_answer_input_uses_existing_session(self):
        self.engine.outputs = iter(map(ord, "This is a longer answer to allow an interruption."))
        self.send("input", text="A")
        await self.wait_for(lambda msg: msg["type"] == "row" and msg["output"].strip())
        self.send("input", text="A B")
        short_answer = self.last_row["output"]
        await self.wait_for(lambda m: m.get("type") == "row" and len(m["output"]) > len(short_answer) + 3)
        self.send("input", text="A B ")
        await self.wait_for_state("waiting")
        self.assertEqual(self.last_row["input"], "A B ")
        self.assertEqual([i for i in self.engine.inputs if i is not None], list(map(ord, " A B")))
        self.assertTrue(self.last_row["output"].endswith("interruption."))
        self.assertEqual(self.engine.sessions, 1)


if __name__ == "__main__":
    unittest.main()
