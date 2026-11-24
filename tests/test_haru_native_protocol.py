"""Exercise the Discord server's wire negotiation and prepared-frame reuse without Metal."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest

import numpy as np
from haru_mlx import native
from haru_mlx.frames import Frame

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from discord_native import DiscordNativeServer, STEP


class Session:
    config = SimpleNamespace(max_frames=2)
    prompt_length = 4

    def __init__(self):
        self.blocks = []

    def start(self):
        return Frame(np.ones(2, dtype=np.float32), 10, "첫", 0, 0.08)

    def feed(self, pcm):
        self.blocks.extend(pcm)

    def step(self):
        assert self.blocks == [0.5, 0.5]
        return Frame(np.zeros(2, dtype=np.float32), 11, "끝", 1, 0.09, True)

    def start_streaming(self, publish):
        frame = self.start()
        publish(0, frame.audio, 0.071)
        return frame

    def step_streaming(self, publish):
        frame = self.step()
        publish(1, frame.audio, 0.073)
        return frame


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def test_explicit_empty_prompt_preserves_voice_and_skips_native_default(self):
        server = object.__new__(DiscordNativeServer)
        server.default_card = "기본 캐릭터"
        server.default_voice = np.ones(3200, dtype=np.float32)
        server.speak_first, server.seed = False, 7
        self.assertEqual(server._settings({}).system_prompt, "기본 캐릭터")
        current = server._settings({"system_prompt": "", "guidance_scale": 1.5})
        self.assertEqual(current.system_prompt, "")
        self.assertIs(current.reference, server.default_voice)
        self.assertEqual(current.seed, 7)
        self.assertFalse(current.speak_first)

    async def test_stock_audio_first_and_cached_prepare(self):
        for early, prepare in ((False, False), (True, False), (True, True)):
            with self.subTest(early=early, prepare=prepare):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    async def run(fn, *args):
                        return await asyncio.wrap_future(pool.submit(fn, *args))

                    made = []
                    def session(settings):
                        made.append(settings)
                        return Session()

                    srv = object.__new__(DiscordNativeServer)
                    srv.backend = SimpleNamespace(
                        worker=SimpleNamespace(run=run, expect=lambda due: None),
                        model=SimpleNamespace(config=SimpleNamespace(frame_samples=2, sample_rate=24000)),
                        model_path="test-model", quantize=8, session=session,
                    )
                    srv.language, srv.assets, srv.step_seconds = "ko", {}, .083
                    srv._settings = lambda request: request
                    reader = asyncio.StreamReader()
                    packets = []
                    def request(kind, payload):
                        reader.feed_data(struct.pack("<BI", kind, len(payload)) + payload)

                    def write(data):
                        kind, n = struct.unpack("<BI", data[:5])
                        payload = data[5:]
                        self.assertEqual(n, len(payload))
                        packets.append((kind, payload))
                        start = json.dumps(dict(audio_first=True) if early else {}).encode()
                        if kind == native.ASSETS:
                            if prepare:
                                request(native.PREPARE, b"{}")
                            else:
                                request(native.START, start)
                        elif kind == native.READY:
                            request(native.START, start)
                        elif kind == native.HELLO:
                            self.assertEqual(json.loads(payload).get("audio_first", False), early)
                        elif kind == native.FRAME and struct.unpack_from("<I", payload)[0] == 0:
                            request(native.AUDIO, struct.pack("<4f", 0.5, 0.5, 0.5, 0.5))
                        elif kind == native.TEXT and payload.decode() == "끝":
                            reader.feed_eof()

                    writer = SimpleNamespace(write=write, drain=lambda: asyncio.sleep(0))
                    with self.assertRaises(asyncio.IncompleteReadError):
                        await asyncio.wait_for(srv._serve(reader, writer), 2)
                    self.assertEqual(len(made), 1)
                    frame_types = [native.FRAME, STEP, native.TEXT] if early else [native.FRAME, native.TEXT]
                    expected = [native.ASSETS] + ([native.READY] if prepare else []) + [native.HELLO] + frame_types * 2
                    self.assertEqual([k for k, _ in packets], expected)
                    frames = [struct.unpack_from("<If", p) for k, p in packets if k == native.FRAME]
                    self.assertEqual(frames, [(0, 71.0 if early and not prepare else 80.0),
                                              (1, 73.0 if early else 90.0)])
                    if early:
                        self.assertEqual([struct.unpack("<If", p) for k, p in packets if k == STEP],
                                         [(0, 80.0), (1, 90.0)])
