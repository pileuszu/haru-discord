"""Stream on-device Korean Yuna speech to the browser as mono signed 16-bit PCM."""
import asyncio
import hashlib
import logging
import os
from pathlib import Path
import struct
import tempfile
from urllib.parse import urlsplit

from aiohttp import web


ROOT = Path(__file__).resolve().parent
SAMPLE_RATE = 22050
MAX_CHARS = 480
LOG = logging.getLogger(__name__)


async def worker_binary():
    source = ROOT / "yuna_worker.m"
    name = "yuna-worker-" + hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    directory = ROOT / ".runtime/yuna"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if not path.is_file():
        # Separate servers may warm simultaneously during a rolling upgrade.
        with tempfile.TemporaryDirectory(dir=directory) as tmp:
            built = Path(tmp) / "worker"
            compiler = await asyncio.create_subprocess_exec(
                "/usr/bin/clang", "-O2", "-fobjc-arc", "-framework", "Foundation",
                "-framework", "AVFoundation", str(source), "-o", str(built),
            )
            try:
                if await compiler.wait():
                    raise RuntimeError("유나 음성 합성기를 빌드할 수 없습니다.")
                os.replace(built, path)
            finally:
                if compiler.returncode is None:
                    compiler.kill()
                    await compiler.wait()
    return path


class YunaBusy(Exception):
    pass


class Yuna:
    def __init__(self):
        self.process = None
        self.lock = asyncio.Lock()
        self.jobs = 0

    async def close(self):
        process, self.process = self.process, None
        if process and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            # Drain stdout as well: the child may have been writing when stopped.
            await process.communicate()

    async def _phrase(self, text, write=None):
        data = text.encode("utf-8")
        self.process.stdin.write(struct.pack("<I", len(data)) + data)
        await self.process.stdin.drain()
        while True:
            prefix = await self.process.stdout.readexactly(4)
            length, = struct.unpack("<I", prefix)
            if not length:
                return
            if length % 2 or length > 2 * 1024 * 1024:
                raise RuntimeError("유나 음성 버퍼 형식이 올바르지 않습니다.")
            pcm = await self.process.stdout.readexactly(length)
            if write:
                await write(pcm)

    async def render(self, text=None, write=None, connected=lambda: True):
        # A tab issues a single fetch at a time. Allow a replacement request
        # while its aborted predecessor finishes draining its short native phrase.
        if self.jobs >= 3:
            raise YunaBusy
        self.jobs += 1
        try:
            async with self.lock:
                if not connected():
                    return
                try:
                    async with asyncio.timeout(30):
                        if self.process is None or self.process.returncode is not None:
                            self.process = await asyncio.create_subprocess_exec(
                                await worker_binary(), stdin=asyncio.subprocess.PIPE,
                                stdout=asyncio.subprocess.PIPE,
                            )
                            # Render privately; phoneme/voice resources initialize lazily.
                            await self._phrase("준비")
                        if text and connected():
                            await self._phrase(text, write)
                except BaseException:
                    # A canceled or truncated frame cannot be reused by another request.
                    await self.close()
                    raise
        finally:
            self.jobs -= 1


YUNA = web.AppKey("yuna", Yuna)


def install(app, ui_origin=None):
    async def lifecycle(app):
        app[YUNA] = Yuna()
        yield
        await app[YUNA].close()

    async def speech(request):
        headers = {"Cache-Control": "no-store", "Vary": "Origin"}
        origin = request.headers.get("Origin")
        host = urlsplit("//" + request.host).hostname
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise web.HTTPForbidden()
        if origin:
            if origin != f"{request.scheme}://{request.host}" and origin != ui_origin:
                raise web.HTTPForbidden()
            headers["Access-Control-Allow-Origin"] = origin
        if request.method == "OPTIONS":
            headers.update({"Access-Control-Allow-Methods": "POST", "Access-Control-Allow-Headers": "Content-Type"})
            return web.Response(status=204, headers=headers)
        if request.path == "/yuna.js":
            return web.FileResponse(ROOT / "static/yuna.js", headers=headers)
        text = None
        if request.path == "/tts":
            if request.content_type != "application/json":
                return web.Response(status=415, text="application/json 요청이 필요합니다.", headers=headers)
            try:
                payload = await request.json()
            except (ValueError, UnicodeDecodeError):
                payload = None
            text = payload.get("text") if isinstance(payload, dict) else None
            if not isinstance(text, str) or not 0 < len(text.strip()) <= MAX_CHARS or len(text) > MAX_CHARS:
                return web.Response(status=400, text="한국어 구절을 480자 이내로 보내 주세요.", headers=headers)

        response = web.StreamResponse(headers={**headers, "Content-Type": "application/octet-stream",
                                               "X-Audio-Sample-Rate": str(SAMPLE_RATE)})
        disconnected = False
        def connected():
            return not disconnected and request.transport is not None and not request.transport.is_closing()

        async def write(pcm):
            nonlocal disconnected
            if connected():
                try:
                    if not response.prepared:
                        await response.prepare(request)
                    await response.write(pcm)
                except ConnectionError:
                    # Consume the rest from the worker without scheduling it on the client.
                    disconnected = True

        try:
            await request.app[YUNA].render(text, write, connected)
        except YunaBusy:
            return web.Response(status=429, text="유나 음성 합성기가 사용 중입니다.", headers=headers)
        except (OSError, RuntimeError, asyncio.IncompleteReadError, TimeoutError):
            LOG.exception("Yuna synthesis failed")
            if response.prepared:
                if request.transport:
                    request.transport.close()
                return response
            return web.Response(status=503, text="macOS 한국어 유나 음성을 준비할 수 없습니다.", headers=headers)
        if text is None:
            return web.json_response({"voice": "Yuna", "sample_rate": SAMPLE_RATE}, headers=headers)
        return response

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/yuna.js", speech)
    for path in ("/tts", "/tts/warm"):
        app.router.add_post(path, speech)
        app.router.add_options(path, speech)
