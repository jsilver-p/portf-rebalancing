#!/usr/bin/env python3
"""여러 화면의 비전추출을 종합 → 정규화된 보유자산 + 계좌합계 대조 게이트.

앱은 화면을 1장씩 추출하지만, 정확한 결과에는 크로스-스크린 종합이 필요하다:
  · 요약/계좌목록 화면(상품별 총액·계좌 잔고)은 '보유종목'이 아니라 대조 기준(totals)이다.
    → 이 화면의 행은 홀딩에서 제외하고, 상세 홀딩 합을 이 총액과 대조(재현율·환각 점검).
  · broker 라벨은 화면마다 화면 그대로다([Super365]=브랜드, 계좌번호, 삼성증권=정규명).
    → 정규명은 그대로, 브랜드는 웹검색(resolve_broker), 계좌번호/별칭은 같은 앱 요약화면에서 상속.

입력: [{"file":.., "raw": 비전 원문 JSON텍스트}] (+ 캡처시각)
출력: {"holdings":[...정규화...], "gate": {대조 리포트}}
"""
import difflib, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import resolve_broker as RB

# 상품 카테고리(요약화면 총액 라벨) — 특정 증권사 무관한 일반 금융용어
_CATEGORIES = ("국내주식", "해외주식", "원화예수금", "외화예수금", "국내채권", "해외채권",
               "펀드", "파생상품", "연금", "현금", "채권", "주식")
_CASH_CAT = ("예수금", "예금", "현금", "CMA")
# accountType 정규화(GT 어휘: 일반/연금저축/IRP/ISA).
# '퇴직연금'은 IRP로 통일한다 — 같은 계좌('퇴직연금(다이렉트IRP)')가 라벨 전문에서는 IRP,
# VLM 축약('퇴직연금')에서는 퇴직연금으로 갈려 **같은 계좌가 두 이름**이 됐다(실측 G1 edge↔orin).
_ATYPE = (("IRP", "IRP"), ("ISA", "ISA"), ("연금저축", "연금저축"),
          ("퇴직", "IRP"), ("일반", "일반"))


def _num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = re.sub(r"[^\d.\-]", "", str(x))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except Exception:
        return None


def _sanitize(frag):
    """LLM이 흔히 내는 JSON 위반을 교정. 화면 표기(+1,234원)를 그대로 옮기면 JSON이 깨진다.
    한 행의 사소한 위반이 배열 전체를 무효로 만들어 화면이 통째로 사라지는 걸 막는다."""
    frag = re.sub(r'([:\[,]\s*)\+(\d)', r'\1\2', frag)              # 양수 선행 + (JSON 위반)
    frag = re.sub(r':\s*-?\d{1,3}(?:,\d{3})+(?:\.\d+)?',            # 숫자 안의 천단위 쉼표
                  lambda m: m.group(0).replace(",", ""), frag)
    frag = re.sub(r",\s*([\]}])", r"\1", frag)                      # 트레일링 콤마
    return frag


# 압축(positional) 출력의 열 순서 — prompt4c와 이 상수가 같은 순서를 봐야 한다(진실의 출처는 여기 하나).
COMPACT_COLUMNS = ["broker", "accountType", "name", "assetClass", "currency",
                   "qty", "price", "value", "cost", "pnl", "confidence"]

# 예약 이름 — '이 행은 보유종목이 아니라 **이 화면 자신의 평가금액 총액**이다'.
# 계좌요약 화면이 함께 올라오지 않는 화면(전체계좌 폼 등)에서는 이것이 게이트가 대조할
# **유일한** 근거다. 없으면 게이트는 '완벽한 추출'과 '행 누락'을 구분하지 못한다
# (측정 20260731: img1 정확·img2 한 행 누락인데 경고 문구가 동일했다).
# 추출기가 낼 수도 안 낼 수도 있다(VLM 경로는 내지 않는다) → 없으면 기존 동작 그대로.
SCREEN_TOTAL = "__screen_total__"


def _row_from_list(arr):
    """positional 배열 행 → dict. 열 수가 어긋난 행은 버린다(오배정된 값을 쓰느니 비운다 —
    유실은 합계 대조 게이트가 시끄럽게 잡지만, 한 칸 밀린 값은 조용히 틀린다)."""
    if not isinstance(arr, list) or len(arr) != len(COMPACT_COLUMNS):
        return None
    return dict(zip(COMPACT_COLUMNS, arr))


def parse_rows(raw):
    """비전 원문 텍스트 → 행 리스트(견고한 JSON 파싱). 추출 경로의 **단일 파서**(server도 이걸 쓴다).
    행 형식은 dict(prompt4)와 positional 배열(prompt4c) 둘 다 수용 — 프롬프트 롤백 시 파서는 그대로.

    3단계: ①원문 ②정규화(+부호·쉼표·트레일링콤마) ③행 단위 구제(salvage).
    ③이 핵심 — 한 행이 깨져도 나머지 행은 살린다(전부 아니면 전무 = 화면 통째 유실)."""
    import json
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if m:
        raw = m.group(1)
    i, j = raw.find("["), raw.rfind("]")
    frag = raw[i:j + 1] if (i >= 0 and j > i) else raw
    for f in (frag, _sanitize(frag)):
        try:
            d = json.loads(f)
            if isinstance(d, list):
                dicts = [r for r in d if isinstance(r, dict)]
                lists = [r for r in (_row_from_list(x) for x in d) if r]
                if dicts or lists:
                    return dicts + lists
        except Exception:
            pass
    rows = []                                    # ③ 행 단위 구제
    clean = _sanitize(frag)
    for m in re.finditer(r"\{[^{}]*\}", clean):
        try:
            r = json.loads(m.group(0))
            if isinstance(r, dict):
                rows.append(r)
        except Exception:
            continue
    if not rows:                                 # positional 행 구제(중첩 없는 최심부 배열만 매치)
        for m in re.finditer(r"\[[^\[\]]*\]", clean):
            try:
                r = _row_from_list(json.loads(m.group(0)))
                if r:
                    rows.append(r)
            except Exception:
                continue
    return rows


def norm_atype(s):
    s = s or ""
    for key, canon in _ATYPE:
        if key in s:
            return canon
    return "일반"


def is_category(name):
    """행 이름이 상품 카테고리(총액 라벨)인가 — '국내주식'처럼 종목이 아닌 집계 라벨."""
    n = (name or "").strip()
    return n in _CATEGORIES


_ACCT_NICK = ("연금저축", "IRP", "ISA", "퇴직연금", "CMA", "중개형", "종합저축")


def _security_like(r):
    """행이 개별 보유종목(상세)인가 — 카테고리 라벨/계좌 잔고행이 아닌가.
    종목명(ETF·기업·티커·현금성자산)이면 True. 계좌별칭·홀더명만이면 False."""
    name = (r.get("name") or "").strip()
    if not name or is_category(name):
        return False
    # 계좌 잔고행: 이름이 계좌별칭뿐(예: '[연금저축 CMA]')이고 종목명이 아님
    if name.startswith("[") and any(k in name for k in _ACCT_NICK):
        return False
    return True


def classify(rows):
    """화면 유형: product_summary(상품별 총액) / account_summary(계좌 잔고) / detail(개별종목).
    핵심 판별자 = 행이 '개별 종목'인가 '집계(카테고리/계좌)'인가. broker가 계좌번호여도
    행 이름이 종목이면 detail(계좌번호를 broker 라벨로 쓰는 상세화면)."""
    # 예약행(화면 총액)은 유형 판정에서 제외 — 계좌토큰이 없어서 account_summary의
    # `all(keys)` 판정을 뒤집는다(총액 카드가 있는 계좌요약이 detail로 오분류).
    rows = [r for r in rows if str(r.get("name") or "").strip() != SCREEN_TOTAL]
    if not rows:
        return "empty"      # 빈 화면을 '빈 상세'로 삼으면 화면 유실이 조용히 통과한다 → 게이트가 경고
    n = len(rows)
    cat = sum(1 for r in rows if is_category(r.get("name")))
    if cat >= max(2, n * 0.5):
        return "product_summary"
    # 계좌목록: 행마다 **다른 계좌**를 가리킨다(상세화면은 모든 행이 같은 계좌에 속한다).
    # 이름이 예금주로 읽혀 종목처럼 보여도 이 구조로 가려낸다 — 계좌 잔고 행이 홀딩으로 새는 걸 막는다.
    keys = [frozenset(acct_tokens(" ".join(str(r.get(k, "")) for k in
                                           ("broker", "accountType", "name")))) for r in rows]
    if n >= 2 and all(keys) and len(set(keys)) == n:
        return "account_summary"
    # 기본값은 detail. 상세화면을 계좌목록으로 오분류하면 그 화면의 보유자산이 **조용히 사라진다**
    # (예수금 한 행짜리 화면이 그랬다). 계좌목록은 위의 명시적 판별자(행마다 다른 계좌)로만 인정한다.
    return "detail"


def _drop_total_rows(grp, tol=0.02):
    """합계행 제거 — **이름이 아니라 산술로** 판정한다. 어떤 행의 값이 나머지 행들의 합과 같으면
    그건 종목이 아니라 그 화면의 총액(화면 제목·탭·소계)이다. 이름 목록(카테고리)에 기대면
    '외화예수금' 같은 정당한 현금 보유행까지 지워진다 — 실제로 그 버그가 있었다."""
    if len(grp) < 2:
        return grp
    total = sum(h["value"] for h in grp)
    keep = [h for h in grp if abs(h["value"] - (total - h["value"])) / h["value"] > tol]
    return keep if keep else grp        # 전부 지워질 상황이면 아무것도 지우지 않는다(보수적)


# 예수금 상세 화면의 '같은 돈 다른 표기' — 확정 잔고 한 행 아래에 시점 추정치와 내역이 깔린다.
_DEPOSIT_ECHO = ("현금", "수표", "청약증거금", "접기", "접기›", "출금가능금액", "대용금")


def _drop_deposit_echoes(grp, totals=()):
    """예수금 상세 화면: **같은 돈이 여러 행**으로 나오는 것을 한 행으로 되돌린다.

    화면은 잔고 하나를 시점별(`예수금(당일)`·`추정예수금(D+1)`·`(D+2)`)로, 그리고 내역
    (`현금`·`수표`·`청약증거금`)으로 나눠 보여준다. 미결제가 없으면 값이 전부 같아 위의
    `seen_vals` 중복 제거가 접어버리지만, **미결제가 있으면 값이 달라 각각 살아남아 예수금이
    2~3배로 부푼다**(실측 test-asset 07-21: 48,789,151 + 44,186,151 + 40,225,651).

    **어느 시점이 자산인가는 추측하지 않는다.** 화면이 직접 답한다 — 같은 앱의 상품요약
    화면에 `원화예수금` 총액이 따로 찍히고, 그것과 일치하는 시점이 그 계좌의 예수금이다
    (실측 2026-07-29: 요약 36,682,999 = **D+2**, 당일 41,153,999은 미결제 포함 금액이라
    자산이 아니다 — 앱 자신의 헤드라인도 `원화 예수금 (D+2) 36,682,999원`이다).
    대조할 총액이 없으면 접지 않는다 — 무엇이 진짜인지 모르는 채로 버리느니 남겨두고
    게이트가 `상세합 ≠ 총액`으로 울게 한다.

    (비전 LLM 경로는 프롬프트가 이 화면을 한 행으로 내라고 지시해 이 문제가 없다 —
    OCR 경로는 화면에 있는 것을 다 읽으므로 여기서 같은 규칙을 코드로 준다.)"""
    def deposit_like(h):
        n = str(h.get("name") or "").strip()
        return ("예수금" in n) or (n in _DEPOSIT_ECHO)
    cands = [h for h in grp if deposit_like(h) and h.get("value")]
    if len(cands) < 2:
        return grp
    anchor = next((h for h in cands for t in totals
                   if t and abs(h["value"] - t) / abs(t) <= 0.01), None)
    if anchor is None:
        return grp                      # 근거 없음 → 접지 않는다(게이트가 불일치를 알린다)
    return [h for h in grp if h is anchor or not deposit_like(h)]


def acct_tokens(text):
    """계좌 식별 토큰: 계좌번호 + 계좌유형. 상세화면과 요약화면의 '같은 계좌'를 잇는 열쇠.
    전역 상속(증권사 3곳 이상이면 반드시 오염)을 대신해 **계좌 단위**로만 상속하기 위한 것."""
    t = str(text or "")
    toks = set(re.findall(r"\d[\d\-]{6,}", t))              # 계좌번호(하이픈 포함)
    for k in _ACCT_NICK:
        if k in t:
            a = norm_atype(k)
            if a != "일반":     # '일반'은 기본값 — 계좌를 식별하지 못한다(CMA 등이 오매칭을 부른다)
                toks.add(a)
    return toks


_NUMTOK = re.compile(r"^[\d,.\-+%원₩$()]+$")


def _norm_ev(s):
    """근거 대조용 정규화 — 공백·괄호·구두점 제거, 소문자."""
    return re.sub(r"[\s\[\]()·\-_.,:/]", "", str(s or "")).lower()


def _ev_forms(evidence):
    """근거 텍스트의 두 형태 — 원본과 **숫자 토큰을 뺀 것**.

    화면은 좁아서 라벨이 줄바꿈된다: `현금성자산(삼성증` … `3,065,859` `0` … `권)`.
    읽기순으로 이으면 '삼성증권'이 끊겨 실재하는 값이 '근거 없음'으로 오판된다(실측 false
    positive). 끼어든 것은 **다른 열의 금액**이지 텍스트가 아니므로, 숫자 토큰을 빼면
    조각이 다시 붙는다. 두 형태 중 하나라도 맞으면 근거로 인정한다.
    """
    toks = str(evidence or "").split()
    return (_norm_ev(evidence),
            _norm_ev("".join(t for t in toks if not _NUMTOK.match(t))))


NAME_SUPPORT = 0.6      # 종목명 ↔ 화면 근거의 최장 연속 일치 비율 하한
NAME_MIN_LEN = 4        # 이보다 짧은 이름은 심판하지 않는다 — 아래 주석 참조


def _name_supported(name, evidence):
    """모델이 낸 **종목명**이 화면에 실재하는가 — broker와 같은 심판을 이름에 건다.

    필요한 이유: 범용 VLM이 `△△전자`(가림 기호 자리표시자)를 종목명으로 낸 적이 있고(§4.2)
    금액만 맞으면 총액 대조를 통과해 게이트가 침묵했다. 이름에는 심판이 없었다.

    **완전 일치를 요구하지 않는다.** 근거인 OCR 자신도 글리프를 틀리므로(`IVV`→`IWV`)
    엄격히 대조하면 옳은 이름을 거짓 경보로 잡는다. 그래서 *최장 연속 일치*가 이름 길이의
    NAME_SUPPORT 이상이면 근거로 인정한다 — 한두 글자 오독은 통과하고, 화면에 아예 없는
    이름은 통과하지 못한다.

    **짧은 이름(NAME_MIN_LEN 미만)은 판정하지 않는다.** 3자 티커는 한 글자만 어긋나도
    비율이 0.33으로 떨어져 오독과 날조를 구분할 수 없다. 그 자리는 이미 심볼 게이트가
    **시세**라는 더 강한 근거로 지킨다(`server._arbitrate_symbol`) — 약한 심판을 겹쳐서
    거짓 경보를 만들 이유가 없다."""
    n = _norm_ev(name)
    if not evidence or len(n) < NAME_MIN_LEN:
        return True
    # 근거 전체와 한 번에 비교하면 안 된다 — 긴 텍스트엔 글자가 다 흩어져 있어 아무 이름이나
    # '부분적으로' 맞는다. 이름 길이만 한 **창**을 밀면서 그 안에서만 유사도를 잰다.
    step = max(1, len(n) // 3)
    best = 0.0
    for ev in _ev_forms(evidence):
        for i in range(0, max(1, len(ev) - len(n) + 1), step):
            w = ev[i:i + len(n) + step]
            best = max(best, difflib.SequenceMatcher(None, n, w, autojunk=False).ratio())
            if best >= NAME_SUPPORT:
                return True
    return False


def _label_supported(label, screen_text, broker, evidence):
    """모델이 내놓은 증권사명이 **화면에 실재하는가** — 라벨의 독립 심판.

    값에는 '총액'이라는 독립 심판이 있어서 틀리면 게이트가 운다. **라벨에는 없었다.**
    그래서 비전 모델이 `한국투자증권`을 지어내도 금액만 맞으면 총액 대조를 통과하고
    게이트는 침묵했다(측정: 프로덕션 VLM, 3행. `main`에서도 재현).

    `evidence`는 같은 이미지의 OCR 원문 텍스트다 — **비전 모델과 독립**이므로 심판이 된다.
    근거가 없으면 지어낸 것으로 보고 비운다(§4.7의 '근거 없으면 비우고 경고'와 같은 규칙을
    우리 폴백이 아니라 **모델 출력**에 적용한다).

    evidence가 없으면(구 호출부·OCR 텍스트 미가용) 판정하지 않는다 — 무판정이지 통과가 아니다.
    OCR 추출 경로에서는 broker가 화면 텍스트에서 나오므로 이 심판이 항상 통과한다(자기점검).
    """
    if not evidence:
        return True
    forms = _ev_forms(evidence)
    for cand in (RB.brand_token(label), RB.canonical_in(label),
                 RB.canonical_in(screen_text), broker, label):
        c = _norm_ev(cand)
        if c and any(c in ev for ev in forms):
            return True
    return False


def fname_brand(fname):
    """업로드 **파일명**에서 캡처 앱 토큰을 뽑는다 — 안드로이드 스크린샷 파일명에는 캡처한
    앱 이름이 박힌다('Screenshot_20260709_160155_ SMART.jpg' → 'SMART', '..._mPOP.jpg' → 'mPOP').

    화면에 브랜드조차 없는 캡처(mPOP 상세 등)에서 증권사의 **마지막 근거**가 된다. 단 약한
    증거다 — 파일명은 사용자가 바꿀 수 있다. 그래서 화면 근거(정규명·상속·화면 브랜드)가 전부
    실패한 뒤에만 쓰고, 출처를 `filename`으로 표시해 사용자 확인 대상으로 남긴다."""
    s = re.sub(r"\.[A-Za-z0-9]+$", "", str(fname or ""))
    # 구분자(숫자·밑줄)를 먼저 공백으로 — 그래야 'IMG_1234'의 IMG에 단어 경계가 생겨
    # 아래 제거식이 잡는다(예전엔 \bimg\b가 IMG_에 안 걸려 'IMG'가 브랜드로 샜다).
    s = re.sub(r"[\d_\-]+", " ", s)
    s = re.sub(r"(?i)screen[-_ ]?shot|kakaotalk|\bimg\b|\bimage\b|\bphoto\b|\bpxl\b|\bdsc\b",
               " ", s).strip()
    return s if s and not RB.is_placeholder(s) else None


def _fill_qty_from_geom(p):
    """VLM이 비운 **수량**을 같은 화면의 기하 바인딩 행에서 되메꾼다 — 값(평가금액)이 정확히
    일치하는 행이 유일한 수량을 줄 때만.

    실측(08-10 폰 케이스): IRP·ISA 상세 폼에서 화면에 수량이 실재하는데 VLM이 전 행
    qty=null → 수량 사다리가 종가 역산으로 '추정' 표시. 같은 요청에서 OCR은 이미 돌고
    있고(broker 심판) 기하 바인딩은 결정적이므로, 이것은 추정이 아니라 화면 판독이다
    (enrich가 qty_src=screen으로 기록). VLM이 채운 수량은 절대 덮지 않는다."""
    if not p.get("geom"):
        return
    by_val = {}
    for gr in parse_rows(p["geom"]):
        gv, gq = _num(gr.get("value")), _num(gr.get("qty"))
        if gv and gq is not None and str(gr.get("name") or "").strip() != SCREEN_TOTAL:
            by_val.setdefault(gv, set()).add(gq)
    for r in p["rows"]:
        if r.get("qty") is None and r.get("value") is not None:
            qs = by_val.get(r["value"]) or set()
            if len(qs) == 1:                 # 값이 같은 행이 여럿인데 수량이 갈리면 귀속 불명 → 안 채움
                r["qty"] = next(iter(qs))


def finalize(screens, use_llm=True):
    """screens: [{"file":str,"raw":str,"evidence":str|None,"fname":str|None}]. 반환 {holdings, gate}.

    evidence = 그 화면의 OCR 원문 텍스트(선택). 있으면 broker 라벨의 독립 심판으로 쓴다.
    fname = 업로드 원본 파일명(선택). 화면에 근거가 전무할 때 증권사 앱 토큰의 최후 근거."""
    broker_memo = {}      # 이 실행 안에서만 산다 — 디스크 캐시 없음(`resolve_broker` docstring)
    parsed = []
    for sc in screens:
        rows = parse_rows(sc.get("raw", ""))
        for r in rows:
            r["value"] = _num(r.get("value"))
            r["cost"] = _num(r.get("cost"))
            r["qty"] = _num(r.get("qty"))
            r["pnl"] = _num(r.get("pnl"))
        parsed.append({"file": sc.get("file"), "rows": rows, "type": classify(rows),
                       "evidence": sc.get("evidence"),    # 화면 원문 텍스트(있으면 심판)
                       "geom": sc.get("geom"),            # 같은 화면의 기하 바인딩 행(VLM 모드)
                       "fname": sc.get("fname")})         # 업로드 원본 파일명(있으면 최후 근거)

    # 1) 요약화면에서 totals + 계좌 목록(계좌 → 증권사·유형) 수집
    product_totals = {}     # 카테고리 → 총액
    total_src = {}          # 카테고리 → 그 총액이 실린 요약화면(같은 앱 판별용)
    account_totals = []     # [{key, atype, total}]
    accounts = []           # [{tokens, broker, atype}] — 상속의 출처(계좌 단위)
    brokers_seen = set()
    bad_totals = []                      # 잔고는 음수일 수 없다 → 음수면 그건 평가손익 오독
    for p in parsed:
        if p["type"] == "product_summary":
            for r in p["rows"]:
                if is_category(r.get("name")) and r.get("value") is not None:
                    v, pl = r["value"], r.get("pnl")
                    if v < 0 or (pl is not None and pl != 0 and abs(v - pl) < 1):
                        bad_totals.append(f"{p['file']}: '{r['name']}' 총액을 읽지 못함"
                                          f"({v:,.0f}은 평가손익) → 이 항목은 대조 불가")
                        continue
                    product_totals[r["name"].strip()] = v
                    total_src[r["name"].strip()] = p["file"]      # 이 총액이 어느 요약화면에서 왔나
        elif p["type"] == "account_summary":
            screen_text = " ".join(str(r.get(k, "")) for r in p["rows"]
                                   for k in ("broker", "accountType", "name"))
            scr_broker = RB.canonical_in(screen_text)       # 화면 어딘가의 정규 증권사명(탭 등)
            for r in p["rows"]:
                # 계좌 잔고 행에는 '수량' 개념이 없다 → qty에 숫자가 있으면 칸을 잘못 채운 것.
                # 값이 비었는데 수량이 있으면 그 숫자가 곧 잔고다(작은 잔고에서 실제로 발생).
                if not r.get("value") and r.get("qty"):
                    r["value"], r["qty"] = r["qty"], None
                acct_name = " ".join(str(r.get(k, "")) for k in ("broker", "accountType", "name"))
                b = RB.canonical_in(acct_name) or scr_broker
                if b:
                    brokers_seen.add(b)
                accounts.append({"tokens": acct_tokens(acct_name), "broker": b,
                                 "atype": norm_atype(acct_name)})
                v, pl = r.get("value"), r.get("pnl")
                dup = v is not None and pl is not None and pl != 0 and abs(v - pl) < 1
                if v is not None and (v < 0 or dup):
                    # 잔고는 음수 불가. 잔고와 손익이 같은 숫자면 손익을 잔고 자리에 복제한 것.
                    # 오염된 총액을 대조 기준으로 쓰면 게이트가 거짓 경보를 낸다 → 기준에서 제외.
                    bad_totals.append(f"{p['file']}: 계좌 '{norm_atype(acct_name)}' 잔고를 읽지 못함"
                                      f"({v:,.0f}은 평가손익) → 이 계좌는 대조 불가")
                elif v is not None:
                    account_totals.append({"key": str(r.get("broker") or r.get("name") or "").strip(),
                                           "atype": norm_atype(acct_name),
                                           "nick": acct_name, "total": r["value"]})

    def inherit(screen_text):
        """상세화면 ← 같은 **계좌**의 요약행에서 증권사·유형 상속(토큰 교집합이 최대인 계좌).
        교집합이 없으면 상속하지 않는다 — 다른 앱의 증권사명을 끌어오는 오염을 막는다."""
        toks = acct_tokens(screen_text)
        best, score = None, 0
        for a in accounts:
            s = len(toks & a["tokens"])
            if s > score:
                best, score = a, s
        if best:
            return best["broker"], best["atype"]
        # '증권사가 하나뿐이면 그걸 쓴다'는 폴백은 금지 — 계좌요약이 A증권 것뿐인데 B증권 화면이
        # 섞여 들어오면 B의 자산에 A가 붙는다(교차오염). 근거가 없으면 비워두고,
        # 총액 대조 관계로 소속을 추론한다(아래 transitive 규칙).
        return None, None

    # 2) 상세화면 홀딩만 수집 + broker 정규화 + 화면별 그룹(게이트용)
    holdings, groups = [], []
    fabricated = []                      # 모델이 지어낸 증권사명(아래 _label_supported가 잡는다)
    for p in parsed:
        if p["type"] != "detail":
            continue
        _fill_qty_from_geom(p)               # VLM이 비운 수량을 같은 화면의 기하 판독으로 되메꿈
        # 예약행(SCREEN_TOTAL)은 broker가 비어 있다 → 라벨·화면텍스트에서 제외한다.
        # 안 빼면 그 행이 첫 행일 때 label이 빈 문자열이 되어 broker 해석이 통째로 실패한다
        # (측정 20260731 실캡처: 메리츠증권 15/15 → None 15/15로 회귀).
        real = [r for r in p["rows"] if str(r.get("name") or "").strip() != SCREEN_TOTAL]
        label = str((real[0].get("broker") if real else "") or "")
        screen_text = " ".join(str(r.get(k, "")) for r in real
                               for k in ("broker", "accountType", "name"))
        # 증권사 라벨이 그 화면의 **종목명과 같으면** 열이 잘못 채워진 것이다(브랜드가 아니다).
        # 그대로 검색에 태우면 종목 검색결과에서 아무 증권사나 주워온다 — 실측 오염:
        # `한화오션`→대신증권, `TIGER 미국나스닥100`→키움증권이 캐시에 남아 있었다.
        names = {str(r.get("name") or "").strip() for r in real}
        if label.strip() in names:
            label = ""
        broker = RB.resolve_broker(label, broker_memo, use_llm=use_llm)   # 정규명·브랜드(검색)
        bsrc = "screen" if (broker and RB.canonical_in(label)) else ("research" if broker else None)
        if not broker:
            broker = RB.canonical_in(screen_text)   # 화면 어딘가의 정규명(예: '현금성자산(삼성증권)')
            bsrc = "screen" if broker else None
        if broker and not _label_supported(label, screen_text, broker, p.get("evidence")):
            fabricated.append(f"{p['file']}: 증권사 '{broker}' — 화면 텍스트에 근거 없음"
                              f"(추출 모델이 지어낸 값으로 판단해 비움)")
            broker = None
            bsrc = None
        inh_broker, inh_atype = inherit(screen_text)
        if not broker and inh_broker:               # 계좌번호·별칭뿐이면 같은 계좌의 요약에서 상속
            broker, bsrc = inh_broker, "inherited"
        if not broker:
            # 정규명도 상속도 없다 → **화면에 찍힌 브랜드를 그대로 라벨로 쓴다**(`Super365`).
            # 지어내는 게 아니다. 브랜드는 화면에 실재하고 심판도 통과한다. 정규명이 아닐 뿐이다.
            #
            # 비우는 것보다 **엄격히 낫다.** broker=None은 중립이 아니라 프런트에서 전부
            # `미상`으로 접히고, 그러면 **서로 다른 증권사의 보유가 한 그룹으로 합쳐진다**
            # (실측 B그룹: 메리츠 Super365 21행 + 삼성 종합 1행이 `미상|일반` 하나로).
            # 브랜드를 쓰면 그룹은 계좌별로 갈리고, 정규명은 요약화면이 오면 상속으로 승격된다.
            bt = RB.brand_token(label)
            if bt and _label_supported(label, screen_text, bt, p.get("evidence")):
                broker, bsrc = bt, "brand"
        if not broker:
            # 화면에 근거가 전무 → **업로드 파일명**의 캡처 앱 토큰이 마지막 근거다
            # ('Screenshot_…_mPOP.jpg'). 심판(_label_supported)은 걸지 않는다 — 근거가
            # 화면이 아니라 파일명 자체다. LLM 경로는 검색으로 정규명까지 시도하고,
            # 못 풀면 토큰을 라벨로 남긴다(브랜드 폴백과 같은 원칙, 출처만 `filename`).
            ft = fname_brand(p.get("fname"))
            bt = RB.brand_token(ft) if ft else None
            if bt:
                resolved = RB.resolve_broker(bt, broker_memo, use_llm=use_llm)
                broker, bsrc = (resolved, "research") if resolved else (bt, "filename")
        grp, seen_vals = [], {}
        screen_total = None
        for r in p["rows"]:
            v = r.get("value")
            if str(r.get("name") or "").strip() == SCREEN_TOTAL:
                screen_total = v            # 보유행이 아니라 이 화면의 대조 기준
                continue
            if not v:                        # 평가금액 0/없음 = 리밸런싱 대상 아님(수표·미사용 항목 등)
                continue
            if v in seen_vals:               # 같은 화면에 같은 금액이 반복 = 같은 자산의 다른 표기
                continue                     # (예수금 화면의 당일/D+1/D+2/출금가능금액) → 한 자산 한 행
            seen_vals[v] = True
            h = {k: r.get(k) for k in ("name", "assetClass", "currency", "qty",
                                       "price", "value", "cost", "pnl")}
            # **`or label` 폴백을 두지 않는다.** 예전엔 해석 실패 시 화면 라벨을 그대로 넣었는데,
            # 그 라벨은 대개 계좌번호라 `broker='1234567890-14[ISA(...)]'` 같은 값이 나왔다.
            # 값이 아니라 **라벨을 지어내는** 것이고, 게이트는 침묵했다(측정 test-asset 07-21: 6/34행).
            # 근거가 없으면 비운다 — 미상은 아래에서 경고로 표면화된다.
            h["broker"] = broker or None
            # 증권사 값의 **출처**를 남긴다 — 다른 필드(qty_src·price_src)와 같은 규약.
            # screen=화면에 적혀 있음 / inherited=같은 계좌의 요약화면 / research=검색으로 확정.
            # 근거 없이 채운 값은 이제 존재하지 않으므로, 이 칸이 비면 broker도 비어 있다.
            if broker:
                h["broker_src"] = bsrc
            # 종목명의 독립 심판. **이름은 비우지 않는다** — 행의 식별자라 비우면 행이 사라지고,
            # 값(금액·수량)은 이 판정과 무관하게 멀쩡하다. 표시만 하고 경고로 표면화한다.
            if not _name_supported(h.get("name"), p.get("evidence")):
                h["name_src"] = "unverified"
                fabricated.append(f"{p['file']}: 종목명 '{h.get('name')}' — 화면 텍스트에 근거 없음"
                                  f"(추출 모델이 지어냈을 수 있음 — 값은 유지)")
            atype = str(r.get("accountType") or "")
            # 유형도 계좌 단위 상속: 상세화면 라벨은 줄임말이기 쉽다('퇴직연금' ← '퇴직연금(다이렉트IRP)')
            h["accountType"] = inh_atype or norm_atype(atype or label)
            if h.get("value") is not None and not h.get("currency"):
                h["currency"] = "KRW"        # 화면 금액 표기는 원화(외화는 추출기가 명시) — 표기 통일
            h["_file"] = p["file"]
            # 화면 간 병합의 열쇠 — 이 행이 속한 계좌의 **번호 토큰**(행 구분자 우선, 없으면 화면 라벨).
            # VLM 경로는 정규명만 내서 비어 있을 수 있다 → 그때 병합은 약한 키로만(_merge_across_screens).
            h["_acct"] = frozenset(t for t in acct_tokens(str(r.get("broker") or "") + " " + label)
                                   if t[:1].isdigit())
            grp.append(h)
        # 예수금 상세: 같은 돈의 시점·내역 표기를 한 행으로 — 어느 시점인지는 총액이 정한다.
        # 근거는 요약화면의 원화예수금 총액 **또는 이 화면 자신의 카드 총액**(예수금 탭의
        # '원화 예수금 (D+2)' 카드). 카드가 답이 될 수 있게 되면서, 상세만 올린 세트에서도
        # 같은 돈이 3행(당일 48.8M·D+2 44.2M·현금 40.2M)으로 새던 것이 닫힌다(실측 G4).
        grp = _drop_deposit_echoes(grp, list(product_totals.values())
                                   + ([screen_total] if screen_total else []))
        grp = _drop_total_rows(grp)          # 화면 제목·탭·소계가 종목처럼 섞여 나오는 것 제거
        holdings.extend(grp)
        groups.append({"file": p["file"], "sum": sum(x["value"] or 0 for x in grp),
                       "n": len(grp), "rows": grp, "screen_total": screen_total,
                       "fname": p.get("fname")})

    # 상세화면이 없는 '현금 계좌'(CMA 등)는 잔고 자체가 보유자산이다 — 요약에만 있다고 누락시키면
    # 총자산이 어긋난다. 단 현금 계좌라고 라벨이 말할 때만(구성을 모르는 계좌를 현금으로 단정하지 않는다).
    covered = {h["accountType"] for h in holdings}
    for a in account_totals:
        if a["atype"] in covered or not a["total"]:
            continue
        if not any(k in a["nick"] for k in ("CMA", "현금", "예수금")):
            continue                          # 구성 불명 → 추측하지 않는다(게이트가 '미대조'로 경고)
        a["_as_cash"] = True                  # 잔고를 현금 자산으로 편입했다 → '미대조' 경고 대상 아님
        m = re.search(r"\[([^\]]+)\]", a["nick"])
        holdings.append({"name": f"현금({(m.group(1) if m else a['atype']).strip()})",
                         "assetClass": "현금", "currency": "KRW", "qty": None, "price": None,
                         "value": a["total"], "cost": None, "pnl": None,
                         "broker": RB.canonical_in(a["nick"]) or (next(iter(brokers_seen)) if
                                                                  len(brokers_seen) == 1 else None),
                         "accountType": a["atype"], "value_src": "screen(계좌 잔고)",
                         "_file": "account_summary"})

    # 증권사 라벨이 없는 화면(예: 외화예수금 탭)의 소속 추론 — **같은 요약화면에 대조되는 화면들은
    # 같은 앱(증권사)에 속한다.** 화면 순서·파일명에 기대지 않고 '총액 대조'라는 이미 있는 관계를 쓴다.
    # 단일 증권사라고 넘겨짚지 않는다(증권사 여럿이면 오염되므로).
    owner = {}              # 요약화면 file → 그 화면에 대조된 상세화면들의 증권사
    for g in groups:
        b = next((h["broker"] for h in g["rows"] if h.get("broker")), None)
        if not b:
            continue
        for cat, amt in product_totals.items():
            if amt and abs(g["sum"] - amt) / abs(amt) <= 0.02:
                owner.setdefault(total_src.get(cat), set()).add(b)
    for g in groups:
        if any(h.get("broker") for h in g["rows"]):
            continue
        for cat, amt in product_totals.items():
            if not amt or abs(g["sum"] - amt) / abs(amt) > 0.02:
                continue
            cands = owner.get(total_src.get(cat)) or set()
            if len(cands) == 1:              # 그 요약화면의 다른 상세들이 모두 한 증권사 → 이 화면도 그 증권사
                b = next(iter(cands))
                for h in g["rows"]:
                    h["broker"] = b
            break

    # 파일명 앱 토큰 **교차 전파** — 같은 앱이 찍은 화면은 같은 증권사다. 브랜드조차 화면에
    # 없는 캡처(mPOP ISA 상세: 계좌번호뿐)도, 파일명 토큰이 같은 형제 화면이 정규명으로
    # 풀렸으면('현금성자산(삼성증권)' 등) 그 정규명을 받는다(실측 08-10 폰 케이스: ISA 미상).
    # 파일명은 사용자가 바꿀 수 있는 약한 증거 → 출처는 filename으로 남겨 확인 대상 유지.
    # 토큰 하나가 여러 증권사로 풀리면 전파하지 않는다(오염 방지).
    tokmap = {}             # 파일명 앱 토큰 → 확인된 정규 증권사명(1순위) / 화면 브랜드(2순위)
    brandmap = {}
    for g in groups:
        ft = fname_brand(g.get("fname"))
        if not ft:
            continue
        for h in g["rows"]:
            if h.get("broker_src") in ("screen", "research", "inherited") \
                    and RB.canonical_in(str(h.get("broker") or "")):
                tokmap.setdefault(ft, set()).add(h["broker"])
            elif h.get("broker_src") == "brand":     # 정규명은 아니지만 화면에 실재하는 브랜드
                brandmap.setdefault(ft, set()).add(h["broker"])
    for g in groups:
        ft = fname_brand(g.get("fname"))
        # 정규명이 유일하면 그것을, 없으면 브랜드가 유일할 때 그 라벨을 전파 — 같은 앱의
        # 화면들이 한 그룹으로 묶여 확인(정규명 승격)도 한 번이면 된다.
        cands = tokmap.get(ft) or set()
        if len(cands) != 1:
            cands = brandmap.get(ft) or set() if not cands else set()
        if len(cands) != 1:
            continue
        b = next(iter(cands))
        for h in g["rows"]:
            # 근거가 전무한 행, 또는 파일명 토큰을 라벨로 임시 채운 행만 승격.
            if not h.get("broker") or h.get("broker_src") == "filename":
                h["broker"], h["broker_src"] = b, "filename"

    repairs = _repair_digit_slips(groups, product_totals, account_totals)
    for g in groups:                     # 보정 후 합계 갱신(게이트가 보정된 값을 보게)
        g["sum"] = sum(x["value"] or 0 for x in g["rows"])
    gate = _cross_check(groups, product_totals, account_totals)
    gate["repairs"] = repairs
    # 같은 계좌가 여러 화면(재캡처·스크롤 겹침)으로 올라오면 같은 자산이 화면 수만큼 복제된다 —
    # '한 자산 한 행'이 화면 안(seen_vals)에만 있었다(실측 G5/G9: IRP 7행·ISA 4행 2중 계상).
    # 게이트 **뒤**에 병합한다: 화면별 상세합 대조는 화면 그대로의 행으로 해야 맞는다.
    holdings, merge_warns = _merge_across_screens(holdings, gate["checks"])
    gate["warnings"].extend(merge_warns)
    # 증권사 미상 경고 — **owner 추론까지 끝난 뒤에** 판정한다(그 전에 세면 거짓 경보가 난다).
    # 비워두는 것 자체는 옳지만 **조용히** 비면 안 된다: 화면에 근거가 없다는 사실이
    # 사용자에게 보여야 다음 캡처에서 계좌요약 화면을 같이 올릴 수 있다.
    no_broker = sorted({h.get("_file") or "?" for h in holdings if not h.get("broker")})
    unknown = [f"{f}: 증권사 미상 — 화면에 근거 없음(계좌요약 화면을 함께 올리면 해결)"
               for f in no_broker]
    # 브랜드 표기로 남은 것은 **미상이 아니다** — 값이 있고 그룹핑도 된다. 다만 정규명이 아니라
    # 다른 캡처의 정규명과 자동으로 합쳐지지 않는다. 그 사실만 알린다(경고 문구를 섞지 않는다).
    branded = sorted({(h.get("_file") or "?", h["broker"], h["broker_src"]) for h in holdings
                      if h.get("broker_src") in ("brand", "filename")})
    unknown += [f"{f}: 증권사 정규명 미확인 — "
                + (f"화면 브랜드 '{b}'를 그대로 씀" if src == "brand" else
                   f"같은 앱(파일명)의 다른 화면에서 확인된 '{b}'로 추정" if RB.canonical_in(b)
                   else f"파일명의 앱 이름 '{b}'를 그대로 씀")
                + "(계좌요약 화면을 함께 올리면 정규명으로 합쳐진다)"
                for f, b, src in branded]
    gate["warnings"] = bad_totals + repairs + fabricated + unknown + gate["warnings"]
    for p in parsed:                     # 빈 화면 = 추출 실패. 조용히 넘기지 않는다.
        if p["type"] == "empty":
            gate["warnings"].insert(0, f"{p['file']}: 추출 0행 — 화면 유실(파싱 실패·미인식) 의심")
    for h in holdings:
        h.pop("_acct", None)                 # 병합 키는 내부용 — 결과 스키마에 남기지 않는다
    return {"holdings": holdings, "gate": gate,
            "screens": [{"file": p["file"], "type": p["type"]} for p in parsed]}


def _merge_across_screens(holdings, checks):
    """화면 간 같은 계좌·같은 자산 병합 — 사용자의 업로드 방식(같은 계좌를 부분·중복 캡처)이
    이중 계상이 되지 않게 한다.

    두 단계 키(강한 근거부터, 근거 없으면 합치지 않는다):
      · 강한 키 — 두 행 모두 **계좌번호 토큰**이 있고 교집합 + 이름 동일. 같은 계좌임이 확실
        하므로 값이 달라도 한 행만 남긴다(화면 총액 대조를 통과한 화면 우선, 다음은 채워진
        필드가 많은 행) + 경고.
      · 약한 키 — 번호가 없으면(VLM 경로) (증권사, 계좌유형, 이름) 동일 + **값까지 동일**할
        때만 병합. 값이 다르면 다른 계좌일 수 있다 → 둘 다 남기고 '중복 의심' 경고만
        (틀린 병합은 이중 계상보다 나쁘다 — 자산 하나가 조용히 사라진다).
    병합은 무손실: 빈 필드(수량·매수가·단가)는 상대 행에서 보완한다 — 같은 계좌를 다른
    화면(수량이 보이는 폼·안 보이는 폼)으로 찍었을 때 서로를 채운다."""
    st_ok = {c["file"]: c["match"] for c in checks if c.get("scope") == "화면 총액"}
    out, warns = [], []
    idx_of = {}                              # (종류, 키) → out 인덱스

    def fill(dst, src):
        for k in ("qty", "price", "cost", "pnl", "currency", "assetClass"):
            if dst.get(k) is None and src.get(k) is not None:
                dst[k] = src[k]

    for h in holdings:
        name = _norm_ev(h.get("name"))
        toks = h.get("_acct") or frozenset()
        prev_i = None
        if name and toks:
            for (kind, key), i in idx_of.items():
                if kind == "acct" and key[1] == name and (toks & key[0]) \
                        and out[i].get("_file") != h.get("_file"):
                    prev_i = i
                    break
        elif name and h.get("broker"):
            k = ("label", (h["broker"], h.get("accountType"), name))
            i = idx_of.get(k)
            if i is not None and out[i].get("_file") != h.get("_file"):
                prev_i = i
        elif name:
            # 미상(broker·계좌번호 모두 없음) — VLM 경로의 ISA 재캡처가 여기로 온다(실측 G9:
            # 같은 화면 두 장이 미상 4행씩 복제, 경고도 없이 총액 +1M). 구별 증거가 0인 행
            # (유형·이름에 더해 **값·수량·원가·손익까지 전부 동일**)만 같은 것으로 본다 —
            # 하나라도 다르면 다른 계좌일 수 있으므로 합치지 않고 '중복 의심'으로 알린다.
            k = ("anon", (h.get("accountType"), name))
            i = idx_of.get(k)
            if i is not None and out[i].get("_file") != h.get("_file"):
                # 한쪽이 비운 칸(None)은 모순이 아니다 — 같은 화면 재캡처에서 VLM이 손익을
                # 한 번은 0, 한 번은 null로 내는 요동(실측 G9 현금잔고)이 복제를 남겼다.
                same = all(out[i].get(f) is None or h.get(f) is None or out[i].get(f) == h.get(f)
                           for f in ("value", "qty", "cost", "pnl")) \
                    and out[i].get("value") == h.get("value")
                if same:
                    fill(out[i], h)          # 동일 복제 — 빈 칸만 보완하고 버린다
                    continue
                warns.append(f"{out[i].get('_file')}·{h.get('_file')}: '{h.get('name')}' 중복 의심 — "
                             f"증권사 미상·같은 유형인데 값이 다름 — 둘 다 남김(확인 필요)")
        if prev_i is None:
            idx_of[("acct", (toks, name)) if toks else
                   ("label", (h.get("broker"), h.get("accountType"), name)) if h.get("broker") else
                   ("anon", (h.get("accountType"), name))] = len(out)
            out.append(h)
            continue
        prev = out[prev_i]
        pv, hv = prev.get("value"), h.get("value")
        same_val = pv is not None and hv is not None and abs(pv - hv) <= abs(pv) * 0.005
        if same_val:
            fill(prev, h)                    # 무손실 병합 — 복제 행은 버린다
            continue
        if not toks:                         # 약한 키 + 값 불일치 → 병합하지 않는다(다른 계좌 가능)
            warns.append(f"{prev.get('_file')}·{h.get('_file')}: '{h.get('name')}' 중복 의심 — "
                         f"같은 증권사·유형인데 값이 다름({pv:,.0f} vs {hv:,.0f}) — 둘 다 남김(확인 필요)")
            out.append(h)
            continue
        keep_new = (st_ok.get(h.get("_file")) is True and
                    st_ok.get(prev.get("_file")) is not True)
        if not keep_new and st_ok.get(h.get("_file")) == st_ok.get(prev.get("_file")):
            keep_new = (sum(v is not None for v in h.values())
                        > sum(v is not None for v in prev.values()))
        w = h if keep_new else prev
        warns.append(f"{prev.get('_file')}·{h.get('_file')}: 같은 계좌의 '{h.get('name')}' 값이 "
                     f"화면마다 다름({pv:,.0f} vs {hv:,.0f}) — {w.get('value'):,.0f} 채택")
        if keep_new:
            fill(h, prev)
            out[prev_i] = h
        else:
            fill(prev, h)
    return out, warns


def _repair_digit_slips(groups, product_totals, account_totals,
                        broken=0.01, fixed=0.001):
    """계좌 총액(독립 측정치)으로 **자릿수 오독**을 잡아 보정한다 — 교차검증의 본령.

    비전 모델은 작은 숫자에서 자릿수를 흘린다(4,716 → 47,160). 상세 합이 총액과 크게(>1%)
    어긋나는데 **단 한 행**을 10의 거듭제곱으로 고치면 총액과 정확히(≤0.1%) 맞아떨어진다면,
    그건 우연이 아니라 그 행의 자릿수 오독이다. 후보가 여럿이면 손대지 않는다(모호 → 경고만).
    시세 변동 같은 작은 괴리(<1%)는 손대지 않는다 — 실시간 시세차를 '보정'하면 안 된다."""
    totals = [v for v in product_totals.values() if v] + \
             [a["total"] for a in account_totals if a["total"]]
    out = []
    for g in groups:
        if g["n"] < 1 or not g["sum"]:
            continue
        tgt = min(totals, key=lambda t: abs(g["sum"] - t), default=None)
        if not tgt or abs(g["sum"] - tgt) / abs(tgt) <= broken:
            continue                       # 애초에 맞거나(또는 근소차) → 보정 대상 아님
        cands = []
        for r in g["rows"]:
            if not r.get("value"):
                continue
            for f in (0.1, 0.01, 10, 100):
                s = g["sum"] - r["value"] + r["value"] * f
                if abs(s - tgt) / abs(tgt) <= fixed:
                    cands.append((r, f))
        if len(cands) == 1:
            r, f = cands[0]
            old = r["value"]
            r["value"] = round(old * f, 2)
            r["value_src"] = "screen(계좌합계 대조로 자릿수 보정)"
            out.append(f"{g['file']}: {r.get('name')} 평가금액 {old:,.0f} → {r['value']:,.0f} "
                       f"(계좌합계 {tgt:,.0f}와 일치하도록 자릿수 보정 — 오독 교정)")
    return out


def _cross_check(groups, product_totals, account_totals, tol=0.02):
    """상세화면별 홀딩합을 요약 총액(상품별/계좌별)에 매칭 → 스코프·재현율·환각 점검.
    통화 추론 대신 '화면 합 ↔ 총액'으로 매칭(해외주식 상세합 128M ↔ 해외주식 총액 128M).
    하드 드롭 아님(종합 판단) — 불일치는 경고로 표면화."""
    warns, checks = [], []
    totals = ([{"label": k, "amt": v, "kind": "상품"} for k, v in product_totals.items()] +
              [{"label": a["atype"], "amt": a["total"], "kind": "계좌",
                "as_cash": a.get("_as_cash")} for a in account_totals])
    used = [False] * len(totals)

    for g in groups:
        if g["n"] == 0:
            continue
        # **화면 자신의 총액이 있으면 먼저 대조한다** — 같은 화면·같은 시점·같은 스코프라
        # 다른 화면의 총액보다 강한 근거다. 단 **대체가 아니라 추가 검사**다: 아래의 요약총액
        # 매칭을 건너뛰면 그 총액이 소비되지 않아 '미대조 상품총액' 거짓 경보가 난다
        # (측정: 정상 입력에 경고 3건 — 게이트 침묵이 깨졌다).
        st = g.get("screen_total")
        st_ok = None
        if st:
            st_ok = abs(g["sum"] - st) <= abs(st) * tol
            checks.append({"file": g["file"], "scope": "화면 총액", "sum": g["sum"],
                           "total": st, "match": st_ok})
            if not st_ok:
                warns.append(f"{g['file']}: 상세합 {g['sum']:,.0f} ≠ 화면 총액 {st:,.0f} "
                             f"({g['sum'] - st:+,.0f}) — 행 누락·오추출 의심"
                             f"(스크롤에 가려 안 찍힌 행이 있으면 이어서 내린 화면을 추가 업로드)")
        best, bi = None, -1
        for i, t in enumerate(totals):
            if used[i] or not t["amt"]:
                continue
            d = abs(g["sum"] - t["amt"])
            if best is None or d < best:
                best, bi = d, i
        if bi >= 0 and best <= abs(totals[bi]["amt"]) * tol:
            used[bi] = True
            checks.append({"file": g["file"], "scope": totals[bi]["label"],
                           "sum": g["sum"], "total": totals[bi]["amt"], "match": True})
        else:
            near = f"{totals[bi]['label']}({totals[bi]['amt']:,.0f})" if bi >= 0 else "없음"
            checks.append({"file": g["file"], "scope": None,
                           "sum": g["sum"], "total": None, "match": False})
            # 화면 자신의 총액이 있으면 이 뭉뚱그린 경고는 내지 않는다 — 성공했으면 더 강한
            # 근거로 검증이 끝난 것이고(거짓 경보), 실패했으면 바로 위에서 **차액까지 찍어**
            # 이미 경고했다(중복). 어느 쪽이든 여기서 덧붙일 정보가 없다.
            if st_ok is not None:
                continue
            # '검증 실패'와 '검증 불가'는 다른 말이다 — 대조할 총액 자체가 없으면 환각의
            # 증거가 아니라 근거 부재다(실측: 요약 없는 세트에서 전 화면에 '환각 의심'이
            # 남발돼 진짜 경고가 묻혔다). 문구를 갈라 행동 가능하게 한다.
            if bi >= 0:
                warns.append(f"{g['file']}: 상세합 {g['sum']:,.0f} — 근접 총액 {near} (환각·오추출 의심)")
            else:
                warns.append(f"{g['file']}: 상세합 {g['sum']:,.0f} — 대조할 총액 없음(검증 불가 — "
                             f"요약·총액이 보이는 화면을 함께 올리면 검증된다)")

    for i, t in enumerate(totals):
        if not used[i] and not t.get("as_cash"):   # 현금 계좌로 편입된 잔고는 누락이 아니다
            warns.append(f"미대조 {t['kind']}총액 {t['label']} {t['amt']:,.0f} "
                         f"— 해당 상세화면 없음(누락·재현율)")
    return {"warnings": warns, "checks": checks,
            "product_totals": product_totals,
            "account_totals": [{"atype": a["atype"], "total": a["total"]} for a in account_totals]}


if __name__ == "__main__":
    import glob, json
    d = os.path.join(os.path.dirname(HERE), "eval/results/batch8")
    screens = []
    for f in sorted(glob.glob(os.path.join(d, "*.jpg.json"))):
        j = json.load(open(f))
        screens.append({"file": j["image"], "raw": j["raw"]})
    out = finalize(screens, use_llm=False)
    print(json.dumps(out["screens"], ensure_ascii=False, indent=2))
    print("== holdings:", len(out["holdings"]))
    for h in out["holdings"]:
        print(f"  {h['broker']:>8} {h['accountType']:>6} {str(h['name'])[:22]:22} "
              f"{(h['value'] or 0):>14,.0f}")
    print("== gate warnings:", json.dumps(out["gate"]["warnings"], ensure_ascii=False, indent=2))
