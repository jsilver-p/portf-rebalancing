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


def parse_rows(raw):
    """비전 원문 텍스트 → 행 리스트(느슨한 JSON 파싱). server.parse_json과 동일 정신."""
    import json
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if m:
        raw = m.group(1)
    i, j = raw.find("["), raw.rfind("]")
    if i < 0 or j < 0:
        return []
    frag = raw[i:j + 1]
    for f in (frag, re.sub(r",\s*([\]}])", r"\1", frag)):
        try:
            d = json.loads(f)
            return d if isinstance(d, list) else []
        except Exception:
            continue
    return []


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
        return "detail"
    n = len(rows)
    cat = sum(1 for r in rows if is_category(r.get("name")))
    if cat >= max(2, n * 0.5):
        return "product_summary"
    sec = sum(1 for r in rows if _security_like(r))
    if sec >= max(1, n * 0.5):
        return "detail"
    return "account_summary"


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
        parsed.append({"file": sc.get("file"), "rows": rows, "type": classify(rows)})

    # 1) 요약화면에서 totals + 앱 정규 증권사명 수집
    product_totals = {}     # 카테고리 → 총액
    account_totals = []     # [{key, atype, total}]
    summary_broker = None   # 요약화면에서 읽힌 정규 증권사명(계좌요약 탭 등)
    for p in parsed:
        if p["type"] == "product_summary":
            for r in p["rows"]:
                if is_category(r.get("name")) and r.get("value") is not None:
                    product_totals[r["name"].strip()] = r["value"]
        elif p["type"] == "account_summary":
            for r in p["rows"]:
                key = str(r.get("broker") or r.get("name") or "").strip()
                acct_name = " ".join(str(r.get(k, "")) for k in ("broker", "accountType", "name"))
                c = RB.canonical_in(acct_name)
                if c and not summary_broker:
                    summary_broker = c
                if r.get("value") is not None:
                    account_totals.append({"key": key,
                                           "atype": norm_atype(acct_name),
                                           "nick": acct_name, "total": r["value"]})

    # 2) 상세화면 홀딩만 수집 + broker 정규화 + 화면별 그룹(게이트용)
    holdings, groups = [], []
    for p in parsed:
        if p["type"] != "detail":
            continue
        label = str((p["rows"][0].get("broker") if p["rows"] else "") or "")
        screen_text = " ".join(str(r.get(k, "")) for r in p["rows"]
                               for k in ("broker", "accountType", "name"))
        broker = RB.resolve_broker(label, broker_cache, use_llm=use_llm)
        if not broker:                       # 계좌번호/별칭 라벨
            broker = RB.canonical_in(screen_text)   # 화면 어딘가의 정규명(예: '…(삼성증권)')
        if not broker:                       # 그래도 없으면 요약 정규명 상속
            broker = summary_broker
        grp = []
        for r in p["rows"]:
            if is_category(r.get("name")):   # 상세화면에 섞인 집계행 방어
                continue
            h = {k: r.get(k) for k in ("name", "assetClass", "currency", "qty",
                                       "price", "value", "cost")}
            h["broker"] = broker or label
            h["accountType"] = norm_atype(str(r.get("accountType") or label))
            h["_file"] = p["file"]
            holdings.append(h); grp.append(h)
        groups.append({"file": p["file"], "sum": sum(x["value"] or 0 for x in grp),
                       "n": len(grp)})

    gate = _cross_check(groups, product_totals, account_totals)
    RB.save_cache(broker_cache)
    return {"holdings": holdings, "gate": gate,
            "screens": [{"file": p["file"], "type": p["type"]} for p in parsed]}


def _cross_check(groups, product_totals, account_totals, tol=0.02):
    """상세화면별 홀딩합을 요약 총액(상품별/계좌별)에 매칭 → 스코프·재현율·환각 점검.
    통화 추론 대신 '화면 합 ↔ 총액'으로 매칭(해외주식 상세합 128M ↔ 해외주식 총액 128M).
    하드 드롭 아님(종합 판단) — 불일치는 경고로 표면화."""
    warns, checks = [], []
    totals = ([{"label": k, "amt": v, "kind": "상품"} for k, v in product_totals.items()] +
              [{"label": a["atype"], "amt": a["total"], "kind": "계좌"} for a in account_totals])
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
        if not used[i]:
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
