"""Decode an assistant turn in short, interruptible slices on the local llama.cpp slot."""
import codecs
from copy import copy
import json
import random


CONTEXT_LIMIT = 4096
SLICE_TOKENS = 8


class NewTurnNeeded(ValueError):
    """The next input needs a fresh Gemma user-role boundary."""


class ContextFull(NewTurnNeeded):
    """The current prompt cannot fit the next input or output slice."""


class GemmaDecoder:
    def __init__(self, backend, seed, *, slot=0):
        self.client, self.url, _ = backend
        self.slot = slot
        self.rng = random.Random(seed)
        self.tokens = []
        self.pieces = {}
        self.utf8 = codecs.getincrementaldecoder("utf-8")(errors="ignore")
        self.hidden_channel = False
        self.generated_role = False
        self.turn_boundary = None

    @staticmethod
    async def _check_response(response):
        if response.status == 400:
            error = (await response.json()).get("error", {})
            if error.get("type") == "exceed_context_size_error":
                raise ContextFull("Gemma의 대화 문맥 한도에 도달했습니다.")
        response.raise_for_status()

    async def _post(self, path, payload):
        async with self.client.post(self.url + path, json=payload, allow_redirects=False) as response:
            await self._check_response(response)
            return await response.json()

    async def _format(self, messages):
        template = await self._post("/apply-template", {
            "messages": messages, "add_generation_prompt": True, "enable_thinking": False,
        })
        return template["prompt"]

    async def _tokenize(self, text, *, special):
        encoded = await self._post("/tokenize", {
            "content": text, "add_special": False, "parse_special": special,
        })
        return encoded["tokens"]

    async def _prompt_tokens(self, messages):
        return await self._tokenize(await self._format(messages), special=True)

    async def _turn_boundary(self):
        if self.turn_boundary is None:
            previous = "__ASSISTANT_TEXT__"
            upcoming = "__NEW_USER_TEXT__"
            example = await self._format([{"role": "user", "content": "Hi"},
                                          {"role": "assistant", "content": previous},
                                          {"role": "user", "content": upcoming}])
            if example.count(previous) != 1 or example.count(upcoming) != 1:
                raise RuntimeError("Gemma 채팅 템플릿에서 역할 경계를 찾을 수 없습니다.")
            tail = example.split(previous, 1)[1]
            before, found, after = tail.partition(upcoming)
            if not found or not before or not after:
                raise RuntimeError("Gemma 채팅 템플릿의 사용자·모델 역할 순서가 예상과 다릅니다.")
            self.turn_boundary = (await self._tokenize(before, special=True),
                                  await self._tokenize(after, special=True))
        return self.turn_boundary

    async def start(self, messages):
        """Keep the system message and current user input when dropping older exchanges."""
        ids = await self._prompt_tokens(messages)
        budget = CONTEXT_LIMIT - SLICE_TOKENS - 1
        if len(ids) > budget:
            offset = int(messages[0]["role"] == "system")
            pair_ends = []
            while (offset + 2 < len(messages) and messages[offset]["role"] == "user"
                   and messages[offset + 1]["role"] == "assistant"):
                offset += 2
                pair_ends.append(offset)
            system = messages[:1] if messages[0]["role"] == "system" else []
            lo, hi = 0, len(pair_ends)
            while lo < hi:
                mid = (lo + hi) // 2
                shorter = await self._prompt_tokens(system + messages[pair_ends[mid]:])
                if len(shorter) <= budget:
                    ids = shorter
                    hi = mid
                else:
                    lo = mid + 1
            if len(ids) > budget:
                raise ContextFull("이번 입력이 Gemma의 대화 문맥보다 깁니다. 나눠 입력해 주세요.")
        self.tokens = ids
        self.utf8.reset()
        self.hidden_channel = False
        self.generated_role = False

    async def inject(self, text):
        if self.generated_role or self.hidden_channel:
            raise NewTurnNeeded("Gemma의 사용자 역할 경계에서 실제 입력을 다시 엽니다.")
        before, after = await self._turn_boundary()
        user = await self._tokenize(text, special=False)
        ids = before + user + after
        if len(self.tokens) + len(ids) + SLICE_TOKENS >= CONTEXT_LIMIT:
            raise ContextFull("새 입력을 반영할 공간이 부족합니다.")
        self.tokens.extend(ids)
        # New user tokens separate the two byte streams. A later assistant token
        # cannot complete the unfinished character before this interruption.
        self.utf8.reset()

    async def _visible_text(self, ids, done):
        # A slice may end inside a Korean syllable or emoji. llama.cpp's
        # with_pieces endpoint returns such token pieces as their exact bytes.
        missing = list(dict.fromkeys(t for t in ids if t not in self.pieces))
        if missing:
            response = await self._post("/tokenize", {
                "content": missing, "add_special": False, "with_pieces": True,
            })
            for token in response["tokens"]:
                piece = token["piece"]
                self.pieces[token["id"]] = piece.encode("utf-8") if isinstance(piece, str) else bytes(piece)
        out = bytearray()
        for token in ids:
            piece = self.pieces[token]
            if piece in (b"<|turn>", b"<turn|>"):
                self.generated_role = True
                break
            if piece == b"<|channel>":
                self.hidden_channel = True
            elif piece == b"<channel|>":
                self.hidden_channel = False
            elif not self.hidden_channel and not (
                (piece.startswith(b"<|") and piece.endswith(b">"))
                or (piece.startswith(b"<") and piece.endswith(b"|>"))
                or (piece.startswith(b"<unused") and piece.endswith(b">"))
                or piece in (b"<pad>", b"<bos>", b"<eos>", b"<unk>", b"<mask>", b"[multimodal]")
            ):
                out.extend(piece)
        return self.utf8.decode(out, final=done)

    async def _stream_completion(self, payload, on_prefix):
        # Preview uses the same token-piece cache and filters, but owns its UTF-8
        # and role/channel state. Only the terminal event identifies the EOS ID
        # to omit from the canonical decoder state used by the next injection.
        preview = copy(self)
        preview.utf8 = copy(self.utf8)
        ids = []
        prefix = ""
        async with self.client.post(self.url + "/completion", json={**payload, "stream": True},
                                    allow_redirects=False) as response:
            await self._check_response(response)
            async for line in response.content:
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    break
                event = json.loads(data)
                if "error" in event:
                    raise RuntimeError("Gemma의 스트리밍 요청이 실패했습니다.")
                new = event.get("tokens", [])
                ids.extend(new)
                if event.get("stop"):
                    return {**event, "tokens": ids}
                if new and not preview.generated_role:
                    piece = await preview._visible_text(new, False)
                    if piece:
                        prefix += piece
                        await on_prefix(prefix)
        raise RuntimeError("Gemma가 스트리밍 응답을 완료하지 못했습니다.")

    async def step(self, on_prefix=None):
        room = CONTEXT_LIMIT - len(self.tokens) - 1
        if room <= 0:
            raise ContextFull("Gemma의 대화 문맥 한도에 도달했습니다.")
        payload = {
            "prompt": self.tokens, "id_slot": self.slot, "cache_prompt": True, "return_tokens": True,
            "n_predict": min(room, SLICE_TOKENS), "seed": self.rng.randrange(2**32 - 1),
            "temperature": 0.5, "top_p": 0.9,
        }
        if on_prefix is None:
            result = await self._post("/completion", payload)
        else:
            result = await self._stream_completion(payload, on_prefix)
        ids = result["tokens"]
        stop = result["stop_type"]
        if result.get("truncated"):
            raise ContextFull("Gemma의 대화 문맥 한도에 도달했습니다.")
        if stop == "limit" and not ids:
            raise RuntimeError("Gemma가 출력 토큰을 생성하지 못했습니다.")
        done = stop != "limit"
        # On EOS the server returns the stop token among the raw IDs, even
        # when it displays no content. An EOS is not part of the next reply.
        visible = ids[:-1] if stop == "eos" else ids
        text = await self._visible_text(visible, done)
        self.tokens.extend(visible)
        return text, done or self.generated_role
