#!/usr/bin/env python3
"""증권사 라벨/브랜드 → 정규 증권사명. 하드코딩 매핑 없음 — 검색으로 도출.

증권사 앱 화면의 broker 라벨은 세 형태로 나온다:
  1) 정규명 그대로       "삼성증권"                 → 그대로 채택
  2) 브랜드/상품명        "[Super365]"               → 웹검색으로 정규사 확정(예: 메리츠증권)
  3) 계좌번호/계좌별칭    "7174376991-29", "[ISA…]"  → 라벨만으론 불가 → 크로스-스크린(finalize)에서
                                                       같은 앱 요약화면의 정규사를 상속(여기선 None 반환)

검색은 GT를 만들 때와 동일한 방식('추출 후 서치'): naver 검색 결과 텍스트를 근거로
로컬 LLM이 정규사명을 읽어낸다(모델의 틀린 기억이 아니라 검색결과 기반). LLM 미가용/실패 시
검색결과의 최빈 'XX증권' 토큰으로 폴백. 확정 결과는 캐시(파생 데이터, 레포 밖).
"""
import collections, json, os, re, urllib.parse, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
      "Accept-Language": "ko,en;q=0.9"}
OLLAMA = os.environ.get("OLLAMA", "http://127.0.0.1:11434") + "/api/generate"
MODEL = os.environ.get("MODEL", "qwen2.5vl:7b")
DATA_DIR = os.environ.get("DATA_DIR", os.path.expanduser("~/portf-agent/data"))
CACHE_PATH = os.path.join(DATA_DIR, "broker_cache.json")

# 계좌별칭/유형 토큰: 브랜드가 아니라 계좌 성격 → 검색 대상 아님(크로스-스크린으로)
_ACCT_HINT = ("연금", "IRP", "ISA", "퇴직", "CMA", "중개형", "비대면", "저축")
_STOP = {"이", "그", "한", "본", "저", "위", "및", "해당", "관련", "증권사"}


def canonical_in(text):
    """텍스트 속 '○○증권' 정규명(첫 매치) 또는 None. '증권사명' 같은 placeholder는 안 잡힘."""
    if not text:
        return None
    m = re.search(r"([가-힣A-Za-z]{2,10})증권", text)
    if not m or m.group(1) in _STOP:
        # placeholder/불용어 앞이면 더 뒤에서 재시도
        for mm in re.finditer(r"([가-힣A-Za-z]{2,10})증권", text):
            if mm.group(1) not in _STOP:
                return mm.group(1) + "증권"
        return None
    return m.group(1) + "증권"


def brand_token(label):
    """라벨에서 검색 가능한 브랜드 토큰 추출. 계좌번호·계좌별칭이면 None."""
    if not label:
        return None
    s = label.strip().strip("[]() ").strip()
    if not s:
        return None
    if re.fullmatch(r"[\d][\d\-]+", s):            # 계좌번호
        return None
    if any(h in s for h in _ACCT_HINT):            # 계좌별칭(연금/IRP/ISA…)
        return None
    return s


def _search_text(query, timeout=15):
    url = "https://search.naver.com/search.naver?query=" + urllib.parse.quote(query)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        h = r.read().decode("utf-8", "ignore")
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)).strip()


def _freq_broker(text):
    """검색결과 텍스트에서 최빈 'XX증권' 정규명(불용어 제외) 또는 None."""
    c = collections.Counter(m.group(1) + "증권"
                            for m in re.finditer(r"([가-힣A-Za-z]{2,10})증권", text)
                            if m.group(1) not in _STOP)
    return c.most_common(1)[0][0] if c else None


def _llm_broker(brand, text, timeout=90):
    """검색결과 텍스트를 근거로 로컬 LLM이 정규사명 추출. 실패 시 None."""
    prompt = (f"아래는 '{brand}' 관련 한국 웹 검색 결과 텍스트다.\n---\n{text[:2500]}\n---\n"
              f"이 검색 결과에만 근거해서 '{brand}'을(를) 운영·제공하는 한국 증권회사의 정식 명칭"
              f"(예: ○○증권)만 한 줄로 답하라. 검색결과에서 확인 불가하면 UNKNOWN.")
    req = json.dumps({"model": MODEL, "prompt": prompt, "stream": False,
                      "options": {"temperature": 0}}).encode()
    try:
        r = urllib.request.Request(OLLAMA, data=req, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            out = json.loads(resp.read()).get("response", "")
        return canonical_in(out)
    except Exception:
        return None


def search_broker(brand, use_llm=True):
    """브랜드 토큰 → 정규 증권사명(검색 기반) 또는 None."""
    try:
        text = _search_text(f"{brand} 어느 증권사")
    except Exception:
        return None
    name = _llm_broker(brand, text) if use_llm else None
    return name or _freq_broker(text)


def load_cache(path=CACHE_PATH):
    try:
        return json.load(open(path))
    except Exception:
        return {}


def save_cache(cache, path=CACHE_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(cache, open(path, "w"), ensure_ascii=False, indent=2)


def resolve_broker(label, cache=None, use_llm=True):
    """broker 라벨 → 정규 증권사명 또는 None(라벨만으론 불가 → 크로스-스크린 상속 필요).
    cache: 브랜드 토큰(소문자) → 정규명 dict(옵션)."""
    c = canonical_in(label)
    if c:
        return c
    bt = brand_token(label)
    if not bt:
        return None
    key = bt.lower()
    if cache is not None and key in cache:
        return cache[key]
    name = search_broker(bt, use_llm=use_llm)
    if cache is not None and name:
        cache[key] = name
    return name


if __name__ == "__main__":
    import sys
    cache = load_cache()
    for lab in (sys.argv[1:] or ["삼성증권", "[Super365]", "7174376991-29",
                                 "[ISA(평생혜택 중개형)(비대면)]"]):
        print(f"{lab:34} -> {resolve_broker(lab, cache)}")
    save_cache(cache)
