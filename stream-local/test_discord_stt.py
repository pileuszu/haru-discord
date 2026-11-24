import asyncio
import unittest
import numpy as np

from discord_stt import DiscordSTT, FRAME_BYTES, MAX_PENDING_FINALS

VOICE = (np.sin(np.arange(960) / 7) * 2400).astype("<i2").tobytes()
SILENCE = bytes(FRAME_BYTES)


class FakeRecognizer:
    def __init__(self):
        self.calls = []
        self.audio = []
        self.closed = False
        self.block = asyncio.Event()
        self.block.set()

    async def start(self):
        pass

    async def transcribe(self, pcm):
        self.calls.append(len(pcm))
        self.audio.append(pcm)
        await self.block.wait()
        return str(len(pcm))

    async def close(self):
        self.closed = True


class StreamingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.asr = FakeRecognizer()
        async def on_event(event):
            self.events.append(event)
        self.stt = DiscordSTT(on_event, recognizer=self.asr)
        await self.stt.start()

    async def asyncTearDown(self):
        await self.stt.close()
        self.assertFalse(self.asr.closed)

    async def test_silence_and_one_short_noise_do_not_trigger(self):
        self.stt.feed("a", SILENCE*64 + VOICE + SILENCE*24)
        await asyncio.sleep(0)
        self.assertFalse(self.events)
        self.assertFalse(self.asr.calls)

    async def test_separate_speakers_and_overlap(self):
        self.stt.feed("a", VOICE*28 + SILENCE*6, overlap=True)
        await asyncio.sleep(0)
        self.stt.feed("b", VOICE*10 + SILENCE*16)
        self.stt.feed("a", VOICE*3 + SILENCE*16)
        await asyncio.sleep(0)
        self.assertEqual([(e.speaker, e.kind, e.overlap) for e in self.events],
                         [("a","partial",True), ("b","final",False), ("a","final",True)])
        self.assertEqual(self.events[0].turn_id, self.events[2].turn_id)
        self.assertNotEqual(self.events[0].turn_id, self.events[1].turn_id)

    async def test_input_stops_without_silence_packets(self):
        self.stt.feed("a", VOICE*10)
        await asyncio.sleep(.35)
        self.assertEqual([e.kind for e in self.events], ["partial", "final"])
        self.assertEqual(self.asr.audio, [VOICE*10 + SILENCE*4])

    async def test_decode_inside_pause_only_publishes_final_at_the_endpoint(self):
        self.stt.feed("a", VOICE*24)
        await asyncio.sleep(0)
        self.assertFalse(self.asr.calls)
        self.stt.feed("a", SILENCE*6)
        await asyncio.sleep(0)
        self.assertEqual([e.kind for e in self.events], ["partial"])
        self.stt.feed("a", SILENCE*10)
        await asyncio.sleep(0)
        self.assertEqual([e.kind for e in self.events], ["partial", "final"])
        self.assertEqual(self.asr.calls, [28*FRAME_BYTES])

    async def test_late_quiet_packets_replace_a_synthetic_tail(self):
        quiet = np.full(960, 20, dtype="<i2").tobytes()
        self.stt.feed("a", VOICE*10)
        await asyncio.sleep(.15)
        self.stt.feed("a", quiet*16)
        await asyncio.sleep(0)
        self.assertEqual(self.asr.audio, [VOICE*10 + SILENCE*4, VOICE*10 + quiet*4])
        self.assertEqual(self.events[-1].kind, "final")

    async def test_in_flight_candidate_can_finish_after_the_endpoint(self):
        self.asr.block.clear()
        self.stt.feed("a", VOICE*10 + SILENCE*6)
        await asyncio.sleep(0)
        self.stt.feed("a", SILENCE*10)
        self.asr.block.set()
        await asyncio.sleep(0)
        self.assertEqual([e.kind for e in self.events], ["final"])
        self.assertEqual(self.asr.calls, [14*FRAME_BYTES])

    async def test_queued_finals_run_before_other_speakers_candidates(self):
        self.asr.block.clear()
        self.stt.feed("a", VOICE*8 + SILENCE*6)
        await asyncio.sleep(0)
        self.stt.feed("b", VOICE*9 + SILENCE*6)
        self.stt.feed("c", VOICE*10 + SILENCE*16)
        self.asr.block.set()
        await asyncio.sleep(0)
        self.assertEqual([(e.speaker, e.kind) for e in self.events],
                         [("a", "partial"), ("c", "final"), ("b", "partial")])

    async def test_endpoint_padding_equal_with_explicit_or_missing_silence(self):
        self.stt.feed("a", VOICE*10)
        self.stt.end("a")
        self.stt.feed("b", VOICE*10 + SILENCE*16)
        await asyncio.sleep(0)
        self.assertEqual(self.asr.calls, [14*FRAME_BYTES, 14*FRAME_BYTES])

    async def test_delayed_partials_superseded_by_final(self):
        self.asr.block.clear()
        self.stt.feed("a", VOICE*24 + SILENCE*6)
        await asyncio.sleep(0)
        self.stt.feed("a", VOICE*64 + SILENCE*16)
        self.asr.block.set()
        await asyncio.sleep(0)
        self.assertEqual([e.kind for e in self.events], ["final"])
        self.assertEqual(len(self.asr.calls), 2)
        self.assertEqual(self.asr.audio[-1], VOICE*24 + SILENCE*6 + VOICE*64 + SILENCE*4)

    async def test_backlog_and_disconnection(self):
        self.asr.block.clear()
        for i in range(20):
            self.stt.feed(str(i), VOICE*6 + SILENCE*16)
        self.assertEqual(len(self.stt.finals), MAX_PENDING_FINALS)
        self.assertEqual(self.stt.dropped_turns, 20-MAX_PENDING_FINALS)
        await self.stt.close()
        self.assertEqual(self.events, [])

    async def test_failure_visible_to_idle_session(self):
        async def fail(pcm):
            raise OSError("test error")
        self.asr.transcribe = fail
        self.stt.feed("a", VOICE*10 + SILENCE*16)
        with self.assertRaisesRegex(OSError,"test error"):
            await asyncio.wait_for(self.stt.wait_failed(), 1)

    async def test_reject_malformed_frame(self):
        with self.assertRaises(ValueError):
            self.stt.feed("a", b"\0")

if __name__ == "__main__":
    unittest.main()
