import unittest

from discord_spoken_math import normalize_math_question


class SpokenMathTests(unittest.TestCase):
    def test_spoken_numbers_keep_original_operator_and_question(self):
        cases = {
            '칠 더하기 오는 얼마야?': '7 더하기 5는 얼마야?',
            '칠더하기오는얼마야': '7더하기5는얼마야',
            '칠 더하기 오 는 얼마야?': '7 더하기 5 는 얼마야?',
            ' 구 빼기 사는 몇이야? ': ' 9 빼기 4는 몇이야? ',
            '둘 곱하기 서른넷은 몇일까요？': '2 곱하기 34은 몇일까요？',
            '삼십육 나누기 여섯 하면 얼마예요.': '36 나누기 6 하면 얼마예요.',
            '공 더하기 12는 얼마야?': '0 더하기 12는 얼마야?',
            '7 더하기 5는 얼마야?': '7 더하기 5는 얼마야?',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(normalize_math_question(text), expected)

    def test_homophones_codes_dates_prices_and_complex_expressions_survive(self):
        cases = ['대한민국의 수도는 어디야?', '칠 더하기 오는 건 언제야?', '오늘 오는 친구는 몇이야?',
                 '칠 더하기 오', '둘을 더하면 몇이야?', '이 코드는 007 더하기 5는 얼마야?',
                 '007 더하기 오는 얼마야?', '칠 월 오 일은 언제야?', '이백 원 더하기 오 원은 얼마야?',
                 '칠 더하기 오 더하기 구는 얼마야?', '1.2 더하기 5는 얼마야?', '마이너스 오 빼기 사는 몇이야?',
                 '검토할 문장: 칠 더하기 오는 얼마야?', '쟤가 칠 더하기 오는 얼마야?라고 했어.', '잠깐만']
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(normalize_math_question(text), text)
