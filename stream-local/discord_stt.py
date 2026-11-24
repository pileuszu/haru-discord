"""Live Korean captions from per-speaker Discord mono S16LE frames, entirely local."""
import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import struct
import sys
from time import monotonic

import numpy as np


FRAME_BYTES = 1920  # 20 ms at 48 kHz, signed 16-bit mono
MAX_FRAMES = 800
MIN_VOICED = 5
SILENCE_FRAMES = 16
EARLY_FRAMES = 6
DECODER_TAIL = 4
MAX_SPEAKERS = 8
MAX_PENDING_FINALS = 8


@dataclass(frozen=True)
class Transcript:
    speaker: str
    turn_id: int
    kind: str
    text: str
    overlap: bool
    started_at: float | None = None
    last_voiced_at: float | None = None


class MlxRecognizer:
    """One optional shared, warmed subprocess; no cross-turn audio or transcript state."""
    def __init__(self, python=None, model=None):
        repo = Path(os.environ.get("HARU_REPO", Path.home() / "Repos/haru-discord"))
        self.python = Path(python or os.environ.get("HARU_STT_PYTHON", repo / ".venv-pipeline/bin/python"))
        snapshots = repo / ".runtime/huggingface/hub/models--mlx-community--Qwen3-ASR-0.6B-8bit/snapshots"
        self.model = Path(model or os.environ.get("HARU_STT_MODEL") or snapshots / "89e96d92ba34aca20b3e29fb10cc284097d1219f")
        self.proc = None
        self.lock = asyncio.Lock()

    async def start(self):
        async with self.lock:
            if self.proc and self.proc.returncode is None:
                return
            if not self.python.is_file() or not (self.model / "model.safetensors").is_file():
                raise FileNotFoundError("한국어 STT 실행 환경 또는 모델이 없습니다. HARU_STT_PYTHON 및 HARU_STT_MODEL을 확인하세요.")
            self.proc = await asyncio.create_subprocess_exec(
                str(self.python), "-u", str(Path(__file__).resolve()), "--worker", str(self.model),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
            )
            try:
                async with asyncio.timeout(60):
                    if await self.proc.stdout.readline() != b'READY\n':
                        raise RuntimeError("한국어 STT 작업자를 시작할 수 없습니다.")
            except BaseException:
                await self._stop()
                raise

    async def transcribe(self, pcm):
        async with self.lock:
            # Drain a cancelled caller's reply while holding the lock. A second
            # session can then reuse the warm worker and cannot receive stale text.
            work = asyncio.create_task(self._request(pcm))
            try:
                return await asyncio.shield(work)
            except asyncio.CancelledError:
                await asyncio.gather(work, return_exceptions=True)
                raise

    async def _request(self, pcm):
        try:
            async with asyncio.timeout(30):
                proc = self.proc
                if proc is None or proc.returncode is not None:
                    raise RuntimeError("한국어 STT 작업자가 종료됐습니다.")
                proc.stdin.write(struct.pack("<I", len(pcm)) + pcm)
                await proc.stdin.drain()
                line = await proc.stdout.readline()
                if not line:
                    raise RuntimeError("한국어 STT 작업자와의 연결이 끊어졌습니다.")
                return json.loads(line)
        except BaseException:
            await self._stop()
            raise

    async def close(self):
        async with self.lock:
            await self._stop(graceful=True)

    async def _stop(self, *, graceful=False):
        proc, self.proc = self.proc, None
        if proc is not None:
            if graceful and proc.returncode is None:
                proc.stdin.close()
                try:
                    await asyncio.wait_for(proc.wait(), 3)
                except TimeoutError:
                    pass
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.communicate()


@dataclass
class _Turn:
    speaker: str
    seq: int
    started_at: float = field(default_factory=monotonic)
    last_voiced_at: float | None = None
    frames: bytearray = field(default_factory=bytearray)
    trailing: int = 0
    voiced: int = 0
    overlap: bool = False
    last_text: str = ""
    finished: bool = False
    timer: asyncio.TimerHandle | None = None
    early_timer: asyncio.TimerHandle | None = None
    early_requested: bool = False
    early_result: tuple[bytes, str] | None = None

    def audio(self):
        # The decoder sees 80 ms after the last voiced frame, including any real
        # quiet samples. Discord DTX may suppress some or all of these packets.
        end = len(self.frames) - max(0, self.trailing - DECODER_TAIL) * FRAME_BYTES
        pad = max(0, DECODER_TAIL - self.trailing) * FRAME_BYTES
        return bytes(memoryview(self.frames)[:end]) + bytes(pad)

    def cancel_timers(self):
        if self.timer:
            self.timer.cancel()
        if self.early_timer:
            self.early_timer.cancel()


class DiscordSTT:
    """One voice session. feed/end are non-blocking and must run on its event loop."""
    def __init__(self, on_event, *, recognizer=None):
        self.on_event = on_event
        self.recognizer = recognizer if recognizer is not None else MlxRecognizer()
        self.owns_recognizer = recognizer is None
        self.active = {}
        self.preroll = OrderedDict()
        self.candidates = OrderedDict()
        self.finals = deque()
        self.wake = asyncio.Event()
        self.runner = None
        self.closed = False
        self.seq = 0
        self.dropped_turns = 0
        self.error = None
        self.failed = asyncio.Event()

    async def start(self):
        if self.closed:
            raise RuntimeError("STT session closed")
        await self.recognizer.start()
        if self.runner is None:
            self.runner = asyncio.create_task(self._run())

    def _check(self):
        if self.error:
            raise self.error
        if self.closed or self.runner is None:
            raise RuntimeError("STT session not running")

    async def wait_failed(self):
        await self.failed.wait()
        raise self.error

    def feed(self, speaker: str, pcm: bytes, overlap=False):
        self._check()
        if len(pcm) % FRAME_BYTES:
            raise ValueError("expected complete 20 ms mono S16LE 48 kHz frames")
        for pos in range(0, len(pcm), FRAME_BYTES):
            data = pcm[pos:pos + FRAME_BYTES]
            samples = np.frombuffer(data, dtype="<i2").astype(np.float32)
            voiced = float(np.dot(samples, samples)) >= len(samples) * 140**2
            turn = self.active.get(speaker)
            if turn is None:
                if not voiced:
                    if speaker not in self.preroll:
                        if len(self.preroll) >= MAX_SPEAKERS:
                            self.preroll.popitem(last=False)
                        self.preroll[speaker] = deque(maxlen=4)
                    self.preroll[speaker].append(data)
                    self.preroll.move_to_end(speaker)
                    continue
                if len(self.active) >= MAX_SPEAKERS:
                    continue
                self.seq += 1
                turn = self.active[speaker] = _Turn(speaker, self.seq)
                turn.frames.extend(b"".join(self.preroll.pop(speaker, ())))
            turn.frames.extend(data)
            if voiced:
                turn.last_voiced_at = monotonic()
                if turn.early_requested:
                    self.candidates.pop(turn.seq, None)
                    turn.early_requested = False
                    turn.early_result = None
            turn.trailing = 0 if voiced else turn.trailing + 1
            turn.voiced += int(voiced)
            turn.overlap |= bool(voiced and overlap)
            size = len(turn.frames) // FRAME_BYTES
            if turn.trailing >= SILENCE_FRAMES or size >= MAX_FRAMES:
                self.end(speaker)
        turn = self.active.get(speaker)
        if turn:
            turn.cancel_timers()
            loop = asyncio.get_running_loop()
            remaining = SILENCE_FRAMES - turn.trailing
            turn.timer = loop.call_later(remaining * .02, self.end, speaker)
            if turn.voiced >= MIN_VOICED and not turn.early_requested:
                wait_frames = EARLY_FRAMES - turn.trailing
                if wait_frames <= 0:
                    self._queue_candidate(turn)
                else:
                    turn.early_timer = loop.call_later(wait_frames * .02, self._queue_candidate, turn)

    def _queue_candidate(self, turn):
        turn.early_requested = True
        self.candidates[turn.seq] = turn
        self.wake.set()

    def end(self, speaker: str):
        self._check()
        turn = self.active.pop(speaker, None)
        self.preroll.pop(speaker, None)
        if not turn:
            return
        turn.cancel_timers()
        turn.finished = True
        self.candidates.pop(turn.seq, None)
        if turn.voiced < MIN_VOICED:
            return
        if len(self.finals) >= MAX_PENDING_FINALS:
            self.finals.popleft()
            self.dropped_turns += 1
        self.finals.append(turn)
        self.wake.set()

    async def _run(self):
        try:
            while True:
                if not self.finals and not self.candidates:
                    self.wake.clear()
                    await self.wake.wait()
                if self.finals:
                    turn, kind = self.finals.popleft(), "final"
                    pcm = turn.audio()
                    if turn.early_result is not None and pcm == turn.early_result[0]:
                        text = turn.early_result[1]
                    else:
                        text = await self.recognizer.transcribe(pcm)
                elif self.candidates:
                    _, turn = self.candidates.popitem(last=False)
                    kind = "partial"
                    voiced = turn.voiced
                    pcm = turn.audio()
                    text = await self.recognizer.transcribe(pcm)
                    # feed() may have received a correction while the worker was busy.
                    if turn.voiced != voiced:
                        continue
                    turn.early_result = (pcm, text)
                    if turn.finished or text == turn.last_text:
                        continue
                else:
                    continue
                if self.closed:
                    continue
                turn.last_text = text
                await self.on_event(Transcript(turn.speaker, turn.seq, kind, text, turn.overlap,
                                               turn.started_at, turn.last_voiced_at))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc
            for turn in self.active.values():
                turn.cancel_timers()
            self.failed.set()

    async def close(self):
        self.closed = True
        for turn in self.active.values():
            turn.cancel_timers()
        self.active.clear()
        self.preroll.clear()
        self.candidates.clear()
        self.finals.clear()
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        if self.owns_recognizer:
            await self.recognizer.close()


def worker(model_path):
    import contextlib
    import re
    # Some model loaders print progress to stdout; the protocol owns that fd.
    output = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        import mlx.core as mx
        from mlx_audio.stt import load
        from scipy.signal import resample_poly
        mx.set_cache_limit(256 * 1024 * 1024)
        model = load(model_path)

        def decode(waveform, limit):
            result = model.generate(waveform, language="Korean", temperature=0.0,
                                    max_tokens=limit, verbose=False)
            text = result.text.rsplit("<asr_text>", 1)[-1]
            text = re.sub(r"<\|[^|]*\|>", "", text).strip()
            return text if any(c.isalnum() for c in text) and text.lower() not in {
                "[silence]", "[blank_audio]", "[no speech]", "[music]", "(무음)", "[무음]", "[음악]",
            } else ""

        decode(np.zeros(16000, dtype=np.float32), 1)
        print("READY", file=output, flush=True)
        while prefix := sys.stdin.buffer.read(4):
            length, = struct.unpack("<I", prefix)
            if not 0 < length <= FRAME_BYTES * (MAX_FRAMES + 4) or length % FRAME_BYTES:
                raise ValueError("invalid PCM length")
            raw = sys.stdin.buffer.read(length)
            if len(raw) != length:
                return
            pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
            waveform = resample_poly(pcm, 1, 3)
            # Bound runaways on truncated speech and leave room for fast Korean.
            limit = min(256, 12 + int(16 * len(waveform) / 16000))
            print(json.dumps(decode(waveform, limit), ensure_ascii=False), file=output, flush=True)


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--worker":
    worker(sys.argv[2])
