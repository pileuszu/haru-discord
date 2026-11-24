"""Choose when to search and turn the latest addressed request into a public query."""
import asyncio
from datetime import datetime
import json
import logging
import re
from time import monotonic

from aiohttp import ClientError


LOG = logging.getLogger("discord-search")
NO_SEARCH = re.compile(
    r"(?:검색|구글링|조회)(?:은|을|은요)?\s*(?:하?지\s*(?:마|말|않)|없이|말고|빼고|금지|생략|"
    r"안\s*(?:해|하)|할\s*필요\s*없)|(?:찾아\s*보|찾)지\s*(?:마|말)|"
    r"(?:인터넷|웹)(?:을|은)?\s*(?:없이|쓰지\s*(?:마|말)|사용하지\s*(?:마|말))|"
    r"(?:로컬|오프라인)(?:으로|로)(?:만\s*(?:답|대답|말|해)|\s*(?:답|대답)해)|"
    r"(?:do not|don't|don’t|never)\s+(?:search|google|look up|browse|use the internet)|without\s+(?:searching|browsing)", re.I)
PRIVATE = re.compile(r"[@<>\[\]{}]|https?://|www\.|(?:\d[ -]?){8,}|비밀\s*번호|전화\s*번호|"
                     r"이메일|우리\s*집|내\s*(?:집\s*주소|주소|계정|토큰|연락처)|password|api[ _-]?key", re.I)
TERM = r"[가-힣a-zA-Z0-9]+(?:[.+'-][가-힣a-zA-Z0-9]+)*"
QUERY = re.compile(rf"{TERM}(?: {TERM}){{0,8}}")
# A local reformulation can name the field the speaker implied ("우산" → "날씨").
# New entities, locations, dates and numbers must still come from the utterance.
PUBLIC_FIELDS = frozenset("""
날씨 예보 일기예보 강수확률 강수량 기온 미세먼지 강수 확률 일기 예보
운영시간 개관시간 개방시간 영업시간 관람시간 이용시간 입장시간 휴관일 휴무일 입장료 예약 입장 관람
운영 영업 개관 이용 가능 여부 시간 요금 가격 마감 개장 폐장
일정 운행시간 소요시간 결과 점수 순위 시세 환율 버전 공식 문서 보도 뉴스
weather forecast rain hours opening admission availability schedule price score results
""".split())
TODAY_NEWS = re.compile(
    r"(?:오늘|금일)(?:(?![,.!?，。！？]|는데|지만).){0,50}(?:뉴스|속보|헤드라인|보도|소식|기사)|"
    r"\btoday(?:'s|’s)?\b.{0,40}\b(?:news|headlines?)\b|\b(?:news|headlines?)\b.{0,20}\btoday\b", re.I)
# DIRECT is one short decision. WEB and NEWS carry their query in the same generation.
GRAMMAR = r'''root ::= "DIRECT" | ("WEB " | "NEWS ") term (" " term){0,8}
term ::= [가-힣a-zA-Z0-9]+ ([.+'-] [가-힣a-zA-Z0-9]+)*'''
GUIDE = (
    "너는 하루의 도구 선택기야. 친구의 마지막 말에 지금 인터넷 자료가 도움이 되는지 뜻을 보고 결정해. "
    "대신 대답하지 말고 DIRECT, WEB 검색어, NEWS 검색어 중 하나만 출력해. "
    "DIRECT: 잡담, 감정, 계산, 안정적인 기초 지식, 창작, 추천 아이디어는 바로 대답. "
    "WEB: 명시적으로 찾아 달라거나, 특정 장소의 이용 조건이나 운영 일정, 예보, 경기 결과, "
    "시세처럼 변하거나 정확한 확인이 필요한 공개 사실은 검색. NEWS: 최근 기사, 보도된 사건이나 헤드라인을 요청하면 뉴스 검색. "
    "외부 조회를 거절하거나 경험담, 개념 설명을 말하면 뉴스나 날씨 단어가 있어도 DIRECT. "
    "검색어는 상대가 실제 말한 공개 주제와 지역, 시점을 살려. 필요한 일반 항목(날씨, 운영시간)은 덧붙여도 돼. "
    "조사, 부탁 표현, 개인 사연이나 식별자는 빼. "
    "예: '이사해서 정신없는데 국립중앙박물관 몇 시에 닫아?' → WEB 국립중앙박물관 몇 시 닫아. "
    "'무슨 일 있었는지 최근 보도 좀 알려줘, 한국은행 기준금리 말이야' → NEWS 한국은행 기준금리. "
    "'오늘 힘들었어' → DIRECT."
)
UNAVAILABLE = "지금은 검색 결과를 확인하지 못했어. 이 조회가 필요한 수치나 사실은 추측하지 마."
UNCERTAIN = "외부 자료를 조회하지 못했어. 일상 대화에는 평소처럼 답하고 현재 정보나 출처가 필요한 부분은 확인하지 못했다고 말해."
OPT_OUT = "사용자가 외부 검색을 원하지 않아 조회하지 않았어. 알고 있는 내용으로 답하되 현재 수치나 변동 사실은 추측하지 마."
PRIVATE_REQUEST = "공개 검색어를 분리할 수 없어 조회하지 않았음. 개인 정보는 그대로 두고 필요한 경우 공개 주제만 물어봐."


async def decide_search(text, backend, *, slot=0):
    """Return the local model's action and its validated, standalone public keywords."""
    client, url, model = backend
    messages = [{"role": "system", "content": GUIDE}, {"role": "user", "content": text}]
    async with client.post(url + "/v1/chat/completions", allow_redirects=False,
                           json={"model": model, "id_slot": slot, "messages": messages, "temperature": 0, "grammar": GRAMMAR,
                                 "max_tokens": 54, "chat_template_kwargs": {"enable_thinking": False}}) as response:
        response.raise_for_status()
        choice = (await response.json())["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError("Incomplete search decision")
    reply = choice["message"]["content"]
    if not isinstance(reply, str):
        raise ValueError("Invalid search decision")
    action, _, query = reply.strip().partition(" ")
    if action == "DIRECT" and not query:
        return action, ""
    if action not in {"WEB", "NEWS"} or len(query) > 100 or not QUERY.fullmatch(query) or PRIVATE.search(query):
        raise ValueError("Invalid search query")
    # Korean ASR may split a compound, for instance '기준 금리' for '기준금리'.
    source = "".join(text.casefold().split())
    words = query.casefold().split()
    if (not any(word in source for word in words)
            or any(word not in source and word not in PUBLIC_FIELDS for word in words)):
        raise ValueError("Query includes terms absent from the addressed request")
    # The RSS provider interprets this explicit date as a KST calendar day.
    # Preserve that scope when the model returned only the topic of today's news.
    if action == "NEWS" and TODAY_NEWS.search(text) and not re.search(r"오늘|\btoday\b", query, re.I):
        query += " 오늘" if re.search(r"오늘|금일", text) else " today"
    return action, query


async def search_context(text, backend, *, on_search=None, slot=0):
    if not text.strip():
        return ""
    if NO_SEARCH.search(text):
        return OPT_OUT
    if len(text) > 400 or PRIVATE.search(text):
        return PRIVATE_REQUEST
    from discord_search_providers import lookup

    start = monotonic()
    try:
        async with asyncio.timeout(4):
            action, query = await decide_search(text, backend, slot=slot)
    except (TimeoutError, ClientError, OSError, ValueError, TypeError, KeyError, IndexError):
        LOG.info("web_search decision=unavailable elapsed_ms=%d", (monotonic() - start) * 1000)
        return UNCERTAIN
    LOG.info("web_search decision=%s elapsed_ms=%d", action, (monotonic() - start) * 1000)
    if action == "DIRECT":
        return ""
    if on_search is not None:
        await on_search()
    try:
        provider, rows = await lookup(query, news=action == "NEWS")
    except (TimeoutError, ClientError, OSError, ValueError):
        LOG.info("web_search status=failed elapsed_ms=%d", (monotonic() - start) * 1000)
        return UNAVAILABLE
    LOG.info("web_search provider=%s results=%d elapsed_ms=%d", provider, len(rows), (monotonic() - start) * 1000)
    if not rows:
        return UNAVAILABLE
    evidence = [{key: row[key] for key in ("title", "snippet", "source", "date") if row.get(key)} for row in rows[:4]]
    scope = ("뉴스의 제목·발행처·게시 날짜(한국 기준)만 확인했어. 보도된 날과 사건 날짜를 구분해. "
             "게시 시각은 알 수 없어. 지난 기사에서 말한 '다음 주'를 오늘 기준으로 옮기지 마.\n"
             if provider == "google_news_rss" else "")
    return scope + f"조회 시각 {datetime.now().astimezone().isoformat(timespec='minutes')}\n" + json.dumps(evidence, ensure_ascii=False)
