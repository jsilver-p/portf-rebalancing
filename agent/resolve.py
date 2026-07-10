#!/usr/bin/env python3
"""이름 → 시세 심볼 해석. 하드코딩 없음 — 전부 동적 조회.

- 국내(KRW): Naver 종목 autocomplete로 이름 → 6자리 코드 + 시장(KOSPI→.KS / KOSDAQ→.KQ).
  신형 영숫자 코드(0053L0 등)·사명변경(엔씨소프트→"NC")도 처리. 정확 일치를 우선.
- 미국(USD): 이름 속 괄호 티커 '알파벳 A (GOOGL)' 또는 티커 그대로 'VOO'.
- 현금성(예수금·CMA 등): 시세 대상 아님 → None.

해석 결과는 Yahoo chart 엔드포인트가 그대로 받는 심볼이다(measure: 2026-07-09 26/26 커버).
결과는 선택적으로 캐시(name→symbol)한다 — 파생 데이터일 뿐이라 언제든 갱신 가능.

CLI:  python3 agent/resolve.py "TIGER 차이나휴머노이드로봇"  KODEX...  VOO
"""
import json, os, re, sys, time, urllib.parse, urllib.request

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
CACHE_PATH = os.path.join(os.path.dirname(__file__), ".symbol-cache.json")
CASH_KEYS = ("예수금", "현금", "예금", "잔고", "CMA", "deposit")


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


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


def naver_resolve(name):
    """국내 이름 → (symbol, market_name). 실패 시 (None, None).
    질의 폴백: 원문 → 괄호 제거 → 첫 토큰. 각 응답에서 이름 정확 일치를 우선 선택."""
    base = re.sub(r"\(.*?\)", "", name).strip()
    tok = name.split("(")[0].split()
    tok = tok[0] if tok else ""
    queries, seen = [], set()
    for q in (name, base, tok):
        if q and q not in seen:
            seen.add(q); queries.append(q)
    for q in queries:
        try:
            d = _get("https://ac.stock.naver.com/ac?" +
                     urllib.parse.urlencode({"q": q, "target": "stock"}))
        except Exception:
            continue
        items = [it for it in d.get("items", []) if it.get("nationCode") == "KOR"]
        if not items:
            continue
        exact = [it for it in items if it.get("name") in (name, base)]
        it = (exact or items)[0]
        suffix = ".KS" if "KOSPI" in it.get("typeCode", "") else ".KQ"
        return it["code"] + suffix, it.get("typeName")
    return None, None


def resolve(name, currency=None, cache=None):
    """이름 → {'symbol','market','source'} 또는 None(현금/해석실패).
    cache: name→record dict(옵션). currency 힌트가 있으면 미국/국내 분기에 사용."""
    name = (name or "").strip()
    if not name or is_cash(name):
        return None
    if cache is not None and name in cache:
        return cache[name]
    rec = None
    # 미국: 통화가 USD면 선두 티커 토큰까지 허용, 아니면 괄호/단독 티커만
    t = us_ticker(name, allow_leading=(currency == "USD"))
    if t and (currency == "USD" or currency is None):
        rec = {"symbol": t, "market": "US", "source": "ticker"}
    if rec is None and currency != "USD":
        sym, mkt = naver_resolve(name)
        if sym:
            rec = {"symbol": sym, "market": mkt, "source": "naver"}
    if rec is None and t:  # USD 힌트 없이도 티커 폴백
        rec = {"symbol": t, "market": "US", "source": "ticker"}
    if cache is not None and rec is not None:
        cache[name] = rec
    return rec


def load_cache(path=CACHE_PATH):
    try:
        return json.load(open(path))
    except Exception:
        return {}


def save_cache(cache, path=CACHE_PATH):
    json.dump(cache, open(path, "w"), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    names = sys.argv[1:] or ["SK하이닉스", "NC(엔씨소프트)", "TIGER 차이나휴머노이드로봇",
                             "KODEX 미국S&P500", "알파벳 A (GOOGL)", "VOO", "원화예수금"]
    for n in names:
        r = resolve(n)
        print(f"{n:30} -> {r}")
        time.sleep(0.15)
