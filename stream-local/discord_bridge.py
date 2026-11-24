"""Serve a local Discord voice session with Korean STT, Gemma and Yuna."""
import argparse
import asyncio
import base64
from collections import deque
from contextlib import AsyncExitStack
import json
import logging
import os
from pathlib import Path
import signal

from aiohttp import WSMsgType, web

from discord_wake import WakeGate
from discord_speech import DiscordSpeech
from discord_spoken_math import normalize_math_question
from discord_stt import DiscordSTT, MAX_SPEAKERS, MlxRecognizer
from gemma_chat import conversation, local_backend
from server import LiveInput


LOG = logging.getLogger("discord-bridge")


class VoiceInput(LiveInput):
    """A room's finalized utterances and their session-local speaker numbers."""

    def __init__(self):
        super().__init__(settle_seconds=0)
        self._numbers: dict[str, int] = {}
        self._pending_speakers: deque[tuple[int, bool]] = deque()

    def append_utterance(self, speaker, text, overlap):
        # Keep exactly one line per STT turn, even if a recognizer inserts newlines.
        text = normalize_math_question(" ".join(text.splitlines()).strip())
        number = self._numbers.setdefault(speaker, len(self._numbers) + 1)
        self._pending_speakers.append((number, overlap))
        self.update(self.text + text + "\n", speech_overlap=self._pending_speakers[0][1])

    @property
    def speaker_number(self):
        return self._pending_speakers[0][0]

    def acknowledge(self):
        super().acknowledge()
        self._pending_speakers.popleft()
        self.speech_overlap = bool(self._pending_speakers and self._pending_speakers[0][1])


class VoiceTurn:
    def __init__(self, socket):
        self.socket = socket
        self.draft = VoiceInput()
        self.speech = DiscordSpeech(socket.send_bytes)
        self.generating = False
        self.output_length = 0
        self.wake = WakeGate(MAX_SPEAKERS)
        self.chat_wake = WakeGate(512)

    @property
    def closed(self):
        return self.socket.closed

    async def heard(self, event):
        state = "stt_" + event.kind
        if event.kind == "final":
            text = self.wake.address(event.speaker, event.text, started_at=event.started_at,
                                     ended_at=event.last_voiced_at)
            if text:
                self.draft.append_utterance(event.speaker, text, event.overlap or self.speech.speaking)
            else:
                state = "wake_waiting"
        await self.socket.send_json({"type": "status", "state": state,
                                     "chars": len(event.text), "overlap": event.overlap})

    async def typed(self, data):
        channel, speaker, raw = (data.get(key) for key in ("channel", "speaker", "text"))
        if (not all(isinstance(key, str) and 1 <= len(key) <= 64 for key in (channel, speaker))
                or not isinstance(raw, str) or not 1 <= len(raw) <= 4000):
            return
        # Each text channel has its own follow-up window. A microphone wake uses
        # just the speaker ID, but both share that same person's conversation.
        text = self.chat_wake.address((channel, speaker), raw)
        if text:
            text = text.replace("<", "＜")
            self.draft.append_utterance(speaker, text, self.speech.speaking or data.get("overlap") is True)
            await self.socket.send_json({"type": "status", "state": "chat_addressed"})

    async def send_json(self, event):
        if event["type"] == "row":
            clear = self.speech.row(event)
            action = event.get("speech_action")
            if clear:
                await self.socket.send_json({"type": "clear"})
            if action == "stop":
                self.generating = False
            if action:
                await self.socket.send_json({"type": "status", "state": "turn_" + action})
            if event["phase"] == "writing" and len(event["output"]) > self.output_length:
                if not self.generating:
                    await self.socket.send_json({"type": "status", "state": "reply_started"})
                self.generating = True
            self.output_length = len(event["output"])
            if action == "redirect":
                self.generating = False
        elif event.get("state") in ("searching", "search_finished"):
            if event["state"] == "searching":
                self.speech.announce_search()
            await self.socket.send_json(event)
        elif event.get("state") == "waiting":
            self.speech.finish()
            self.generating = False
            await self.socket.send_json({"type": "status", "state": "waiting"})

    async def receive(self, stt):
        async for message in self.socket:
            if message.type == WSMsgType.ERROR:
                raise self.socket.exception() or ConnectionError("음성 소켓 오류")
            if message.type != WSMsgType.TEXT:
                raise ValueError("음성 입력은 JSON 메시지여야 합니다.")
            data = json.loads(message.data)
            if data.get("type") == "text":
                await self.typed(data)
                continue
            speaker = data.get("speaker")
            if not isinstance(speaker, str) or not 1 <= len(speaker) <= 64:
                raise ValueError("잘못된 화자 ID입니다.")
            if data.get("type") == "end":
                stt.end(speaker)
            elif data.get("type") == "pcm":
                pcm = base64.b64decode(data["audio"], validate=True)
                if not pcm or len(pcm) % 1920 or len(pcm) > 19200:
                    raise ValueError("48kHz 모노 20ms 단위 PCM이 필요합니다.")
                stt.feed(speaker, pcm, overlap=self.speech.speaking or data.get("overlap") is True)
            else:
                raise ValueError("알 수 없는 음성 메시지입니다.")


class Bridge:
    def __init__(self, backend, recognizer):
        self.backend = backend
        self.recognizer = recognizer
        self.active = None

    async def voice(self, request):
        if self.active is not None:
            raise web.HTTPConflict(text="이미 연결된 음성 세션이 있습니다.")
        ws = web.WebSocketResponse(max_msg_size=32768, heartbeat=30)
        self.active = ws
        tasks = []
        turn = VoiceTurn(ws)
        stt = DiscordSTT(turn.heard, recognizer=self.recognizer)
        try:
            await ws.prepare(request)
            await asyncio.gather(stt.start(), turn.speech.warm())
            await ws.send_json({"type": "ready"})
            tasks = [asyncio.create_task(coro) for coro in (
                turn.receive(stt), conversation(turn, turn.draft, 42, self.backend, discord_voice=True),
                stt.wait_failed(), turn.speech.wait_failed())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception:
            LOG.exception("음성 세션 오류")
            if ws.prepared and not ws.closed:
                await ws.send_json({"type": "error", "message": "로컬 음성 처리가 중단됐습니다. Mac의 봇 로그를 확인하세요."})
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.gather(stt.close(), turn.speech.close())
            finally:
                try:
                    await ws.close()
                finally:
                    self.active = None
        return ws


async def serve(socket_path):
    os.umask(0o077)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    async with AsyncExitStack() as stack:
        recognizer = MlxRecognizer()
        stack.push_async_callback(recognizer.close)
        await recognizer.start()
        backend = await stack.enter_async_context(local_backend())
        bridge = Bridge(backend, recognizer)
        app = web.Application()
        app.router.add_get("/voice", bridge.voice)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=3)
        await runner.setup()
        stack.push_async_callback(runner.cleanup)
        await web.UnixSite(runner, str(socket_path)).start()
        print("[bridge] ready", flush=True)
        await stopped.wait()
        if bridge.active is not None:
            await bridge.active.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(serve(args.socket))
