#!/usr/bin/env python3
"""증권사 라벨/브랜드 → 정규 증권사명. 하드코딩 매핑 없음 — 검색으로 도출.

증권사 앱 화면의 broker 라벨은 세 형태로 나온다:
  1) 정규명 그대로       "삼성증권"                 → 그대로 채택
  2) 브랜드/상품명        "[Super365]"               → 웹검색으로 정규사 확정(예: 메리츠증권)
  3) 계좌번호/계좌별칭    "1234567890-01", "[ISA…]"  → 라벨만으론 불가 → 크로스-스크린(finalize)에서
                                                       같은 앱 요약화면의 정규사를 상속(여기선 None 반환)

검색은 naver 검색 결과 텍스트를 근거로 **로컬 LLM이** 정규사명을 읽어낸다(모델의 기억이 아니라
검색결과 기반, 확인 불가면 UNKNOWN). **폴백은 없다** — LLM이 없거나 실패하면 None이다
(최빈 토큰 폴백은 6개 중 5개를 지어내 폐기했다, `_freq_broker` 참조).

**디스크 캐시는 없다(규칙).** 브랜드를 푸는 수단은 검색이지 저장이 아니다. 저장은 "방법이
항상 동작하는가"를 가려서, 검색 경로가 죽어 있어도 통과시킨다 — 실제로 그랬다(2026-08-07:
`MODEL` 기본값이 갈려 검색이 6/6 실패하는 동안 캐시가 정답을 내고 있었다). 매 실행 검색으로
확정하고, 못 풀면 못 풀었다고 말한다. 한 실행 안에서 같은 브랜드가 여러 화면에 나오면
호출부가 넘긴 **메모 dict**로 한 번만 검색한다(휘발성, 파일 아님).

나가는 것: **브랜드 토큰 하나**('Super365'). 계좌번호·별칭·종목명·금액은 `brand_token`이
미리 거른다. 실제 요청은 `outbound.py`를 지난다(`PF_OUTBOUND`로 끌 수 있다).
"""
import collections, json, os, re, sys, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import outbound                                     # noqa: E402  외부망 단일 통로
OLLAMA = os.environ.get("OLLAMA", "http://127.0.0.1:11434") + "/api/generate"
# 기본값은 `server.py:22`와 **같아야 한다.** 갈려 있었고(여기 7b / 서버 3b-ft3-q8), 그래서
# CLI·하네스처럼 MODEL을 안 주는 경로가 다른 모델을 썼다. 실측 2026-08-07: 검색 텍스트에 답이
# 55번 들어 있는데(`메리츠 증권 슈퍼 365`) **7b는 UNKNOWN**을 내고 3b-ft3-q8은 `메리츠증권`을
# 낸다 — 즉 이 갈림 하나로 증권사 해석이 **통째로 죽어 있었다**(콜드 6/6 실패).
MODEL = os.environ.get("MODEL", "qwen2.5vl:3b-ft3-q8")

# 계좌별칭/유형 토큰: 브랜드가 아니라 계좌 성격 → 검색 대상 아님(크로스-스크린으로)
_ACCT_HINT = ("연금", "IRP", "ISA", "퇴직", "CMA", "중개형", "비대면", "저축")
_STOP = {"이", "그", "한", "본", "저", "위", "및", "해당", "관련", "증권사"}

# 스키마 자리표시·일반명사: 비전 모델이 값을 못 읽으면 프롬프트의 필드 설명을 그대로 뱉는다.
# 그걸 브랜드로 착각해 웹검색하면 검색결과의 최빈 증권사가 '정답'으로 둔갑한다(실제 오염 사례:
# "증권사명" → 한국투자증권). 브랜드가 아니라 '값 없음'이므로 검색 금지 → None → 크로스-스크린 상속.
_PLACEHOLDER = {"증권사명", "증권사", "브랜드", "브랜드명", "계좌", "계좌명", "계좌유형", "종목명",
                "상품명", "이름", "null", "none", "n/a", "na", "-", "미상", "알수없음", "unknown",
                "broker", "brokername", "string", "값", "없음",
                "○○증권", "△△전자"}   # prompt4c 예시행의 가상 이름 — 에코돼도 검색·캐시 금지


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
    h = outbound.get("broker", url, timeout).decode("utf-8", "ignore")
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)).strip()


def _freq_broker(text):
    """검색결과 텍스트에서 최빈 'XX증권' 정규명(불용어 제외) 또는 None.

    ⚠ **채택 근거로 쓰지 않는다** — `search_broker`에서 제거됐다. 실측(2026-08-07, 라벨 6종):
    최빈은 6개 중 5개를 지어냈다(`보유계좌`→지분증권, `종합잔고`→나무증권,
    `TIGER 미국나스닥100`→신한투자증권, `my자산현황`→유가증권, 그리고 정답이 페이지에 있는
    `Super365`조차 메리츠증권(1회) 대신 **대신증권(2회)**). 근접도로 바꿔도 같이 지어낸다
    (`보유계좌`→다올투자증권, `TIGER…`→키움증권). 즉 **검색 텍스트 마이닝 자체에 분별력이 없다.**
    진단용으로만 남긴다."""
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

    **근거 없으면 답하지 않는다 — 폴백이 없다.**

    예전엔 LLM이 없을 때 검색결과의 '최빈 ○○증권'으로 폴백했다. 그 폴백을 실측으로 기각했다
    (2026-08-07, 라벨 6종 — `_freq_broker` 주석 참조): 최빈은 6개 중 5개를 지어냈고, **정답이
    페이지에 있는 `Super365`조차 틀렸다**(대신증권 2회 vs 메리츠증권 1회). 게다가 검색 순위가
    호출마다 달라 **같은 입력이 다른 증권사를 낸다** — 같은 세션 안에서 메리츠/대신이 갈렸다.
    근접도(브랜드 토큰과의 거리)로 바꿔봐도 마찬가지로 지어낸다. 텍스트 마이닝 자체가 분별력이
    없으므로 **임계값으로 덮지 않고 단계를 없앤다**(각도 분류기 cls를 껐던 것과 같은 판단).

    남은 유일한 검색 경로는 LLM이 검색 텍스트를 읽고 '확인 불가면 UNKNOWN'을 지키는 것이다
    (Orin 전용). 엣지(`use_llm=False`)에는 읽을 수단이 없으므로 **나가지도 않고 None**이다 →
    화면 상속 → 그래도 없으면 `증권사 미상` 경고. 지어내지 않는다.

    캐시로 이 구멍을 덮지 않는다(모듈 docstring). 엣지가 브랜드만 찍히는 앱에서 증권사를
    못 푸는 것은 **현재 사실**이고, 감출 대상이 아니라 측정 결과다."""
    if not use_llm or not outbound.enabled("broker"):
        return None                      # 정책이 껐거나 검증할 LLM이 없으면 **나가지도 않는다**
    try:
        text = _search_text(f"{brand} 어느 증권사")
    except Exception:
        return None
    name = _llm_broker(brand, text)          # 검색결과 근거 + 확인 불가 시 UNKNOWN
    return name if is_broker_name(name) else None


def resolve_broker(label, memo=None, use_llm=True):
    """broker 라벨 → 정규 증권사명 또는 None(라벨만으론 불가 → 크로스-스크린 상속 필요).

    memo: 브랜드 토큰(소문자) → 정규명 dict. **한 실행 안에서만 사는 메모다** — 호출부가
    매번 새로 만들고 어디에도 저장하지 않는다(디스크 캐시 없음, 모듈 docstring 참조).
    같은 브랜드가 6개 화면에 나와도 검색은 한 번이면 되지만, 다음 실행은 다시 검색한다."""
    c = canonical_in(label)
    if c:
        return c
    bt = brand_token(label)
    if not bt:
        return None
    key = bt.lower()
    if memo is not None and key in memo:
        return memo[key]
    name = search_broker(bt, use_llm=use_llm)
    if memo is not None and name:
        memo[key] = name
    return name


if __name__ == "__main__":
    import sys
    memo = {}
    for lab in (sys.argv[1:] or ["삼성증권", "[Super365]", "1234567890-01",
                                 "[ISA(평생혜택 중개형)(비대면)]"]):
        print(f"{lab:34} -> {resolve_broker(lab, memo)}")
