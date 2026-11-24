import asyncio
import unittest
from unittest.mock import AsyncMock

import numpy as np

from discord_speech import DiscordSpeech, IN_SAMPLES, MAX_PENDING, SEARCH_NOTICE


class Voice:
    def __init__(self, samples=None):
        self.samples = samples if samples is not None else np.arange(1200, dtype='<i2')
        self.phrases = []
        self.closed = False

    async def render(self, phrase=None, write=None):
        if phrase:
            self.phrases.append(phrase)
            raw = self.samples.astype('<i2').tobytes()
            # Split inside a 20 ms frame, including a short first delivery.
            for i in range(0, len(raw), 94):
                await write(raw[i:i + 94])

    async def close(self):
        self.closed = True


class DiscordSpeechTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.voice = Voice()
        self.frames = []
        self.first_frame = asyncio.Event()
        self.clears = []

        async def send(frame):
            self.frames.append((asyncio.get_running_loop().time(), frame))
            self.first_frame.set()

        self.speech = DiscordSpeech(send, clear_pcm=lambda: self.clears.append(True), voice=self.voice)
        self.addAsyncCleanup(self.speech.close)

    async def test_first_phrase_before_eos_split_cardinal_and_identifiers(self):
        self.speech.row({'output': '7 더하기 5는 **1'})
        await asyncio.wait_for(self.first_frame.wait(), 1)
        self.assertEqual(self.voice.phrases, ['7 더하기 5는'])
        self.speech.row({'output': '7 더하기 5는 **12**입니다. 코드가 12야. 0.25입니다.'})
        self.speech.finish()
        await self.speech.drain()
        self.assertEqual(self.voice.phrases, ['7 더하기 5는', '열둘입니다.', '코드가 12야.', '0.25입니다.'])

    async def test_pcm_is_exact_stereo_48khz_paced_and_tail_padded(self):
        self.speech.row({'output': '열둘이에요.'})
        self.speech.finish()
        await self.speech.drain()
        self.assertEqual([len(data) for _, data in self.frames], [3840] * 3)
        times = [time for time, _ in self.frames]
        self.assertGreaterEqual(min(np.diff(times)), 0.015)
        out = np.frombuffer(b''.join(data for _, data in self.frames), dtype='<i2').reshape(-1, 2)
        np.testing.assert_array_equal(out[:, 0], out[:, 1])
        self.assertEqual(out[960, 0], IN_SAMPLES)
        self.assertEqual(out[1920, 0], IN_SAMPLES * 2)
        self.assertEqual(out[-1, 0], 0)
        self.assertFalse(self.speech.speaking)

    async def test_prepared_notice_is_played_before_even_an_immediate_result(self):
        self.voice.samples = np.full(IN_SAMPLES * 2, 77, dtype='<i2')
        await self.speech.warm()
        self.assertFalse(self.frames)
        self.voice.samples.fill(99)
        self.speech.announce_search()
        self.speech.row({'output': '오늘은 맑아.'})
        self.speech.finish()
        await self.speech.drain()
        self.assertEqual(self.voice.phrases, [SEARCH_NOTICE, '오늘은 맑아.'])
        self.assertEqual([int.from_bytes(data[:2], 'little') for _, data in self.frames], [77, 77, 99, 99])

    async def test_redirect_clears_a_wait_notice_mid_playback(self):
        self.speech.announce_search()
        await asyncio.wait_for(self.first_frame.wait(), 1)
        self.assertTrue(self.speech.row({'output': '', 'speech_action': 'redirect'}))
        self.speech.row({'output': '열둘이야.'})
        self.speech.finish()
        await self.speech.drain()
        self.assertEqual(self.clears, [True])
        self.assertEqual(self.voice.phrases, [SEARCH_NOTICE, '열둘이야.'])
        self.assertEqual(len(self.frames), 4)

    async def test_backchannel_and_redirect_keep_only_the_audible_phrase(self):
        old = '서울에서 출발해. 나중에 읽을 내용이야.'
        self.speech.row({'output': old})
        await asyncio.wait_for(self.first_frame.wait(), 1)
        self.speech.row({'speech_action': 'continue', 'output': old})
        self.assertTrue(self.speech.speaking)
        self.speech.row({'model_state': 'interleaved', 'output': old})
        self.speech.row({'output': old + '\n12예요.'})
        self.speech.finish()
        await self.speech.drain()
        self.assertEqual(self.voice.phrases, ['서울에서 출발해.', '열둘이에요.'])
        self.assertEqual(len(self.frames), 6)
        self.assertFalse(self.clears)

    async def test_stop_suppresses_later_pcm_and_new_turn_recovers(self):
        self.speech.row({'output': '이전 문장.'})
        await asyncio.wait_for(self.first_frame.wait(), 1)
        self.speech.row({'speech_action': 'stop', 'output': '이전 문장.'})
        self.speech.row({'output': '이전 문장. 안 돼.'})
        await self.speech.drain()
        self.assertEqual(self.clears, [True])
        self.assertEqual(len(self.frames), 1)
        self.speech.row({'model_state': 'turn', 'output': '이전 문장. 안 돼.'})
        self.speech.row({'output': '이전 문장. 안 돼.\n3이야.'})
        self.speech.finish()
        await self.speech.drain()
        self.assertEqual(self.voice.phrases, ['이전 문장.', '셋이야.'])

    async def test_redirect_before_first_packet_discards_synthesis(self):
        started, release = asyncio.Event(), asyncio.Event()
        render = self.voice.render

        async def delay_first(phrase, write):
            if not started.is_set():
                started.set()
                await release.wait()
            await render(phrase, write)

        self.voice.render = delay_first
        self.speech.row({'output': '이전 답변.'})
        await asyncio.wait_for(started.wait(), 1)
        self.speech.row({'output': '이전 답변.', 'speech_action': 'redirect'})
        self.speech.row({'output': '이전 답변.\n새 답변.'})
        self.speech.finish()
        release.set()
        await self.speech.drain()
        self.assertEqual(self.voice.phrases, ['이전 답변.', '새 답변.'])
        self.assertEqual(len(self.frames), 3)

    async def test_acknowledgement_keeps_unfinished_word_and_last_exact_frame(self):
        self.voice.samples = np.arange(IN_SAMPLES, dtype='<i2')
        start = '서울에서 출발해. 다음'
        self.speech.row({'output': start})
        await asyncio.wait_for(self.first_frame.wait(), 1)
        self.speech.row({'output': start, 'speech_action': 'continue'})
        self.speech.row({'output': start + '은 부산이야.'})
        self.speech.finish()
        await self.speech.drain()
        self.assertEqual(self.voice.phrases, ['서울에서 출발해.', '다음은 부산이야.'])
        self.assertEqual(len(self.frames), 2)

    async def test_errors_surface_to_bridge_and_queue_is_bounded(self):
        self.voice.render = AsyncMock(side_effect=OSError('missing voice'))
        self.speech.row({'output': '읽어.'})
        with self.assertRaisesRegex(OSError, 'missing voice'):
            await asyncio.wait_for(self.speech.wait_failed(), 1)
        bounded = DiscordSpeech(AsyncMock(), voice=Voice())
        bounded.row({'output': '가' * (MAX_PENDING + 1)})
        with self.assertRaises(BufferError):
            await bounded.wait_failed()
        await bounded.close()

    async def test_close_while_synthesis_waits_cancels_worker(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def render(phrase, write):
            try:
                started.set()
                await asyncio.Future()
            finally:
                cancelled.set()

        self.voice.render = render
        self.speech.row({'output': '시작.'})
        await asyncio.wait_for(started.wait(), 1)
        await self.speech.close()
        self.assertTrue(cancelled.is_set())
        self.assertTrue(self.voice.closed)
        await self.speech.wait_failed()
