import unittest
from unittest.mock import Mock

from minicpm_engine import DisplayBytes, Engine


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.output = DisplayBytes({i: i for i in range(256)})

    def test_hangul_spanning_slices(self):
        syllable = list("파".encode())
        self.assertEqual(self.output.take([500, *syllable[:2]], continues=True), [500])
        self.assertEqual(self.output.take([syllable[2], 501], continues=False), syllable + [501])

    def test_incomplete_at_eos_and_invalid_bytes(self):
        self.assertEqual(self.output.take([500, 0xEB, 0x8D], continues=False), [500])
        self.assertEqual(self.output.take([0x82, 501], continues=True), [501])
        self.assertEqual(self.output.take([0xEB, 501, 0x82], continues=False), [501])
        # Correctly encoded U+FFFD is literal text and survives filtering.
        encoded = list("\ufffd".encode())
        self.assertEqual(self.output.take(encoded, continues=False), encoded)

    def test_failed_worker_is_reloaded_only_on_new_session(self):
        engine = Engine.__new__(Engine)
        engine.broken = True
        engine.process = Mock()
        engine.close = Mock()
        engine._start = Mock()
        engine._send = Mock()
        engine.session_id = 1
        session = engine.new_session(43)
        engine.close.assert_called_once()
        engine._start.assert_called_once()
        engine._send.assert_called_once_with({"seed": 43})
        self.assertEqual(session.session_id, 2)


if __name__ == "__main__":
    unittest.main()
