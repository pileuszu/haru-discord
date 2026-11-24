"""Local MLX inference for the ten-channel Stream-Qwen3-8B checkpoint."""

import ast
import json
from pathlib import Path

import mlx.core as mx
import mlx.nn as nn
from mlx_lm.models.cache import KVCache
from mlx_lm.models.qwen3 import Model, ModelArgs
from tokenizers import Tokenizer


CHANNELS = 10


class TextTokenizer:
    def __init__(self, path):
        self.backend = Tokenizer.from_file(str(path))
        self.backend.encode_special_tokens = True

    def encode(self, text):
        return self.backend.encode(text, add_special_tokens=False).ids

    def decode(self, ids):
        return self.backend.decode(ids, skip_special_tokens=True)


class RowRoPE(nn.Module):
    """All ten tokens in a row share a rotary position."""

    def __init__(self, rotary):
        super().__init__()
        self.rotary = rotary

    def __call__(self, x, offset=0):
        batch, heads, length, dim = x.shape
        rows = length // CHANNELS
        # Give each channel its own length-R sequence for the standard RoPE.
        x = x.reshape(batch, heads, rows, CHANNELS, dim).swapaxes(2, 3)
        x = x.reshape(batch, heads * CHANNELS, rows, dim)
        x = self.rotary(x, offset=offset // CHANNELS)
        return x.reshape(batch, heads, CHANNELS, rows, dim).swapaxes(2, 3).reshape(
            batch, heads, length, dim
        )


def block_mask(cached_ids, current_ids, silence_id):
    """Keys in earlier rows are visible unless silent. Self is always visible."""
    query = mx.arange(current_ids.size) + cached_ids.size
    keys = mx.arange(cached_ids.size + current_ids.size)
    ids = mx.concatenate([cached_ids, current_ids])
    past = (query[:, None] // CHANNELS > keys[None, :] // CHANNELS)
    return (past & (ids[None, :] != silence_id)) | (query[:, None] == keys[None, :])


def warm_header(path, tokenizer, silence_id):
    # Read the pinned checkpoint's actual prompt data without executing remote code.
    source = ast.parse(path.read_text(encoding="utf-8"))
    for node in source.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "SYSTEM_PROMPT_WORDS"
            for target in node.targets
        ):
            words = ast.literal_eval(node.value)
            if not words or any(len(row) != CHANNELS for row in words):
                raise ValueError("Invalid warm-start header in the checkpoint")
            return [
                [silence_id if word == "-" else tokenizer.encode(word)[0] for word in row]
                for row in words
            ]
    raise ValueError("Checkpoint is missing its warm-start header")


def direct_reply_header(tokenizer, silence_id):
    """A short style instruction in the checkpoint's User/Output row format."""
    instruction = "Please answer my questions directly."
    user_tokens = [t for word in instruction.split() for t in tokenizer.encode(" " + word)]
    answer_tokens = tokenizer.encode(" Okay.")
    rows = []
    for channel, tokens in ((0, user_tokens), (1, [silence_id] * 2 + answer_tokens + [silence_id] * 2)):
        for token in tokens:
            row = [silence_id] * CHANNELS
            row[channel] = token
            rows.append(row)
    return rows


class Engine:
    def __init__(self, model_path):
        directory = Path(model_path)
        config = json.loads((directory / "config.json").read_text())
        if (config.get("num_channels") != CHANNELS
                or config.get("channel_embedding_method") != "additive"
                or config.get("role_gating_enabled")
                or config.get("use_sliding_window")
                or config.get("model_type") != "qwen3"):
            raise ValueError("Expected the ten-channel dense Stream-Qwen3-8B checkpoint")
        self.tokenizer = TextTokenizer(directory / "tokenizer.json")
        self.silence_id = self.tokenizer.encode(" -")[0]
        self.header = warm_header(directory / "stream_inference.py", self.tokenizer, self.silence_id)
        self.header.extend(direct_reply_header(self.tokenizer, self.silence_id))
        self.network = Model(ModelArgs.from_dict(config))
        self.network.model.channel_embedding = nn.Embedding(CHANNELS, config["hidden_size"])
        for layer in self.network.layers:
            layer.self_attn.rope = RowRoPE(layer.self_attn.rope)

        index = json.loads((directory / "model.safetensors.index.json").read_text())
        weights = {}
        for shard in sorted(set(index["weight_map"].values())):
            weights.update(mx.load(str(directory / shard)))
        # The author saved these auxiliary norms even when role gating is disabled.
        for i in range(config["num_hidden_layers"]):
            weights.pop(f"model.layers.{i}.self_attn.gate_in_norm.weight")
        self.network.load_weights(list(weights.items()), strict=True)
        self.network.eval()
        mx.eval(self.network.parameters())

    def new_session(self, seed=0):
        return Session(self, seed)


def sample_channels(logits, key):
    """Sample each next-row channel using the live demo's T=.6, K=20, P=.95."""
    scaled = (logits / 0.6).astype(mx.float32)
    order = mx.argsort(-scaled, axis=-1)
    values = mx.take_along_axis(scaled, order, axis=-1)
    # Preserve ties at the kth value as in the checkpoint's implementation.
    within_k = values >= values[:, 19:20]
    values = mx.where(within_k, values, -float("inf"))
    probs = mx.softmax(values, axis=-1)
    keep = mx.cumsum(probs, axis=-1) - probs <= 0.95
    chosen = mx.random.categorical(mx.where(keep, values, -float("inf")), key=key)
    return mx.take_along_axis(order, chosen[:, None], axis=-1)[:, 0]


class Session:
    def __init__(self, engine, seed):
        self.engine = engine
        self.cache = [KVCache() for _ in engine.network.layers]
        self.cached_ids = mx.array([], dtype=mx.int32)
        self.key = mx.random.key(seed)
        self.row = self._sample(self._forward(engine.header))

    def _forward(self, rows):
        network = self.engine.network
        token_ids = mx.array(rows, dtype=mx.int32).reshape(-1)
        mask = block_mask(self.cached_ids, token_ids, self.engine.silence_id)
        channel_ids = mx.arange(token_ids.size) % CHANNELS
        hidden = network.model.embed_tokens(token_ids) + network.model.channel_embedding(channel_ids)
        hidden = hidden[None, :, :]
        for layer, cache in zip(network.layers, self.cache):
            hidden = layer(hidden, mask, cache)
        self.cached_ids = mx.concatenate([self.cached_ids, token_ids])
        # User is injected, so only the other nine final-row logits are used.
        return network.lm_head(network.model.norm(hidden[:, -(CHANNELS - 1):, :]))[0]

    def _sample(self, logits, onset_bias=0):
        if onset_bias:
            logits = logits.at[0, self.engine.silence_id].subtract(onset_bias)
        self.key, sample_key = mx.random.split(self.key)
        tokens = sample_channels(logits, sample_key).tolist()
        return [self.engine.silence_id, *tokens]

    def step(self, user_token=None, onset_bias=0):
        """Consume a user token in the current row and return next-row Output."""
        silence = self.engine.silence_id
        self.row[0] = silence if user_token is None else user_token
        self.row = self._sample(self._forward([self.row]), onset_bias)
        return self.row[1]
