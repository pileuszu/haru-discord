"""Keep spoken Korean operands unambiguous when a Discord user asks for a calculation."""
import re


_SINO = ('', '일', '이', '삼', '사', '오', '육', '칠', '팔', '구')
_NATIVE = ('', '하나', '둘', '셋', '넷', '다섯', '여섯', '일곱', '여덟', '아홉')
_TENS = ('', '열', '스물', '서른', '마흔', '쉰', '예순', '일흔', '여든', '아흔')
_NUMBERS = {'영': '0', '공': '0'}
for value in range(1, 100):
    tens, units = divmod(value, 10)
    prefix = (_SINO[tens] if tens > 1 else '') + '십' if tens else ''
    _NUMBERS[prefix + _SINO[units]] = str(value)
    _NUMBERS[_TENS[tens] + _NATIVE[units]] = str(value)

_NUMBER = '(?:' + '|'.join(sorted(_NUMBERS, key=len, reverse=True)) + '|0|[1-9][0-9]?)'
_QUESTION = re.compile(
    rf'(?P<lead>[ \t]*)(?P<left>{_NUMBER})(?P<operator>[ \t]*(?:더하기|빼기|곱하기|나누기)[ \t]*)'
    rf'(?P<right>{_NUMBER})(?P<tail>[ \t]*(?:은|는|이|가|하면)?[ \t]*'
    r'(?:얼마|몇)(?:야|이야|인가요|예요|이에요|일까|일까요|입니까)?[?？!.。]?[ \t]*)'
)


def normalize_math_question(text):
    """Change only the two number spans in a complete standalone arithmetic question."""
    match = _QUESTION.fullmatch(text)
    if match is None:
        return text
    left, right = match.group('left', 'right')
    return ''.join((match['lead'], _NUMBERS.get(left, left), match['operator'],
                    _NUMBERS.get(right, right), match['tail']))
