"""Recognize a Discord address and one timely follow-up by the same participant."""
import re
from time import monotonic
import unicodedata


WAKE_WINDOW = 5.0
_SEPARATOR = r"[\s,.!?…~，。！？、:：—–-]"
_ADDRESS = re.compile(
    rf"^(?:안녕{_SEPARATOR}+)?(?:하루\s*(?:야|찡|징|짱)|하룻\s*(?:짱|장)|"
    rf"하루\s+층(?={_SEPARATOR}*$)|(?P<bare>하루))"
    rf"(?P<separator>{_SEPARATOR}+|$)"
)
_QUOTED = re.compile(r"^라(?:고|는)(?:\s|$|말|불|하)")
# With no vocative punctuation, these continuations usually describe a day.
_DAY_PHRASE = re.compile(r"^(?:종일|동안|정도|이틀|씩|쯤|야근|만[에의큼]|전[날에]?\b|뒤[에]?\b|"
                         r"(?:한|두|세|네|몇|\d+)\s*(?:번|끼|시간))")


class WakeGate:
    def __init__(self, capacity):
        self.deadlines = {}
        self.capacity = capacity

    def address(self, speaker, text, *, started_at=None, ended_at=None):
        """None means ignore, an empty string arms the next turn, otherwise return the request."""
        text = unicodedata.normalize("NFC", text).strip()
        if not text:
            return None
        deadline = self.deadlines.pop(speaker, None)
        start = monotonic() if started_at is None else started_at
        armed = deadline is not None and start <= deadline
        match = _ADDRESS.match(text)
        if not match:
            return text if armed else None
        request = text[match.end():]
        if _QUOTED.match(request):
            return None
        if match["bare"] and match["separator"].isspace() and _DAY_PHRASE.match(request):
            return text if armed else None
        if not request:
            if len(self.deadlines) >= self.capacity:
                del self.deadlines[next(iter(self.deadlines))]
            self.deadlines[speaker] = (monotonic() if ended_at is None else ended_at) + WAKE_WINDOW
        return request
