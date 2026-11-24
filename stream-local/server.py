import argparse
import asyncio
import gc
import json
import logging
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web
import yuna_tts


ROOT = Path(__file__).resolve().parent
QWEN = "stream-qwen3-8b"
MINICPM = "minicpm-duplex"
GEMMA = "gemma-local"
MINICPM_SLICE_WORDS = 5
# Recovery for repeated incomplete microturn endings, independent of answer length.
MINICPM_EOS_RETRIES = 8
MAX_ROWS = 512
QUIET_ROWS_AFTER_OUTPUT = 6
QUIET_ROWS_BEFORE_OUTPUT = 24
LOG = logging.getLogger("stream-local")


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def ends_sentence(text):
    return text.rstrip().rstrip("\"'”’」』)]}").endswith((".", "!", "?", "。", "！", "？", "…"))


class LiveInput:
    """Feed completed words using the checkpoint's training-time tokenization."""

    def __init__(self, clock=time.monotonic, settle_seconds=0.60):
        self.text = ""
        self.confirmed = ""
        self.in_flight = ""
        self.speech_overlap = False
        self.tokens = deque()
        self.changed = asyncio.Event()
        self.clock = clock
        self.settle_seconds = settle_seconds
        self.last_edit = clock()

    def update(self, text, *, speech_overlap=False):
        locked = self.confirmed + self.in_flight
        if text.startswith(locked):
            if text != self.text:
                if text.rstrip() != self.text.rstrip():
                    self.last_edit = self.clock()
                self.text = text
                self.speech_overlap = speech_overlap
                self.changed.set()
            return None
        return {"type": "sync", "text": self.text, "rejected": text,
                "input": locked, "consumed": utf16_length(locked)}

    def next_token(self, tokenizer):
        if self.tokens:
            return self.tokens.popleft()
        pending = self.text[len(self.confirmed):]
        if not pending:
            return None
        chunk = re.match(r"(\s*)(\S+)(\s*)", pending)
        if chunk is None:
            # The reference tokenizer normalizes separators. Count these source
            # characters on an active turn without injecting separate space tokens.
            self.confirmed = self.text
            return None
        leading, word, trailing = chunk.groups()
        if not trailing and not ends_sentence(word) and self.settle_remaining():
            return None

        # _tokenize_user() adds a single leading space to every lexical unit,
        # including the first. If typing resumes inside a previously flushed
        # word, append its suffix directly, preserving the actual text.
        new_word = not self.confirmed or self.confirmed[-1].isspace() or bool(leading)
        ids = tokenizer.encode((" " if new_word else "") + word)
        if not ids:
            raise ValueError("입력 단어를 토큰으로 변환할 수 없습니다.")
        self.in_flight = chunk.group()
        self.tokens.extend(ids)
        return self.tokens.popleft()

    def next_slice(self):
        """Commit a MiniCPM time slice without cutting a word still being typed."""
        pending = self.text[len(self.confirmed):]
        if not pending.strip():
            self.confirmed = self.text
            return ""
        settled = self.settle_remaining() == 0
        for count, match in enumerate(re.finditer(r"\S+\s*", pending), 1):
            part = match.group()
            if ends_sentence(part) or (count == MINICPM_SLICE_WORDS and
                                       (settled or part[-1].isspace())):
                self.in_flight = pending[:match.end()]
                return self.in_flight.strip()
        if settled:
            self.in_flight = pending
            return pending.strip()
        return ""

    def settle_remaining(self):
        return max(0.0, self.last_edit + self.settle_seconds - self.clock())

    def acknowledge(self):
        # All tokens in a lexical unit must reach the model before the UI
        # confirms its original characters, including any Unicode byte fallback.
        if not self.tokens:
            self.confirmed += self.in_flight
            self.in_flight = ""

    def has_words_pending(self):
        return bool(self.tokens or self.text[len(self.confirmed):].strip())

    def has_pending(self):
        """An active session also consumes spaces queued during its latest step."""
        return self.confirmed != self.text


def repeated_sentence(text):
    text = text.rstrip()
    if not text or text[-1] not in ".!?。！？":
        return False
    sentences = [s.strip().casefold() for s in re.split(r"[.!?。！？]+", text) if s.strip()]
    return len(sentences) > 1 and len(sentences[-1]) >= 6 and sentences[-1] in sentences[:-1]


class Runtime:
    def __init__(self, qwen_path, minicpm_path):
        self.paths = {QWEN: qwen_path, MINICPM: minicpm_path}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stream-model")
        self.owner = None
        self.selected = None
        self.engine = None

    async def run(self, func, *args):
        return await asyncio.get_running_loop().run_in_executor(self.pool, func, *args)

    def unload(self):
        if self.engine is None:
            return
        if hasattr(self.engine, "close"):
            self.engine.close()
        self.engine = None
        gc.collect()
        if self.selected == QWEN:
            import mlx.core as mx
            mx.clear_cache()
        self.selected = None

    def get_model(self, name):
        if self.selected != name:
            self.unload()
            if name == MINICPM:
                from minicpm_engine import Engine
            else:
                from engine import Engine
            self.engine = Engine(self.paths[name])
            self.selected = name
            LOG.info("%s ready", name)
        return self.engine


async def lifecycle(app):
    runtime = app[RUNTIME]
    yield
    await runtime.run(runtime.unload)
    runtime.pool.shutdown(wait=True)


RUNTIME = web.AppKey("runtime", Runtime)
UI_ORIGIN = web.AppKey("ui_origin", str)


def requests_reply(text):
    # Live chat often omits punctuation. Only use these cues to open the same
    # short onset window as an explicit '?', never to keep an answer going.
    text = text.strip()
    if text.endswith(("?", "？")):
        return True
    return bool(re.search(
        r"\b(?:what|when|where|who|whose|why|how|which|tell me|explain|calculate|suggest|recommend)\b"
        r"|(?:^|[.!?]\s*)(?:can|could|would|do|does|did|is|are)\s+\w"
        r"|(?:어디|언제|얼마|어떻|어떡|어떻게|무엇|무슨|몇|누구|왜|뭐)"
        r"|(?:(?:알려|설명해|추천해)\s*(?:줘|주세요)|해줄래|인가요|나요|일까|맞아)[.!。！\s]*$",
        text, flags=re.IGNORECASE,
    ))


async def stream(ws, runtime, engine, draft, stopped, seed):
    send = ws.send_json
    output_ids = []
    output = ""
    waiting = True
    wait_reason = "initial"
    session = None
    row = 0
    try:
        while row < MAX_ROWS and not stopped.is_set() and not ws.closed:
            if waiting:
                if not draft.has_words_pending():
                    await send({"type": "status", "state": "waiting", "reason": wait_reason})
                while not draft.has_words_pending() and not stopped.is_set() and not ws.closed:
                    draft.changed.clear()
                    await draft.changed.wait()
                if stopped.is_set() or ws.closed:
                    break
                await send({"type": "status", "state": "running"})
                if session is None:
                    session = await runtime.run(engine.new_session, seed)
                segment_start = len(output)
                reply_started = False
                prompt_start = len(draft.confirmed)
                quiet_rows = 0
                was_feeding = False
                waiting = False
            user_id = draft.next_token(engine.tokenizer)
            if user_id is None and not reply_started and not ends_sentence(draft.text):
                delay = draft.settle_remaining()
                if delay:
                    # Keep the input channel open without advancing the model on
                    # empty rows between keystrokes. An answer already in progress
                    # continues to stream while new words arrive.
                    draft.changed.clear()
                    try:
                        await asyncio.wait_for(draft.changed.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                    continue
            if user_id is not None and not was_feeding:
                segment_start = len(output)
            was_feeding = user_id is not None
            question_waiting = (
                user_id is None and not output[segment_start:].strip()
                and 4 <= quiet_rows < 16
                and requests_reply(draft.confirmed[prompt_start:])
            )
            onset_bias = min(5, quiet_rows - 3) if question_waiting else 0
            output_id = await runtime.run(session.step, user_id, onset_bias)
            draft.acknowledge()
            row += 1
            previous = output
            if output_id != engine.silence_id:
                output_ids.append(output_id)
                output = engine.tokenizer.decode(output_ids).rstrip("\ufffd")
            has_pending = draft.has_pending()
            has_reply = bool(output[segment_start:].strip())
            reply_started = reply_started or has_reply
            phase = "writing" if has_reply else "reading" if user_id is not None or has_pending else "awaiting"
            if not ws.closed:
                await send({"type": "row", "row": row, "output": output, "phase": phase,
                            "input": draft.confirmed, "consumed": utf16_length(draft.confirmed)})
            if user_id is not None or has_pending:
                quiet_rows = 0
                continue
            has_new_text = bool(output[len(previous):].strip())
            quiet_rows = 0 if has_new_text else quiet_rows + 1
            segment = output[segment_start:]
            quiet_limit = QUIET_ROWS_AFTER_OUTPUT if segment.strip() else QUIET_ROWS_BEFORE_OUTPUT
            waiting = quiet_rows >= quiet_limit or (has_new_text and repeated_sentence(segment))
            if waiting:
                wait_reason = "answered" if has_reply else "no_output"
        if not ws.closed:
            reason = "세션을 종료했습니다." if stopped.is_set() else f"이 데모의 세션 한도인 {MAX_ROWS}단계에 도달했습니다. 새 대화를 시작할 수 있습니다."
            await send({"type": "status", "state": "stopped", "message": reason})
    except (ConnectionError, asyncio.CancelledError):
        pass
    except Exception as exc:
        LOG.exception("Generation failed")
        if not ws.closed:
            await send({"type": "error", "message": str(exc)})
    finally:
        runtime.owner = None


async def stream_minicpm(ws, runtime, engine, draft, stopped, seed):
    session = await runtime.run(engine.new_session, seed)
    output = ""
    separate_reply = False
    row = 0
    wait_reason = "initial"
    while row < MAX_ROWS and not stopped.is_set() and not ws.closed:
        if not draft.has_words_pending():
            status = {"type": "status", "state": "waiting", "reason": wait_reason}
            if wait_reason == "unfinished":
                status["message"] = "모델이 여러 차례 이어 썼지만 문장을 마치지 못했습니다."
            await ws.send_json(status)
        while not draft.has_words_pending() and not stopped.is_set() and not ws.closed:
            draft.changed.clear()
            await draft.changed.wait()
        if stopped.is_set() or ws.closed:
            break
        await ws.send_json({"type": "status", "state": "running"})
        segment_start = len(output)
        idle_ticks = 0
        unfinished_ends = 0
        while row < MAX_ROWS and not stopped.is_set() and not ws.closed:
            chunk = draft.next_slice()
            if not chunk and draft.has_words_pending():
                # The user is typing the next slice. Feeding a silent-user
                # tick here makes the model answer an unfinished question.
                draft.changed.clear()
                try:
                    await asyncio.wait_for(draft.changed.wait(), draft.settle_remaining())
                except TimeoutError:
                    pass
                continue
            result = await runtime.run(session.step, chunk)
            if result.state == "limit":
                await ws.send_json({"type": "status", "state": "stopped",
                                    "message": "모델 문맥 한도에 도달했습니다. 새 세션을 시작하세요."})
                return
            draft.acknowledge()
            row += 1
            if chunk and output.strip():
                separate_reply = True
            if separate_reply and result.text.strip():
                if not output.endswith("\n") and not result.text.startswith("\n"):
                    output += "\n"
                separate_reply = False
            output += result.text
            phase = "writing" if result.text.strip() else "reading" if chunk else "awaiting"
            await ws.send_json({"type": "row", "row": row, "output": output, "phase": phase,
                                "model_state": result.state, "input": draft.confirmed,
                                "consumed": utf16_length(draft.confirmed)})
            segment = output[segment_start:]
            complete_slice = result.state == "slice_end" and ends_sentence(segment)
            if chunk:
                unfinished_ends = 0
            elif result.state == "slice_end" and not complete_slice:
                unfinished_ends += 1
            if draft.has_pending():
                idle_ticks = 0
                continue
            idle_ticks = idle_ticks + 1 if not chunk and result.state == "idle" else 0
            # Some native sessions emit <idle> for the last input slice and only
            # start answering on a following empty slice. Never advance an unused session.
            if unfinished_ends >= MINICPM_EOS_RETRIES:
                wait_reason = "unfinished"
                break
            if result.state == "eos" or complete_slice or idle_ticks >= 3 or repeated_sentence(segment):
                wait_reason = "answered" if output[segment_start:].strip() else "no_output"
                break
    if not ws.closed:
        message = "세션을 종료했습니다." if stopped.is_set() else "모델 단계 한도에 도달했습니다. 새 세션을 시작하세요."
        await ws.send_json({"type": "status", "state": "stopped", "message": message})


async def run_selected(ws, runtime, model, draft, stopped, seed):
    try:
        if model == GEMMA:
            from gemma_chat import stream as stream_gemma
            await runtime.run(runtime.unload)
            await stream_gemma(ws, draft, stopped, seed)
            return
        if runtime.selected != model:
            await ws.send_json({"type": "status", "state": "loading", "model": model})
        engine = await runtime.run(runtime.get_model, model)
        run = stream_minicpm if model == MINICPM else stream
        await run(ws, runtime, engine, draft, stopped, seed)
    except (ConnectionError, asyncio.CancelledError):
        pass
    except Exception as exc:
        LOG.exception("Session failed for %s", model)
        if not ws.closed:
            await ws.send_json({"type": "error", "message": str(exc)})
    finally:
        if runtime.owner is ws:
            runtime.owner = None


async def websocket(request):
    origin = request.headers.get("Origin")
    if origin and urlsplit(origin).netloc != request.host and origin != request.app.get(UI_ORIGIN):
        raise web.HTTPForbidden()
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=128 * 1024)
    await ws.prepare(request)
    runtime = request.app[RUNTIME]
    task = None
    stopped = asyncio.Event()
    try:
        await ws.send_json({"type": "status", "state": "ready", "gemma_interleaved": True,
                            "yuna_tts": True, "speech_overlap": True})
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            data = json.loads(msg.data)
            kind = data.get("type")
            if kind == "start":
                model = data.get("model", QWEN)
                if model not in (QWEN, MINICPM, GEMMA):
                    await ws.send_json({"type": "error", "message": "지원하지 않는 모델입니다."})
                    continue
                if runtime.owner is not None:
                    await ws.send_json({"type": "error", "message": "이미 실행 중인 세션이 있습니다. 먼저 해당 세션을 종료해 주세요."})
                    continue
                seed = int(data.get("seed", 42)) % (2**32)
                draft = LiveInput()
                stopped = asyncio.Event()
                runtime.owner = ws
                task = asyncio.create_task(run_selected(ws, runtime, model, draft, stopped, seed))
            elif kind == "input" and runtime.owner is ws and not stopped.is_set():
                text = data.get("text")
                if not isinstance(text, str):
                    raise ValueError("입력 형식이 잘못되었습니다.")
                correction = draft.update(text, speech_overlap=data.get("speech_overlap") is True)
                if correction:
                    await ws.send_json(correction)
            elif kind == "stop" and runtime.owner is ws:
                stopped.set()
                draft.changed.set()
    except (ConnectionError, asyncio.CancelledError):
        pass
    except Exception as exc:
        LOG.exception("WebSocket failed")
        if not ws.closed:
            await ws.send_json({"type": "error", "message": str(exc)})
    finally:
        stopped.set()
        if task:
            draft.changed.set()
            await asyncio.gather(task, return_exceptions=True)
    return ws


async def index(request):
    return web.FileResponse(ROOT / "static/index.html", headers={"Cache-Control": "no-store"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, default=ROOT / ".models/stream-qwen3-8b")
    parser.add_argument("--minicpm-model", type=Path, default=ROOT / ".models/minicpm-duplex")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--ui-port", type=int, help="Also accept the UI served at this 127.0.0.1 port during a rolling upgrade.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    app = web.Application()
    if args.ui_port:
        app[UI_ORIGIN] = f"http://127.0.0.1:{args.ui_port}"
    app[RUNTIME] = Runtime(args.model, args.minicpm_model)
    app.cleanup_ctx.append(lifecycle)
    app.add_routes([web.get("/", index), web.get("/ws", websocket)])
    yuna_tts.install(app, app.get(UI_ORIGIN))
    web.run_app(app, host="127.0.0.1", port=args.port, access_log=None)


if __name__ == "__main__":
    main()
