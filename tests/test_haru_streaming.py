"""Check the handoff between the MLX worker and socket writer without loading a model."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from discord_native import stream_step


class Worker:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1)

    def submit(self, fn, *args):
        return self.executor.submit(fn, *args)

    async def run(self, fn, *args):
        return await asyncio.wrap_future(self.submit(fn, *args))


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.worker = Worker()
        self.release_worker = threading.Event()
        self.addCleanup(self.worker.executor.shutdown)

    async def asyncTearDown(self):
        self.release_worker.set()

    async def test_sends_audio_before_token_and_waits_for_socket_drain(self):
        received_audio = asyncio.Event()
        allow_drain = asyncio.Event()
        token_ready = asyncio.Event()
        loop = asyncio.get_running_loop()
        events = []
        pcm = np.array([0.25, -0.25], dtype=np.float32)
        frame = SimpleNamespace(index=4, audio=pcm, seconds=.085, text="하루", token=123, done=False)

        def step(publish):
            publish(frame.index, pcm, .075)
            if not self.release_worker.wait(2):
                raise TimeoutError("The socket did not receive audio while the worker waited")
            loop.call_soon_threadsafe(token_ready.set)
            return frame

        async def send_audio(index, audio, seconds):
            self.assertEqual(index, frame.index)
            np.testing.assert_array_equal(audio, pcm)
            self.assertAlmostEqual(seconds, .075)
            events.append("audio")
            received_audio.set()
            await allow_drain.wait()
            events.append("drained")

        async def send_complete(result):
            self.assertIs(result, frame)
            events.append("complete")

        task = asyncio.create_task(stream_step(self.worker, SimpleNamespace(step_streaming=step), send_audio, send_complete))
        try:
            await asyncio.wait_for(received_audio.wait(), 2)
            self.assertEqual(events, ["audio"])
            self.release_worker.set()
            await asyncio.wait_for(token_ready.wait(), 2)
            self.assertFalse(task.done())
            self.assertEqual(events, ["audio"])
            allow_drain.set()
            await asyncio.wait_for(task, 2)
            self.assertEqual(events, ["audio", "drained", "complete"])
        finally:
            self.release_worker.set()
            allow_drain.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_worker_error_terminates_before_or_after_audio(self):
        pcm = np.zeros(2, dtype=np.float32)
        for published in (False, True):
            with self.subTest(published=published):
                self.release_worker.clear()
                events = []

                def step(publish):
                    if published:
                        publish(3, pcm, .075)
                        self.release_worker.wait(2)
                    raise RuntimeError("model step failed")

                async def send_audio(*args):
                    events.append("audio")
                    self.release_worker.set()

                async def send_complete(frame):
                    self.fail("a failed step cannot send a completion")

                with self.assertRaisesRegex(RuntimeError, "model step failed"):
                    await asyncio.wait_for(stream_step(self.worker, SimpleNamespace(step_streaming=step), send_audio, send_complete), 3)
                self.assertEqual(events, ["audio"] if published else [])

    async def test_cancellation_drops_worker_publication_after_disconnect(self):
        worker_started = asyncio.Event()
        loop = asyncio.get_running_loop()
        seen = []
        errors = []
        old_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: errors.append(context))
        self.addCleanup(loop.set_exception_handler, old_handler)

        def step(publish):
            loop.call_soon_threadsafe(worker_started.set)
            self.release_worker.wait(2)
            pcm = np.zeros(2, dtype=np.float32)
            publish(7, pcm, .075)
            return SimpleNamespace(index=7, audio=pcm, seconds=.085, text="", token=0, done=False)

        async def send(*args):
            seen.append(args)

        task = asyncio.create_task(stream_step(self.worker, SimpleNamespace(step_streaming=step), send, send))
        await asyncio.wait_for(worker_started.wait(), 2)
        task.cancel()
        self.release_worker.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        # Drain the single worker and the late callback posted to the event loop.
        await self.worker.run(lambda: None)
        await asyncio.sleep(0)
        self.assertEqual(seen, [])
        self.assertEqual(errors, [])
