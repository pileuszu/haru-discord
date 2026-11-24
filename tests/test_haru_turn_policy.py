"""Verify the speech already chosen by Haru cannot be replaced by the late EOP fallback."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import MagicMock, Mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from native_first_policy import NativeFirstPolicy


class NativeFirstTests(unittest.TestCase):
    pad, eop, word = 1, 2, 3

    def setUp(self):
        self.rng = Mock()
        self.rng.random.return_value = 0.0
        self.gate = NativeFirstPolicy([0.0] * 4, self.rng, self.pad, self.eop)
        self.pcm = np.zeros(3200, dtype='float32')
        self.cfg = SimpleNamespace(temperature=0.7, top_k=25, top_p=1.0)
        self.key = object()
        self.logits = MagicMock()

    def pick(self, chosen, frame, candidate=None):
        sampler = Mock(return_value=np.array([candidate if candidate is not None else self.eop]))
        token = self.gate.select(self.logits, np.array([chosen]), self.key,
                                 self.cfg, self.pcm, frame, sampler)
        if candidate is None:
            sampler.assert_not_called()
        else:
            self.assertIs(sampler.call_args.args[-1], self.key)
        return int(token.item())

    def speaking_then_quiet(self):
        self.gate.observe_chunk(0.1, 0.2)
        self.gate.observe_chunk(0.0, 1.0)

    def test_eop_during_user_speech_preserves_following_word(self):
        self.gate.observe_chunk(0.1, 0.2)
        self.assertEqual(self.pick(self.eop, 25), self.eop)
        self.gate.observe_chunk(0.0, 1.0)
        self.assertEqual(self.pick(self.word, 26), self.word)
        self.assertEqual(self.pick(self.pad, 27), self.pad)
        self.rng.random.assert_not_called()

    def test_no_assistance_during_a_short_user_pause(self):
        self.gate.observe_chunk(0.2, 0.22)
        self.gate.observe_chunk(0.0, 0.98)
        self.assertEqual(self.pick(self.pad, 11), self.pad)
        self.rng.random.assert_not_called()

    def test_native_word_wins_even_after_the_deadline(self):
        self.speaking_then_quiet()
        self.assertEqual(self.pick(self.word, 31), self.word)
        self.assertEqual(self.pick(self.pad, 32), self.pad)
        self.rng.random.assert_not_called()

    def test_only_eop_can_replace_an_idle_pad(self):
        self.speaking_then_quiet()
        self.assertEqual(self.pick(self.pad, 31, candidate=self.word), self.pad)
        self.assertEqual(self.pick(self.pad, 32, candidate=self.eop), self.eop)
        self.assertEqual(self.pick(self.word, 33), self.word)

    def test_user_resuming_speech_cancels_a_pending_attempt(self):
        self.speaking_then_quiet()
        self.assertEqual(self.pick(self.pad, 31, candidate=self.pad), self.pad)
        self.gate.observe_chunk(0.2, 0.02)
        self.assertEqual(self.pick(self.pad, 32), self.pad)
        self.assertEqual(self.gate.remaining, 0)

    def test_audible_assistant_with_pad_also_keeps_the_turn(self):
        self.speaking_then_quiet()
        self.pcm.fill(0.1)
        self.assertEqual(self.pick(self.pad, 31), self.pad)
        self.pcm.fill(0.0)
        self.assertEqual(self.pick(self.pad, 32), self.pad)
        self.rng.random.assert_not_called()


if __name__ == '__main__':
    unittest.main()
