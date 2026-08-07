#!/usr/bin/env python3
"""이름 → 시세 심볼 해석. 하드코딩 없음 — 전부 동적 조회.

- 국내(KRW): Naver 종목 autocomplete로 이름 → 6자리 코드 + 시장(KOSPI→.KS / KOSDAQ→.KQ).
  신형 영숫자 코드(0053L0 등)·사명변경(엔씨소프트→"NC")도 처리. 정확 일치를 우선.
- 미국(USD): 이름 속 괄호 티커 '알파벳 A (GOOGL)' 또는 티커 그대로 'VOO'.
- 현금성(예수금·CMA 등): 시세 대상 아님 → None.

해석 결과는 Yahoo chart 엔드포인트가 그대로 받는 심볼이다(measure: 2026-07-09 26/26 커버).

**디스크 캐시는 없다(규칙).** 이름을 심볼로 푸는 수단은 검색이지 저장이 아니다. 저장하면
"검색이 항상 동작하는가"를 가린다 — 증권사 쪽에서 실제로 그 일이 났다(`resolve_broker`
docstring). 한 실행 안에서 같은 이름이 여러 번 나오면 호출부의 **메모 dict**로 한 번만
질의한다(휘발성). 자동완성은 실측 5/5·0.05s라 재질의 비용이 문제되지 않는다.

CLI:  python3 agent/resolve.py "TIGER 차이나휴머노이드로봇"  KODEX...  VOO
"""
import json, os, re, sys, time, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import outbound                                     # noqa: E402  외부망 단일 통로
CASH_KEYS = ("예수금", "현금", "예금", "잔고", "CMA", "deposit")


def _get(url, timeout=15):
    return json.loads(outbound.get("symbol", url, timeout))    # 아웃바운드는 outbound.py 한 곳


def is_cash(name):
    return any(k in name for k in CASH_KEYS)


def us_ticker(name, allow_leading=False):
    """'메타 플랫폼스 (META)'→META, 'VOO'→VOO, (USD면) 'AAPL 애플'→AAPL, 아니면 None.
    allow_leading은 통화가 USD로 확인됐을 때만 켠다 — 'KODEX 미국S&P500'의 KODEX 오인 방지."""
    name = (name or "").strip()
    m = re.search(r"\(([A-Z][A-Z0-9.]{0,5})\)", name)   # 괄호 티커
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Z][A-Z0-9.]{0,5}", name):      # 단독 티커
        return name
    if allow_leading:                                    # 선두 티커 토큰 (USD 한정)
        m = re.match(r"([A-Z][A-Z0-9.]{0,5})\s+\S", name)
        if m:
            return m.group(1)
    return None


def _norm(s):
    return re.sub(r"[\s·\-_.,()]", "", str(s or "")).lower()


def _script_pieces(s):
    """한글↔라틴 전환 지점에서 쪼갠 조각 — OCR이 **공백을 잃어도** 토큰 폴백이 돌게 한다.

    측정(2026-08-06): 자동완성은 `알파벳A`도 `알파벳 A`도 못 풀고 `알파벳`만 GOOGL을 준다.
    즉 공백 자체가 근거인 게 아니라, 아래 질의 폴백이 **공백에서만** 토큰을 쪼개는 것이 원인이다.
    그리고 rec 모델은 이 공백을 4종 전부 잃는다(korean v4/v5, ch v5) — 그러니 소비자 쪽에서
    쪼갠다. 상수 없는 문자 종류 규칙이라 특정 이름에 맞춘 것이 아니다.

    한 글자짜리 조각은 버린다(`A`처럼 질의로 의미가 없고 오매칭만 만든다)."""
    return [p for p in re.findall(r"[가-힣]+|[A-Za-z][A-Za-z0-9.&]*", str(s or ""))
            if len(p) > 1]


# 글리프 혼동류 — 같은 잉크를 내는 문자들. 근거(측정, 2026-08-06): 화면의 `IVV`가 커닝 때문에
# `IWV`의 잉크와 구분되지 않고, rec 모델 4종·8배 확대까지 전부 같은 답을 낸다. 즉 **문자열
# 층에는 정보가 없다.** 그래서 후보를 만들어 두고 판정은 시세(외부 근거)에 맡긴다.
_CONFUSE = ("VWUY", "Il1T", "O0DQ", "S5", "B8", "G6C", "Z2", "PR", "EF", "MN", "XK")
_CLASS = {c: cls for cls in _CONFUSE for c in cls}
_REPEAT = "VWIl1"          # 획 반복으로 개수가 흔들리는 글자 (VV↔W, Il↔II)
MAX_VARIANTS = 48          # 후보당 자동완성 1회 — 기각된 행에서만 도는 비용의 상한


def confusable_variants(token):
    """식별자 토큰 → **편집거리 1** 혼동 후보들(원본 제외, 안정 정렬).

    치환·삭제·삽입 전부 위 혼동류 **안에서만** 일어난다. 임의 편집이 아니므로 후보 수가
    토큰 길이에 선형이고(3~4자 티커면 10~20개), 실재 티커가 아닌 것은 자동완성이 걸러낸다.
    `IWV` → `W`를 같은 류의 `V`로 치환 → **`IVV`**가 후보에 든다.

    이 함수는 시세도 심볼도 모른다 — 순수 문자열 함수다(순환 없음, 테스트 쉬움)."""
    t = str(token or "")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,5}", t):
        return []                       # 국내 6자리 코드·한글명은 이 실패 모드가 아니다
    out = []
    for i, c in enumerate(t):
        for alt in _CLASS.get(c, ""):   # 치환
            if alt != c:
                out.append(t[:i] + alt + t[i + 1:])
        if i and c in _CLASS and t[i - 1] in _CLASS.get(c, ""):
            out.append(t[:i] + t[i + 1:])          # 삭제 — 같은 류가 붙어 있을 때만
        if c in _REPEAT:
            out.append(t[:i] + c + t[i:])          # 삽입 — 획 반복 글자를 하나 더
    seen, uniq = {t}, []
    for v in out:
        if v not in seen:
            seen.add(v); uniq.append(v)
    return uniq[:MAX_VARIANTS]


def naver_resolve(name):
    """이름 → (symbol, market). 실패 시 (None, None).
    **국내·미국 모두** 자동완성으로 해석한다(한국 앱은 미국주식도 한글명으로 표기한다:
    '엔비디아'→NVDA, '팔란티어 테크'→PLTR). 통화 힌트는 믿지 않는다 — 비전 모델이 해외주식을
    KRW로 표기하는 일이 흔하다(화면의 평가금액이 원화라서).
    질의 폴백: 원문 → 괄호 제거 → 첫 토큰 → **스크립트 경계 조각**. 이름 정확·접두 일치를
    우선(오매칭 방지). 조각은 **맨 뒤**에 붙인다 — 앞의 질의로 이미 풀리는 이름의 결과를
    바꾸지 않기 위해서다(회귀 방지)."""
    base = re.sub(r"\(.*?\)", "", name).strip()
    tok = base.split()
    tok = tok[0] if tok else ""
    queries, seen = [], set()
    for q in (name, base, tok, *_script_pieces(base)):
        if q and q not in seen:
            seen.add(q); queries.append(q)
    want = {_norm(name), _norm(base)}
    for q in queries:
        try:
            d = _get("https://ac.stock.naver.com/ac?" +
                     urllib.parse.urlencode({"q": q, "target": "stock"}))
        except Exception:
            continue
        items = [it for it in d.get("items", []) if it.get("code")]
        if not items:
            continue
        exact = [it for it in items if _norm(it.get("name")) in want]
        pref = [it for it in items if _norm(it.get("name")).startswith(tuple(w for w in want if w))]
        it = (exact or pref or items)[0]
        if it.get("nationCode") == "KOR":
            return it["code"] + (".KS" if "KOSPI" in it.get("typeCode", "") else ".KQ"), "KOSPI"
        return it["code"], "US"          # 미국: code가 곧 티커(NVDA·PLTR·META…)
    return None, None


def resolve(name, currency=None, memo=None):
    """이름 → {'symbol','market','source'} 또는 None(현금/해석실패).
    memo: name→record dict. **한 실행 안에서만 사는 메모다**(디스크 캐시 없음, 모듈 docstring).
    currency 힌트가 있으면 미국/국내 분기에 사용."""
    name = (name or "").strip()
    if not name or is_cash(name):
        return None
    if memo is not None and name in memo:
        return memo[name]
    rec = None
    m = re.search(r"\(([A-Z][A-Z0-9.]{0,5})\)", name)   # 괄호 티커 '알파벳 A (GOOGL)' — 명시적 근거
    if m:
        rec = {"symbol": m.group(1), "market": "US", "source": "ticker"}
    if rec is None:      # 이름 검색(국내·미국) — 티커 추측보다 앞: 'NC'는 미국 티커가 아니라 엔씨소프트다
        sym, mkt = naver_resolve(name)
        if sym:
            rec = {"symbol": sym, "market": mkt, "source": "naver"}
    t = us_ticker(name, allow_leading=(currency == "USD"))
    if rec is None and t:  # 검색이 실패했을 때만 티커 형태로 폴백
        rec = {"symbol": t, "market": "US", "source": "ticker"}
    if memo is not None and rec is not None:
        memo[name] = rec
    return rec


if __name__ == "__main__":
    names = sys.argv[1:] or ["SK하이닉스", "NC(엔씨소프트)", "TIGER 차이나휴머노이드로봇",
                             "KODEX 미국S&P500", "알파벳 A (GOOGL)", "VOO", "원화예수금"]
    for n in names:
        r = resolve(n)
        print(f"{n:30} -> {r}")
        time.sleep(0.15)
