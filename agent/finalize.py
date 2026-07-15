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
import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import resolve_broker as RB

# 상품 카테고리(요약화면 총액 라벨) — 특정 증권사 무관한 일반 금융용어
_CATEGORIES = ("국내주식", "해외주식", "원화예수금", "외화예수금", "국내채권", "해외채권",
               "펀드", "파생상품", "연금", "현금", "채권", "주식")
_CASH_CAT = ("예수금", "예금", "현금", "CMA")
# accountType 정규화(GT 어휘: 일반/연금저축/IRP/ISA/퇴직연금)
_ATYPE = (("IRP", "IRP"), ("ISA", "ISA"), ("연금저축", "연금저축"),
          ("퇴직연금", "퇴직연금"), ("퇴직", "IRP"), ("일반", "일반"))


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


def finalize(screens, use_llm=True, broker_cache=None):
    """screens: [{"file":str,"raw":str}]. 반환 {holdings, gate}."""
    if broker_cache is None:
        broker_cache = RB.load_cache()
    parsed = []
    for sc in screens:
        rows = parse_rows(sc.get("raw", ""))
        for r in rows:
            r["value"] = _num(r.get("value"))
            r["cost"] = _num(r.get("cost"))
            r["qty"] = _num(r.get("qty"))
            r["pnl"] = _num(r.get("pnl"))
        parsed.append({"file": sc.get("file"), "rows": rows, "type": classify(rows)})

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
    for p in parsed:
        if p["type"] != "detail":
            continue
        label = str((p["rows"][0].get("broker") if p["rows"] else "") or "")
        screen_text = " ".join(str(r.get(k, "")) for r in p["rows"]
                               for k in ("broker", "accountType", "name"))
        broker = RB.resolve_broker(label, broker_cache, use_llm=use_llm)   # 정규명·브랜드(검색)
        if not broker:
            broker = RB.canonical_in(screen_text)   # 화면 어딘가의 정규명(예: '현금성자산(삼성증권)')
        inh_broker, inh_atype = inherit(screen_text)
        broker = broker or inh_broker               # 계좌번호·별칭뿐이면 같은 계좌의 요약에서 상속
        grp, seen_vals = [], {}
        for r in p["rows"]:
            v = r.get("value")
            if not v:                        # 평가금액 0/없음 = 리밸런싱 대상 아님(수표·미사용 항목 등)
                continue
            if v in seen_vals:               # 같은 화면에 같은 금액이 반복 = 같은 자산의 다른 표기
                continue                     # (예수금 화면의 당일/D+1/D+2/출금가능금액) → 한 자산 한 행
            seen_vals[v] = True
            h = {k: r.get(k) for k in ("name", "assetClass", "currency", "qty",
                                       "price", "value", "cost", "pnl")}
            h["broker"] = broker or label or None
            atype = str(r.get("accountType") or "")
            # 유형도 계좌 단위 상속: 상세화면 라벨은 줄임말이기 쉽다('퇴직연금' ← '퇴직연금(다이렉트IRP)')
            h["accountType"] = inh_atype or norm_atype(atype or label)
            h["_file"] = p["file"]
            grp.append(h)
        grp = _drop_total_rows(grp)          # 화면 제목·탭·소계가 종목처럼 섞여 나오는 것 제거
        holdings.extend(grp)
        groups.append({"file": p["file"], "sum": sum(x["value"] or 0 for x in grp),
                       "n": len(grp), "rows": grp})

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

    repairs = _repair_digit_slips(groups, product_totals, account_totals)
    for g in groups:                     # 보정 후 합계 갱신(게이트가 보정된 값을 보게)
        g["sum"] = sum(x["value"] or 0 for x in g["rows"])
    gate = _cross_check(groups, product_totals, account_totals)
    gate["repairs"] = repairs
    gate["warnings"] = bad_totals + repairs + gate["warnings"]
    for p in parsed:                     # 빈 화면 = 추출 실패. 조용히 넘기지 않는다.
        if p["type"] == "empty":
            gate["warnings"].insert(0, f"{p['file']}: 추출 0행 — 화면 유실(파싱 실패·미인식) 의심")
    RB.save_cache(broker_cache)
    return {"holdings": holdings, "gate": gate,
            "screens": [{"file": p["file"], "type": p["type"]} for p in parsed]}


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
            warns.append(f"{g['file']}: 상세합 {g['sum']:,.0f} — 근접 총액 {near} (환각·오추출 의심)")

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
