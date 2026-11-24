import asyncio
import unittest
from types import SimpleNamespace

from server import LiveInput, MINICPM_EOS_RETRIES, stream_minicpm
from test_idle import Socket, call_model


class MiniCPMInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_ending_parks_with_message_and_can_resume(self):
        draft = LiveInput(settle_seconds=0)
        draft.update("처음 질문.")
        ws, stopped = Socket(), asyncio.Event()
        calls = []

        def step(chunk):
            calls.append(chunk)
            if chunk == "다음 질문.":
                return SimpleNamespace(text="완료.", state="slice_end")
            return SimpleNamespace(text=" 계속", state="slice_end")

        engine = SimpleNamespace(new_session=lambda seed: SimpleNamespace(step=step))
        task = asyncio.create_task(stream_minicpm(ws, SimpleNamespace(run=call_model),
                                                   engine, draft, stopped, 42))
        try:
            async with asyncio.timeout(2):
                for expected in ("unfinished", "answered"):
                    while (event := await ws.responses.get()).get("state") != "waiting":
                        pass
                    self.assertEqual(event["reason"], expected)
                    if expected == "unfinished":
                        self.assertTrue(event.get("message"))
                        self.assertEqual(calls, ["처음 질문."] + [""] * MINICPM_EOS_RETRIES)
                        draft.update("처음 질문. 다음 질문.")
        finally:
            stopped.set()
            draft.changed.set()
            await asyncio.wait_for(task, 2)

    async def test_late_eos_continues_until_complete_or_native_stop(self):
        for ending, final in (("slice_end", "My name is Ada."), ("eos", "My name is Ada")):
            with self.subTest(ending=ending):
                calls = []
                draft = LiveInput(settle_seconds=0)
                draft.update("What's your name?")
                ws, stopped = Socket(), asyncio.Event()

                def step(chunk):
                    calls.append(chunk)
                    if len(calls) == 1:
                        return SimpleNamespace(text="My name", state="slice_end")
                    self.assertEqual(len(calls), 2)
                    return SimpleNamespace(text=final[len("My name"):], state=ending)

                engine = SimpleNamespace(new_session=lambda seed: SimpleNamespace(step=step))
                task = asyncio.create_task(stream_minicpm(ws, SimpleNamespace(run=call_model),
                                                           engine, draft, stopped, 42))
                try:
                    rows = []
                    async with asyncio.timeout(2):
                        while True:
                            event = await ws.responses.get()
                            if event.get("state") == "waiting":
                                break
                            if event["type"] == "row":
                                rows.append(event["output"])
                    self.assertEqual(calls, ["What's your name?", ""])
                    self.assertEqual(rows, ["My name", final])
                finally:
                    stopped.set()
                    draft.changed.set()
                    await asyncio.wait_for(task, 2)

    def test_chunk_boundaries_and_korean_edit_before_commit(self):
        clock = 0
        draft = LiveInput(clock=lambda: clock)
        draft.update("this input has four w")
        self.assertEqual(draft.next_slice(), "")
        draft.update("this input has four words 더")
        self.assertEqual(draft.next_slice(), "this input has four words")
        draft.acknowledge()
        draft.update("this input has four words 더 쓸개")
        self.assertEqual(draft.next_slice(), "")
        draft.update("this input has four words 더 쓸게.")
        self.assertEqual(draft.next_slice(), "더 쓸게.")
        self.assertEqual(draft.confirmed, "this input has four words ")
        draft.acknowledge()
        self.assertEqual(draft.confirmed, draft.text)
        draft.update(draft.text + " 한글")
        clock = 1
        self.assertEqual(draft.next_slice(), "한글")

    async def test_continuation_waits_for_the_next_word_being_typed(self):
        draft = LiveInput(clock=lambda: 0)
        draft.update("Hello.")
        ws, stopped = Socket(), asyncio.Event()
        calls = []

        def step(chunk):
            calls.append(chunk)
            if len(calls) == 1:
                draft.update("Hello. Wh")
                asyncio.get_running_loop().call_soon(draft.update, "Hello. What now?")
                return SimpleNamespace(text="My name", state="slice_end")
            stopped.set()
            return SimpleNamespace(text="Yes?", state="eos")

        engine = SimpleNamespace(new_session=lambda seed: SimpleNamespace(step=step))
        await asyncio.wait_for(stream_minicpm(ws, SimpleNamespace(run=call_model), engine,
                                              draft, stopped, 42), timeout=2)
        self.assertEqual(calls, ["Hello.", "What now?"])

    async def test_mid_answer_correction_and_idle_resume_same_session(self):
        draft = LiveInput(settle_seconds=0)
        ws = Socket()
        stop = asyncio.Event()
        calls = []
        sessions = []

        def step(text):
            calls.append(text)
            if len(calls) == 1:
                # Arrives through the reader while model work is underway.
                draft.update("처음에는 서울. 정정, 부산이야.")
                return SimpleNamespace(text="서울", state="writing")
            if len(calls) == 2:
                return SimpleNamespace(text="", state="idle")
            if len(calls) == 3:
                return SimpleNamespace(text="부", state="writing")
            if len(calls) == 4:
                return SimpleNamespace(text="산입니다.", state="eos")
            return SimpleNamespace(text="", state="idle")

        def new_session(seed):
            sessions.append(seed)
            return SimpleNamespace(step=step)

        engine = SimpleNamespace(new_session=new_session)
        draft.update("처음에는 서울.")
        task = asyncio.create_task(stream_minicpm(ws, SimpleNamespace(run=call_model),
                                                   engine, draft, stop, 44))
        async def rows_until_wait():
            rows = []
            async with asyncio.timeout(2):
                while True:
                    event = await ws.responses.get()
                    if event.get("state") == "waiting":
                        return rows
                    if event["type"] == "row":
                        rows.append(event)

        try:
            rows = await rows_until_wait()
            self.assertEqual(calls, ["처음에는 서울.", "정정, 부산이야.", "", ""])
            self.assertEqual(rows[0]["input"], "처음에는 서울.")
            self.assertEqual(rows[1]["input"], "처음에는 서울. 정정, 부산이야.")
            self.assertEqual(rows[1]["output"], "서울")
            self.assertEqual(rows[2]["output"], "서울\n부")
            self.assertEqual(rows[-1]["output"], "서울\n부산입니다.")
            draft.update(draft.text + " 계속해.")
            await rows_until_wait()
            self.assertEqual(sessions, [44])
            self.assertIn("계속해.", calls)
        finally:
            stop.set()
            draft.changed.set()
            await asyncio.wait_for(task, 2)
