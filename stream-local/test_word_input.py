import unittest

from server import LiveInput


class ByteTokenizer:
    """Multi-token words, including Korean syllables and emoji."""

    def __init__(self):
        self.words = []

    def encode(self, word):
        self.words.append(word)
        return list(word.encode("utf-8"))


class WordInputTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.draft = LiveInput(clock=lambda: self.now)
        self.tokenizer = ByteTokenizer()

    def drain(self):
        ids = []
        while (token := self.draft.next_token(self.tokenizer)) is not None:
            ids.append(token)
            self.draft.acknowledge()
        return ids

    def test_typing_matches_reference_words_without_enter(self):
        text = " What  is\n한국 수도 어디야🤔"
        ids = []
        for n in range(1, len(text) + 1):
            self.now += 0.12
            self.draft.update(text[:n])
            ids.extend(self.drain())
        self.assertEqual(self.draft.confirmed, " What  is\n한국 수도 ")
        self.now += 0.61
        ids.extend(self.drain())
        words = [" " + word for word in text.split()]
        self.assertEqual(self.tokenizer.words, words)
        self.assertEqual(bytes(ids).decode(), "".join(words))
        self.assertEqual(self.draft.confirmed, text)

    def test_emoji_in_flight_locks_source_until_all_byte_tokens_arrive(self):
        self.draft.update("🤔? 다음")
        first = self.draft.next_token(self.tokenizer)
        self.assertIsNotNone(first)
        self.draft.acknowledge()
        self.assertEqual(self.draft.confirmed, "")
        self.assertIsNotNone(self.draft.update("😊? 다음"))
        self.assertIsNone(self.draft.update("🤔? 새질문"))
        self.drain()
        self.assertEqual(self.draft.confirmed, "🤔? ")

    def test_editable_tail_and_terminal_flush(self):
        self.draft.update("어딤")
        self.assertIsNone(self.draft.next_token(self.tokenizer))
        self.draft.update("어디야?")
        self.assertTrue(self.drain())
        self.assertEqual(self.tokenizer.words, [" 어디야?"])
        self.assertEqual(self.draft.confirmed, "어디야?")

    def test_resuming_inside_a_flushed_word_does_not_add_a_space(self):
        self.draft.update("super")
        self.now += 0.61
        self.drain()
        self.draft.update("superman!")
        self.drain()
        self.assertEqual(self.tokenizer.words, [" super", "man!"])
        self.assertEqual(self.draft.confirmed, "superman!")

    def test_trailing_whitespace_needs_no_model_token(self):
        self.draft.update("Hi!")
        self.drain()
        self.draft.update("Hi!\n  ")
        self.assertFalse(self.draft.has_words_pending())
        self.assertEqual(self.drain(), [])
        self.assertEqual(self.draft.confirmed, "Hi!\n  ")
        self.draft.update("Hi!\n  Bye.")
        self.drain()
        self.assertEqual(self.tokenizer.words, [" Hi!", " Bye."])


if __name__ == "__main__":
    unittest.main()
