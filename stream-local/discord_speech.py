"""Stream Gemma output rows through a session's Yuna worker to Discord PCM."""
import asyncio
from contextlib import suppress
import re

import numpy as np
import regex

from yuna_tts import SAMPLE_RATE, Yuna


# The browser uses the same boundaries and spoken forms in static/yuna.js.
MAX_PHRASE = 140
MAX_PENDING = 16_384
SEARCH_NOTICE = '잠깐만, 검색해 볼게.'
FRAME_SECONDS = 0.020
IN_SAMPLES = SAMPLE_RATE // 50
OUT_SAMPLES = 48_000 // 50
# Integer phase repeats exactly at each 20 ms boundary (441 -> 960 samples).
_INDEX, _REMAINDER = np.divmod(np.arange(OUT_SAMPLES) * IN_SAMPLES, OUT_SAMPLES)
_FRACTION = _REMAINDER / OUT_SAMPLES
_PAUSE = re.compile(r"[!?。！？…]|(?<!\d)\.(?=\s|$)|(?<=\d)\.(?=\s)|[,;:](?=\s)|\n")
_INTEGER = re.compile(r'''(^|[\s(“‘"'])(\*{1,2}|_{1,2})?([1-9][0-9]?)(?:\2)?(이야|야|이에요|예요|입니다)(?=$|[\s.,!?。！？…)"'”’*_])''')
_IDENTIFIER = re.compile(r"(?:코드|비밀번호|식별자|버전|번호|마이너스|minus|code|pin|version|id)(?:은|는|이|가)?\s*[:=]?\s*$|[-−±]\s*$", re.I)
_TENS = ('', '열', '스물', '서른', '마흔', '쉰', '예순', '일흔', '여든', '아흔')
_UNITS = ('', '하나', '둘', '셋', '넷', '다섯', '여섯', '일곱', '여덟', '아홉')


def spoken_text(raw):
    raw = re.sub(r'\[([^\]]+)\]\(https?://[^)]+\)', r'\1', raw)
    raw = re.sub(r'https?://\S+', '링크', raw)
    raw = re.sub(r'^\s*[-+#>]+\s+', '', raw, flags=re.M)
    raw = regex.sub(r'[*_`#\p{Extended_Pictographic}\uFE0F\u200D]', '', raw)
    return ' '.join(raw.split())


def spoken_integers(raw, previous):
    def replace(match):
        space, _, digits, ending = match.groups()
        before = previous + raw[:match.start()] + space
        if (not space and before and not re.search(r'''[\s(“‘"']$''', before)) or _IDENTIFIER.search(before):
            return match[0]
        number = int(digits)
        word = _TENS[number // 10] + _UNITS[number % 10]
        consonant = (ord(word[-1]) - 0xac00) % 28 != 0
        if ending in ('예요', '이에요'):
            ending = '이에요' if consonant else '예요'
        elif ending != '입니다':
            ending = '이야' if consonant else '야'
        return space + word + ending
    return _INTEGER.sub(replace, raw)


def phrase_length(raw, first, finished):
    probe = raw[:MAX_PHRASE]
    end = count = 0
    for word in re.finditer(r'\S+\s+', probe):
        end = word.end()
        count += 1
        if end >= 16 or count == 4:
            probe = probe[:end]
            break
    for pause in _PAUSE.finditer(probe):
        if any(char.isalnum() for char in spoken_text(probe[:pause.end()])):
            return pause.end()
    if finished and len(raw) == len(probe):
        return len(raw)
    if count:
        size = len(spoken_text(raw[:end]))
        if (first and (size >= 4 or count >= 2)) or size >= 14 or len(probe) < len(raw) or len(raw) >= MAX_PHRASE:
            return end
    elif len(raw) >= MAX_PHRASE:
        return MAX_PHRASE
    return 0


def discord_frame(source):
    """441 source samples and one lookahead, padded with silence on phrase end."""
    samples = np.frombuffer(source, dtype='<i2')
    interpolated = samples[_INDEX] * (1 - _FRACTION) + samples[_INDEX + 1] * _FRACTION
    return np.repeat(np.rint(interpolated).astype('<i2'), 2).tobytes()


class DiscordSpeech:
    def __init__(self, send_pcm, *, clear_pcm=None, voice=None):
        self.send_pcm = send_pcm
        self.clear_pcm = clear_pcm
        self.voice = voice if voice is not None else Yuna()
        self.cursor = 0
        self.pending = self.previous = ''
        self.first = True
        self.finished = self.stopped = self.closed = False
        self._task = None
        self._audible = self._discard = False
        self._notice_pcm = b''
        self._notice_queued = self._playing_notice = False
        self._next_frame = 0.0
        self._ended = asyncio.Event()
        self._failure = None

    @property
    def speaking(self):
        return not self.closed and (self._audible or asyncio.get_running_loop().time() < self._next_frame)

    async def warm(self):
        # Prepare this fixed phrase once at join so a real lookup can begin
        # playback immediately, even when the provider returns very quickly.
        pcm = bytearray()
        async def collect(chunk):
            pcm.extend(chunk)
        await self.voice.render(SEARCH_NOTICE, collect)
        self._notice_pcm = bytes(pcm)

    def announce_search(self):
        if not self.closed and not self.stopped:
            self._notice_queued = True
            self._pump()

    def row(self, row):
        """Apply a text row and report whether the receiver must clear queued PCM."""
        output = row.get('output')
        if self.closed or not isinstance(output, str):
            return
        action = row.get('speech_action')
        if not action and row.get('model_state') in ('turn', 'interleaved'):
            action = 'redirect'
        if action in ('stop', 'start', 'redirect'):
            self.stopped = action == 'stop'
            clear = self.stopped or (self._playing_notice and self._audible)
            self._discard = clear or self._discard or not self._audible
            self._notice_queued = False
            self.pending = self.previous = ''
            self.finished = False
            self.first = True
            self.cursor = len(output)
            if clear:
                self._audible = False
                self._next_frame = 0.0
                if self.clear_pcm:
                    self.clear_pcm()
            return clear
        if not self.stopped and len(output) > self.cursor:
            if len(output) - self.cursor + len(self.pending) > MAX_PENDING:
                self._fail(BufferError('Discord speech text backlog exceeded its limit'))
                return
            self.pending += output[self.cursor:]
            self.finished = False
        self.cursor = len(output)
        self._pump()

    def finish(self):
        self.finished = True
        self._pump()

    def _pump(self):
        if (self._notice_queued or self.pending) and not self.closed and not self.stopped and self._task is None:
            self._task = asyncio.create_task(self._run())

    async def _run(self):
        try:
            while (self._notice_queued or self.pending) and not self.closed and not self.stopped:
                if self._notice_queued:
                    self._notice_queued = False
                    self._playing_notice = True
                    self._audible = self._discard = False
                    try:
                        await self._play(SEARCH_NOTICE, self._notice_pcm)
                    finally:
                        self._playing_notice = self._audible = False
                    continue
                size = phrase_length(self.pending, self.first, self.finished)
                if not size:
                    break
                raw, self.pending = self.pending[:size], self.pending[size:]
                phrase = spoken_text(spoken_integers(raw, self.previous))
                self.previous = (self.previous + raw)[-48:]
                if any(char.isalnum() for char in phrase):
                    self.first = False
                    self._audible = self._discard = False
                    await self._play(phrase)
                    self._audible = False
        except Exception as exc:
            self._fail(exc)
        finally:
            self._task = None

    async def _play(self, phrase, cached_pcm=b''):
        # Keep native synthesis independent of Discord's real-time send clock.
        # Only the current short phrase is rendered ahead. A redirect can discard it.
        ready = asyncio.Event()
        pending_pcm = bytearray()

        async def append(pcm):
            if not self._discard:
                if len(pcm) % 2 or len(pending_pcm) + len(pcm) > 8 * 1024 * 1024:
                    raise ValueError('Invalid or oversized Yuna PCM for one phrase')
                pending_pcm.extend(pcm)
                ready.set()

        async def synthesize():
            try:
                if cached_pcm:
                    await append(cached_pcm)
                else:
                    await self.voice.render(phrase, append)
            finally:
                ready.set()

        renderer = asyncio.create_task(synthesize())
        try:
            block_bytes = IN_SAMPLES * 2
            window = block_bytes + 2
            while not self._discard:
                if len(pending_pcm) < window and not renderer.done():
                    ready.clear()
                    await ready.wait()
                    continue
                if not pending_pcm:
                    break
                source = pending_pcm[:window]
                del pending_pcm[:block_bytes]
                frame = discord_frame(source.ljust(window, b'\0'))
                loop = asyncio.get_running_loop()
                delay = self._next_frame - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                if self._discard:
                    break
                sent_at = loop.time()
                self._audible = True
                await self.send_pcm(frame)
                if not self._discard:
                    self._next_frame = max(self._next_frame, sent_at - .002) + FRAME_SECONDS
            await renderer
        finally:
            if not renderer.done():
                renderer.cancel()
                with suppress(asyncio.CancelledError):
                    await renderer

    def _fail(self, exc):
        self._failure = exc
        self._audible = False
        self.stopped = self._discard = True
        self.pending = ''
        self._ended.set()

    async def wait_failed(self):
        await self._ended.wait()
        if self._failure is not None:
            raise self._failure

    async def drain(self):
        while self._task is not None:
            await self._task
        if self._failure is not None:
            raise self._failure
        remaining = self._next_frame - asyncio.get_running_loop().time()
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def close(self):
        self.closed = self._discard = True
        self.pending = ''
        try:
            if self._task is not None:
                self._task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._task
            await self.voice.close()
        finally:
            self._ended.set()
