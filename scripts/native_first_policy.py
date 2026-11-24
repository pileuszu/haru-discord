"""A single-turn fallback for a native Haru session that has yet to start a response."""
import math

import numpy as np


class NativeFirstPolicy:
    user_rms_threshold = 10 ** (-43.0 / 20)
    output_rms_threshold = 10 ** (-40.0 / 20)
    min_voiced_seconds = 0.12
    quiet_grace_seconds = 1.0
    max_attempt_frames = 8
    eop_boost = 5.0

    def __init__(self, weights, rng, pad_id, eop_id):
        if len(weights) != 4 or not all(math.isfinite(w) for w in weights):
            raise ValueError("Expected four finite learned hazard weights")
        self.weights = tuple(weights)
        self.rng = rng
        self.pad_id = pad_id
        self.eop_id = eop_id
        self.voiced_seconds = 0.0
        self.quiet_seconds = 0.0
        self.remaining = 0
        self.output_hot_blocks = 0
        self.finished = None
        self.events = []
        self.just_applied_bias = 0.0

    def observe_chunk(self, rms, seconds):
        if rms >= self.user_rms_threshold:
            self.voiced_seconds += seconds
            self.quiet_seconds = 0.0
            # Barge-in or a resumed question cancels any pending intervention.
            self.remaining = 0
        else:
            self.quiet_seconds += seconds

    def _assistant_active(self, pcm):
        # Native PCM for this frame has already been generated from its own hidden state.
        # Inspect short time windows, carrying a run across model frame boundaries.
        for block in np.array_split(pcm, 7):
            rms_squared = float(np.mean(np.square(block)))
            if rms_squared >= self.output_rms_threshold ** 2:
                self.output_hot_blocks += 1
                if self.output_hot_blocks >= 4:  # four consecutive ~19 ms windows
                    return True
            else:
                self.output_hot_blocks = 0
        return False

    def select(self, logits, native_token, token_key, cfg, pcm, frame, sample_token):
        """Return the original token unless a quiet PAD is nudged into EOP, on the same key."""
        self.just_applied_bias = 0.0
        if self.finished:
            return native_token
        token_id = int(native_token.item())
        if token_id != self.pad_id:
            self.finished = 'native_eop' if token_id == self.eop_id else 'native_lexical'
            self.events.append(dict(frame=frame, event=self.finished, token=token_id))
            return native_token
        if self.voiced_seconds + 1e-8 < self.min_voiced_seconds:
            return native_token
        if self._assistant_active(pcm):
            self.finished = 'native_pcm'
            self.events.append(dict(frame=frame, event=self.finished))
            return native_token
        if self.quiet_seconds + 1e-8 < self.quiet_grace_seconds:
            return native_token
        if not self.remaining:
            features = (1.0, min(self.quiet_seconds, 1.5), 0.0, min(self.voiced_seconds, 2.0))
            p = 1 / (1 + math.exp(-sum(w*x for w,x in zip(self.weights, features))))
            u = float(self.rng.random())
            self.events.append(dict(frame=frame, event='decision', p=p, u=u,
                                    start=u<p, features=features))
            if u >= p:
                return native_token
            self.remaining = self.max_attempt_frames
        self.remaining -= 1
        self.just_applied_bias = self.eop_boost
        biased = logits.at[:, self.eop_id].add(self.eop_boost)
        candidate = sample_token(biased, cfg.temperature, cfg.top_k, cfg.top_p, token_key)
        candidate_id = int(candidate.item())
        self.events.append(dict(frame=frame, event='attempt', native_id=token_id,
                                candidate_id=candidate_id, accepted=candidate_id==self.eop_id))
        if candidate_id == self.eop_id:
            self.finished = 'fallback_eop'
            return candidate
        if not self.remaining:
            self.finished = 'fallback_expired'
        return native_token
