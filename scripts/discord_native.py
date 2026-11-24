"""Discord's audio-first transport for the pinned Haru native Unix socket."""
import asyncio
import json
import logging
import os
import struct
import time

import numpy as np
from haru_mlx import native

STEP = 0x16  # <u32 frame index, f32 complete step milliseconds>
logger = logging.getLogger("haru_mlx.native")


async def _stream_call(worker, operation, send_audio):
    """Deliver one PCM notification while its originating MLX operation is still running."""
    loop = asyncio.get_running_loop()
    audio = loop.create_future()

    def deliver(index, pcm, seconds):
        if not audio.done():
            audio.set_result((index, pcm, seconds))

    def publish(index, pcm, seconds):
        # Only CPU-owned numpy data crosses from the MLX worker to the event loop.
        loop.call_soon_threadsafe(deliver, index, pcm, seconds)

    completed = asyncio.create_task(worker.run(operation, publish))
    try:
        await asyncio.wait((audio, completed), return_when=asyncio.FIRST_COMPLETED)
        if not audio.done():
            completed.result()  # surface a model error before it produced audio
            raise RuntimeError("Haru completed a streaming step without publishing PCM")
        await send_audio(*audio.result())
        return await completed
    finally:
        audio.cancel()
        completed.cancel()
        await asyncio.gather(completed, return_exceptions=True)


async def stream_step(worker, session, send_audio, send_complete):
    frame = await _stream_call(worker, session.step_streaming, send_audio)
    await send_complete(frame)
    return frame


class DiscordNativeServer(native.NativeServer):
    def _settings(self, request):
        settings = super()._settings(request)
        # Upstream treats an explicit empty string as a request for its first-meeting preset.
        if request.get("system_prompt") == "":
            settings.system_prompt = ""
        return settings

    async def _serve(self, reader, writer):
        worker = self.backend.worker
        cfg = self.backend.model.config
        frame_samples, rate = cfg.frame_samples, cfg.sample_rate
        ratio = native.CLIENT_SAMPLE_RATE // rate
        write_lock = asyncio.Lock()

        async def send(kind, payload):
            async with write_lock:
                writer.write(struct.pack("<BI", kind, len(payload)) + payload)
                await writer.drain()

        async def send_json(kind, value):
            await send(kind, json.dumps(value, ensure_ascii=False).encode())

        async def read_message():
            kind, size = struct.unpack("<BI", await reader.readexactly(5))
            return kind, await reader.readexactly(size)

        async def send_audio(index, pcm, seconds):
            data = native.upsample_2x(pcm).astype("<f4").tobytes()
            await send(native.FRAME, struct.pack("<If", index, 1000 * seconds) + data)

        early = False

        async def send_complete(frame):
            if early:
                await send(STEP, struct.pack("<If", frame.index, 1000 * frame.seconds))
            if frame.text:
                await send(native.TEXT, frame.text.encode())

        async def send_hello():
            hello = dict(sample_rate=native.CLIENT_SAMPLE_RATE, frame_samples=frame_samples * ratio,
                         step_ms=1000 * self.step_seconds)
            if early:
                hello["audio_first"] = True
            await send_json(native.HELLO, hello)

        await send_json(native.ASSETS, dict(
            self.assets, default_language=self.language, step_ms=1000 * self.step_seconds,
            precision=f"int{self.backend.quantize}" if self.backend.quantize else "bf16",
            model=self.backend.model_path,
        ))
        try:
            first_message = await read_message()
            kind, payload = first_message
            request = json.loads(payload or b"{}") if kind == native.START else {}
            early = request.get("audio_first") is True
            if early:
                def start(publish):
                    session = self.backend.session(self._settings(request))
                    return session, session.start_streaming(publish)

                async def first_audio(index, pcm, seconds):
                    await send_hello()
                    await send_audio(index, pcm, seconds)

                session, first = await _stream_call(worker, start, first_audio)
            else:
                # Upstream owns PREPARE caching/cancellation. A prepared first frame
                # can ship immediately on START; subsequent frames use the chosen mode.
                async def read_start():
                    nonlocal first_message, early
                    message = first_message
                    if message is None:
                        message = await read_message()
                    first_message = None
                    kind, payload = message
                    if kind in (native.START, native.PREPARE):
                        request = json.loads(payload or b"{}")
                        option = request.pop("audio_first", None)
                        if kind == native.START:
                            early = option is True
                        # The packet delivery mode does not change a prompt's cache key.
                        return kind, json.dumps(request).encode()
                    return message

                session, first = await self._wait_for_start(read_start, send_json)
                await send_hello()
                await send_audio(first.index, first.audio, first.seconds)
            await send_complete(first)
            logger.info("conversation started (%d prompt positions)", session.prompt_length)
            await self._conversation(session, read_message, send_audio, send_complete, early)
        except (asyncio.IncompleteReadError, ConnectionError):
            raise
        except Exception as error:
            logger.exception("Haru conversation failed")
            await send_json(native.ERROR, {"message": str(error)})

    async def _conversation(self, session, read_message, send_audio, send_complete, early):
        worker = self.backend.worker
        cfg = self.backend.model.config
        frame_seconds = cfg.frame_samples / cfg.sample_rate
        # One waiting complete input is sufficient. Socket reads and writes provide backpressure.
        complete = asyncio.Queue(maxsize=1)
        step_times = []

        async def receive():
            received = 0
            queued = 0
            leftover = np.zeros(0, dtype=np.float32)
            max_samples = (session.config.max_frames - 1) * cfg.frame_samples
            while True:
                kind, payload = await read_message()
                if kind != native.AUDIO or received >= max_samples:
                    continue
                x = np.concatenate([leftover, np.frombuffer(payload, dtype="<f4")])
                even = len(x) // 2 * 2
                leftover = x[even:]
                pcm = native.downsample_2x(x[:even])[:max_samples - received]
                if not len(pcm):
                    continue
                arrived = time.perf_counter()
                await worker.run(session.feed, pcm)
                received += len(pcm)
                while (queued + 1) * cfg.frame_samples <= received:
                    queued += 1
                    await complete.put(arrived)

        async def generate():
            for _ in range(session.config.max_frames - 1):
                arrived = await complete.get()
                if early:
                    frame = await stream_step(worker, session, send_audio, send_complete)
                else:
                    frame = await worker.run(session.step)
                    await send_audio(frame.index, frame.audio, frame.seconds)
                    await send_complete(frame)
                worker.expect(arrived + frame_seconds)
                step_times.append(frame.seconds)

        reader = asyncio.create_task(receive())
        generator = asyncio.create_task(generate())
        try:
            done, _ = await asyncio.wait((reader, generator), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            # At the model's frame limit the client drains its last PCM, then closes.
            if generator in done:
                await reader
        finally:
            reader.cancel()
            generator.cancel()
            await asyncio.gather(reader, generator, return_exceptions=True)
            worker.expect(None)
            if step_times:
                p50, p95 = np.percentile(step_times, [50, 95]) * 1000
                logger.info("client left: %d frames, complete step p50 %.1f p95 %.1f ms",
                            len(step_times), p50, p95)


def main(argv=None):
    from discord_audio_session import DiscordBackend

    args = native.build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    backend = DiscordBackend(args.model or args.hf_repo or native.DEFAULT_REPO,
                             quantize=None if args.quantize == "none" else int(args.quantize),
                             warm_seconds=args.warm_ms / 1000)
    server = DiscordNativeServer(backend, args.language, args.speak_first, args.seed)
    logger.info("loading %s", backend.model_path)
    backend.load_blocking()
    backend.worker.submit(server.measure_step).result()

    async def run():
        if os.path.exists(args.socket):
            os.unlink(args.socket)
        listener = await asyncio.start_unix_server(server.handle, path=args.socket)
        print(f"[native] ready on {args.socket} (step {1000 * server.step_seconds:.0f} ms)", flush=True)
        async with listener:
            await listener.serve_forever()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
