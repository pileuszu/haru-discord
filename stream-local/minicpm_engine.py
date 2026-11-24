"""Run THUNLP's text duplex decoder in its compatible Transformers environment."""

import json
import os
from pathlib import Path
import subprocess
import sys
from dataclasses import asdict, dataclass
from itertools import groupby


ROOT = Path(__file__).resolve().parent
CONTEXT_LIMIT = 512
OUTPUTS_PER_SLICE = 15


@dataclass
class Slice:
    text: str
    state: str
    tokens: list[int]


class Engine:
    def __init__(self, model_path):
        self.model_path = Path(model_path).resolve()
        self.session_id = 0
        self._start()

    def _start(self):
        interpreter = ROOT / ".runtime/minicpm-venv/bin/python"
        if not interpreter.is_file():
            raise RuntimeError("MiniCPM 실행 환경이 없습니다. README의 MiniCPM 설치 절차를 확인해 주세요.")
        env = dict(os.environ, HF_HOME=str(ROOT / ".runtime/minicpm-hf"),
                   HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
        self.process = subprocess.Popen(
            [str(interpreter), "-u", __file__, str(self.model_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8", env=env,
        )
        self.broken = False
        try:
            self.device = self._receive()["device"]
        except BaseException:
            self.close()
            raise

    def _receive(self):
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("MiniCPM 프로세스가 종료됐습니다. 서버 로그를 확인해 주세요.")
        result = json.loads(line)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result

    def _send(self, data):
        try:
            self.process.stdin.write(json.dumps(data, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            return self._receive()
        except (OSError, ValueError, RuntimeError):
            self.broken = True
            raise

    def new_session(self, seed=42):
        if self.broken or self.process.poll() is not None:
            self.close()
            self._start()
        self._send({"seed": int(seed) % (2**32)})
        self.session_id += 1
        return Session(self, self.session_id)

    def close(self):
        try:
            self.process.stdin.close()
        except BrokenPipeError:
            pass
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()


class Session:
    def __init__(self, engine, session_id):
        self.engine = engine
        self.session_id = session_id

    def step(self, text_chunk=""):
        if self.session_id != self.engine.session_id:
            raise RuntimeError("이미 끝난 MiniCPM 세션입니다.")
        return Slice(**self.engine._send({"input": text_chunk}))


class DisplayBytes:
    """Preserve complete UTF-8 codepoints across slices of the Output stream."""

    def __init__(self, byte_ids):
        self.byte_ids = byte_ids
        self.pending = []

    def take(self, ids, continues):
        pieces = self.pending + ids
        self.pending = []
        visible = []
        for is_bytes, group in groupby(pieces, key=self.byte_ids.__contains__):
            run = list(group)
            if not is_bytes:
                # A regular sentencepiece token cannot finish a byte sequence.
                self.pending = []
                visible.extend(run)
                continue
            data = bytes(self.byte_ids[t] for t in run)
            start = 0
            while start < len(data):
                try:
                    data[start:].decode("utf-8")
                except UnicodeDecodeError as exc:
                    visible.extend(run[start:start + exc.start])
                    if exc.reason == "unexpected end of data":
                        self.pending = run[start + exc.start:]
                        break
                    # Invalid bytes contain no recoverable Unicode character.
                    start += exc.end
                else:
                    visible.extend(run[start:])
                    break
        if not continues:
            self.pending = []
        return visible


class DuplexDecoder:
    def __init__(self, model_path):
        import torch
        from transformers import AutoModelForCausalLM, LlamaTokenizer

        if not torch.backends.mps.is_available():
            raise RuntimeError("MiniCPM을 실행할 Metal 장치를 찾을 수 없습니다.")
        self.tokenizer = LlamaTokenizer.from_pretrained(model_path, local_files_only=True)
        self.model = AutoModelForCausalLM.from_pretrained(
            model_path, torch_dtype=torch.bfloat16, trust_remote_code=True,
            low_cpu_mem_usage=True, local_files_only=True,
        ).eval().to("mps")
        self.idle_id = self.tokenizer.convert_tokens_to_ids("<idle>")
        self.eos_id = self.tokenizer.eos_token_id
        self.byte_ids = {self.tokenizer.convert_tokens_to_ids(f"<0x{b:02X}>"): b for b in range(256)}

    def reset(self, seed):
        import torch
        from transformers import LogitsProcessorList

        # The published reset_chat_history also writes a participant log to
        # /data. Reset its in-memory fields directly for a local chat session.
        m = self.model
        m.input_ids = None
        m.model_kwargs = {}
        m.history = []
        m.history_all = []
        m.logits_processor = LogitsProcessorList()
        m.generate_flag = False
        m.print_len = 0
        m.is_length_limit = False
        self.display = DisplayBytes(self.byte_ids)
        torch.manual_seed(seed)
        torch.mps.manual_seed(seed)

    def step(self, chunk):
        m = self.model
        code = m.stream_chat(self.tokenizer, chunk.strip() or "<idle>", max_length=CONTEXT_LIMIT,
                             top_p=0.8, temperature=0.8, top_k=0)
        if code == 1:
            self.display.pending.clear()
            return Slice("", "limit", [])
        if code == 2:
            self.display.pending.clear()
            return Slice("", "idle", [])

        sampled = []
        state = "writing"
        for index in range(OUTPUTS_PER_SLICE):
            fragment, _ = m.stream_generate()
            if fragment is None:
                state = "idle"
                break
            token = m.input_ids[0, -1].item()
            sampled.append(token)
            if token == self.idle_id or "<idle>" in fragment:
                state = "idle"
                break
            if token == self.eos_id:
                # The reference demo closes generation only for an early EOS.
                # A later EOS terminates this microturn and permits another.
                if index < 6:
                    m.generate_flag = False
                state = "slice_end" if m.generate_flag else "eos"
                break

        # A byte-fallback character can be incomplete in a single-token
        # decode. Re-decode the completed slice with its original left context
        # for both the screen and the text that the next microturn will see.
        input_ids = m.input_ids[0].tolist()
        decoded = self.tokenizer.decode(input_ids, spaces_between_special_tokens=False)
        reply = decoded.rsplit("<AI>", 1)[-1].rstrip("\ufffd")
        m.history[-1]["content"] = reply
        m.history_all[-1]["content"] = reply
        # Sanitize only the presentation. Never inject bridged bytes, dropped
        # invalid bytes or repaired strings into the model's next prompt.
        terminal = sampled[-1:] if sampled and sampled[-1] in (self.eos_id, self.idle_id) else []
        content = sampled[:-1] if terminal else sampled
        visible = self.display.take(content, continues=state == "writing")
        shown = decoded
        if visible != content:
            prefix_length = len(input_ids) - len(sampled)
            shown = self.tokenizer.decode(input_ids[:prefix_length] + visible + terminal,
                                          spaces_between_special_tokens=False)
        text = shown.rsplit("<AI>", 1)[-1].removesuffix("</s>").removesuffix("<idle>")
        return Slice(text, state, sampled)


def worker(model_path):
    from contextlib import redirect_stdout
    import traceback

    try:
        with redirect_stdout(sys.stderr):
            decoder = DuplexDecoder(model_path)
        print(json.dumps({"device": str(decoder.model.device)}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            with redirect_stdout(sys.stderr):
                if "seed" in request:
                    decoder.reset(request["seed"])
                    reply = {}
                else:
                    reply = asdict(decoder.step(request["input"]))
            print(json.dumps(reply, ensure_ascii=False), flush=True)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(worker(sys.argv[1]))
