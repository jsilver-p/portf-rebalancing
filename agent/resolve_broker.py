#!/usr/bin/env python3
"""증권사 라벨/브랜드 → 정규 증권사명. 하드코딩 매핑 없음 — 검색으로 도출.

증권사 앱 화면의 broker 라벨은 세 형태로 나온다:
  1) 정규명 그대로       "삼성증권"                 → 그대로 채택
  2) 브랜드/상품명        "[Super365]"               → 웹검색으로 정규사 확정(예: 메리츠증권)
  3) 계좌번호/계좌별칭    "1234567890-01", "[ISA…]"  → 라벨만으론 불가 → 크로스-스크린(finalize)에서
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

# 스키마 자리표시·일반명사: 비전 모델이 값을 못 읽으면 프롬프트의 필드 설명을 그대로 뱉는다.
# 그걸 브랜드로 착각해 웹검색하면 검색결과의 최빈 증권사가 '정답'으로 둔갑한다(실제 오염 사례:
# "증권사명" → 한국투자증권). 브랜드가 아니라 '값 없음'이므로 검색 금지 → None → 크로스-스크린 상속.
_PLACEHOLDER = {"증권사명", "증권사", "브랜드", "브랜드명", "계좌", "계좌명", "계좌유형", "종목명",
                "상품명", "이름", "null", "none", "n/a", "na", "-", "미상", "알수없음", "unknown",
                "broker", "brokername", "string", "값", "없음"}


def is_placeholder(s):
    """라벨이 실제 값이 아니라 스키마 자리표시/일반명사인가."""
    t = re.sub(r"[\s\[\]()·\-_.]", "", str(s or "")).lower()
    return (not t) or t in _PLACEHOLDER


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
    """라벨에서 검색 가능한 브랜드 토큰 추출. 계좌번호·계좌별칭·자리표시면 None."""
    if not label or is_placeholder(label):         # 자리표시/일반명사 → 검색 금지(오염 차단)
        return None
    s = str(label).strip()
    m = re.match(r"\[([^\]]+)\]", s)               # '[브랜드] 1234-5678-90' → 브랜드는 대괄호 안
    if m:
        s = m.group(1).strip()
    s = re.sub(r"[\d][\d\-]{5,}", " ", s)          # 라벨에 붙은 계좌번호 제거
    s = s.strip("[]() ").strip()
    if not s or is_placeholder(s):
        return None
    # 브랜드명은 짧다. 화면 제목·문장('보유계좌 상품별 자산현황')을 검색에 태우면 검색결과에서
    # 엉뚱한 '○○증권'을 주워온다(유령 증권사). 브랜드 형태가 아니면 검색하지 않는다.
    if len(s) > 20 or len(s.split()) > 3:
        return None
    if re.fullmatch(r"[\d][\d\-\s]+", s):          # 계좌번호
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


def is_broker_name(s):
    """정규 증권사명 형태인가(예: 메리츠증권·삼성증권). 검색결과에서 주워온 쓰레기를 걸러낸다."""
    return bool(re.fullmatch(r"[가-힣A-Za-z]{2,6}증권", str(s or "")))


def search_broker(brand, use_llm=True):
    """브랜드 토큰 → 정규 증권사명(검색 근거) 또는 None.

    **근거 없으면 답하지 않는다.** 예전엔 LLM이 확정 못하면 검색결과의 '최빈 ○○증권'으로 폴백했는데,
    그러면 브랜드가 아닌 문구(화면 제목·일반명사)를 검색했을 때 아무 증권사나 정답으로 둔갑한다
    (실제 오염: '보유계좌'→신한투자증권). 최빈 폴백은 LLM이 없을 때만, 그것도 형태 검증을 통과할 때만."""
    try:
        text = _search_text(f"{brand} 어느 증권사")
    except Exception:
        return None
    if use_llm:
        name = _llm_broker(brand, text)      # 검색결과 근거 + 확인 불가 시 UNKNOWN
        return name if is_broker_name(name) else None
    name = _freq_broker(text)
    return name if is_broker_name(name) else None


def load_cache(path=CACHE_PATH):
    """캐시 로드 + 자가 치유: 자리표시 키(과거 오염)를 버린다. 코드만 고치고 캐시를 두면 버그가 살아남는다."""
    try:
        c = json.load(open(path))
    except Exception:
        return {}
    return {k: v for k, v in c.items()          # 자리표시 키·비(非)증권사명 값(과거 오염) 폐기
            if not is_placeholder(k) and is_broker_name(v)}


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
    for lab in (sys.argv[1:] or ["삼성증권", "[Super365]", "1234567890-01",
                                 "[ISA(평생혜택 중개형)(비대면)]"]):
        print(f"{lab:34} -> {resolve_broker(lab, cache)}")
    save_cache(cache)
