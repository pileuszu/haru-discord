"""The pinned Haru MLX session with an early PCM delivery point for Discord."""
import time

import mlx.core as mx
import numpy as np
from haru_mlx.frames import DuplexConfig, Frame
from haru_mlx.backends import LocalBackend
from haru_mlx.sampling import sample_token
from haru_mlx.session import DuplexSession


class DiscordSession(DuplexSession):
    _publish = None

    def _stream(self, method, publish):
        # All accesses to a DuplexSession, including this callback, run on its MLX worker.
        self._publish = publish
        try:
            return method()
        finally:
            self._publish = None

    def start_streaming(self, publish):
        return self._stream(self.start, publish)

    def step_streaming(self, publish):
        return self._stream(self.step, publish)

    def _emit(self, hidden, started):
        if self._publish is None:
            return super()._emit(hidden, started)
        model, cfg = self.model, self.config
        # Preserve upstream's key order even though audio is evaluated first.
        token_key, audio_key = self._next_key(), self._next_key()
        noise = mx.random.normal((1, model.config.latent_dim), key=audio_key).astype(model.dtype)
        latent = model.unscale_latents(model.head.sample(hidden, noise, cfg.guidance_scale))
        audio = mx.clip(self._codec.decode(latent[None])[0], -1.0, 1.0)
        mx.eval(latent, audio, self._cache_arrays(), self._codec.arrays())
        pcm = np.array(audio, dtype=np.float32)
        self._publish(self.index, pcm, time.perf_counter() - started)

        # Keep the complete text path and its recurrent state for the following frame.
        logits = model.logits(hidden)
        if self._dry is not None:
            self._dry.apply(logits)
        logits[:, self._stream_ids] = model.logits_fp32(hidden, self._stream_ids)
        if cfg.eop_bias:
            logits[:, self.eop_id] = logits[:, self.eop_id] + cfg.eop_bias
        if self._force_text > 0:
            logits[:, mx.array(self._banned, dtype=mx.int32)] = -mx.inf
            self._force_text -= 1
        token = sample_token(logits, cfg.temperature, cfg.top_k, cfg.top_p, token_key)
        policy = getattr(self, "turn_policy", None)
        if policy is not None:
            token = policy.select(logits, token, token_key, cfg, pcm, self.index, sample_token)
        mx.eval(token)
        self._token, self._latent = token, latent
        token_id = int(token.item())
        if self._dry is not None:
            self._dry.update(token_id)
        text = self._detokenizer.push(token_id)
        frame = Frame(pcm, token_id, text, self.index, time.perf_counter() - started)
        self.index += 1
        frame.done = self.index >= cfg.max_frames
        return frame


class DiscordBackend(LocalBackend):
    def session(self, settings):
        config = DuplexConfig(
            guidance_scale=settings.guidance_scale, temperature=settings.temperature,
            top_k=settings.top_k, top_p=settings.top_p, speak_first=settings.speak_first,
            max_frames=settings.max_frames, seed=settings.seed,
        )
        return DiscordSession(self.model, settings.system_prompt, settings.reference, config)
