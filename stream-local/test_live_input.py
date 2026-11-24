import asyncio
import unittest
from types import SimpleNamespace

from server import LiveInput, QUIET_ROWS_AFTER_OUTPUT, stream
from test_idle import Engine, Socket


class LiveInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_space_arriving_on_last_active_step_is_consumed(self):
        ws = Socket()
        engine = Engine()
        # This mock tokenizer uses one token per code point, including the
        # training format's initial virtual space.
        engine.outputs = iter([0, ord("Y")])
        draft = LiveInput(settle_seconds=0)
        draft.update("안")
        stopped = asyncio.Event()

        async def run(fn, *args):
            result = fn(*args)
            if len(engine.inputs) == len(" 안") + QUIET_ROWS_AFTER_OUTPUT:
                draft.update("안 ")
            return result

        task = asyncio.create_task(stream(ws, SimpleNamespace(run=run, owner=ws),
                                          engine, draft, stopped, 42))
        try:
            async with asyncio.timeout(2):
                while (await ws.responses.get()).get("state") != "waiting":
                    pass
            self.assertEqual(draft.confirmed, "안 ")
            self.assertEqual([token for token in engine.inputs if token is not None],
                             [ord(" "), ord("안")])
            # A separate space-only edit after parking waits for the next text.
            draft.update("안  ")
            await asyncio.sleep(0)
            self.assertEqual(draft.confirmed, "안 ")
            self.assertFalse(draft.has_words_pending())
            self.assertTrue(draft.has_pending())
        finally:
            stopped.set()
            draft.changed.set()
            await asyncio.wait_for(task, 2)
