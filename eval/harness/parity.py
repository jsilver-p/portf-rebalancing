#!/usr/bin/env python3
"""파리티 채점기 — 파이프라인 **최종 출력**을 정답표와 대조한다.

기존 score.py는 *화면별* 원출력을 화면별 정답과 채점했다. 그 국소 점수가 100%인데도 파이프라인이
깨졌다(화면 통째 누락·broker 오정규화). 그래서 채점 대상을 앱이 실제로 받는 것과 같은
`finalize(게이트·broker 정규화) → enrich(심볼·수량사다리·가격)` 최종 holdings로 올린다.

정답표는 **채점 키일 뿐**이다. 추출 경로(프롬프트·finalize·enrich)는 이 파일을 임포트하지 않는다.
연결은 단방향(파이프라인 결과 → 채점).

사용:
  python3 eval/harness/parity.py <results_dir> [--no-llm] [--controls]
    results_dir: run_extract.py 산출(<이미지명>.json = {"image","raw"})
    --controls : 게이트 대조군(순서 셔플 / 화면 누락 / 환각행 주입 / 빈 화면)도 함께 실행
"""
import base64, json, os, random, re, sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "agent"))
import finalize as F                       # noqa: E402
import server as S                         # noqa: E402


def norm(s):
    """종목명 정규화 — 괄호주석·공백·기호 제거(score.py와 동일 규칙).
    화면 'NC' ↔ GT 'NC(엔씨소프트)', 화면 '메타 플랫폼스(페이스북)' ↔ GT '메타 플랫폼스 (META)'."""
    if not s:
        return ""
    s = re.sub(r"\(.*?\)", "", str(s))
    return re.sub(r"[\s·\-_.,]", "", s).lower()

GT_PATH = os.path.join(ROOT, "test-fixtures", "ground-truth.json")
SHOTS = os.path.join(ROOT, "test-fixtures", "screenshots")
VAL_TOL = 0.01        # 평가금액: 화면값 그대로여야 함(1% — OCR 오차 아닌 것만 통과)
PRICE_TOL = 0.02      # 주가: 파생값(반올림·시간외가) 여유


def src_kind(s):
    """출처 문자열 → 범주. GT와 파이프라인이 문구는 달라도 '근거의 종류'는 같아야 한다."""
    s = str(s or "")
    if not s or s == "None":
        return "none"
    if "cross-account" in s:
        return "cross-account"
    if "capture-close" in s or "캡처일" in s:
        return "capture-close"
    if s.startswith("screen"):
        return "screen"
    if s.startswith("computed"):
        return "computed"
    if "unobtainable" in s:
        return "none"
    return s


def load_screens(d):
    out = []
    for f in sorted(os.listdir(d)):
        if not f.endswith(".json"):
            continue
        j = json.load(open(os.path.join(d, f)))
        out.append({"file": j["image"], "raw": j["raw"]})
    return out


def capture_dt():
    """캡처시각은 스크린샷 EXIF에서(앱 경로와 동일 근거). 실패 시 서버 기본값."""
    for f in sorted(os.listdir(SHOTS)):
        if f.lower().endswith((".jpg", ".png")):
            b64 = base64.b64encode(open(os.path.join(SHOTS, f), "rb").read()).decode()
            dt = S.exif_capture_dt(b64)
            if dt:
                return dt
    return datetime.now(S.KST)


def run_pipeline(screens, use_llm=True):
    """앱이 타는 경로와 동일: finalize → enrich."""
    fin = F.finalize(screens, use_llm=use_llm)
    rows = S.enrich(fin["holdings"], capture_dt())   # _file은 화면단위 게이트(현금 병합)에 필요
    for h in rows:
        h.pop("_file", None)
    return rows, fin["gate"], fin["screens"]


def score(rows, gt):
    """재현율·환각·필드정확도·broker/유형·출처 범주."""
    gtl = list(gt["holdings"])
    fx0 = gt.get("fx_usd_krw") or 1
    used, matched, halluc = [False] * len(gtl), [], []

    def krw(h):     # 파이프라인은 USD 자산을 네이티브로 낸다 → 금액 비교는 원화 실질로
        v = h.get("value")
        return None if v is None else v * (fx0 if h.get("fx_applied") else 1)

    for h in rows:
        n = norm(h.get("name"))
        hit = -1
        for i, g in enumerate(gtl):          # ① 종목명
            if used[i]:
                continue
            gn = norm(g["name"])
            if n and gn and (n == gn or n in gn or gn in n):
                hit = i
                break
        if hit < 0:                          # ② 계좌+금액 — 보유자산의 정체는 이름만이 아니다.
            v = krw(h)                       #    (같은 계좌의 같은 금액 = 같은 자산. 이름 표기는 화면마다 다르다)
            for i, g in enumerate(gtl):
                if used[i] or not v or not g.get("value"):
                    continue
                if (g["broker"] == h.get("broker") and g["accountType"] == h.get("accountType")
                        and abs(v - g["value"]) / g["value"] < 0.005):
                    hit = i
                    break
        if hit < 0:
            halluc.append(h)
        else:
            used[hit] = True
            matched.append((gtl[hit], h))
    missing = [g for i, g in enumerate(gtl) if not used[i]]

    err = {"name": [], "value": [], "qty": [], "price": [], "cost": [], "broker": [],
           "accountType": [], "qty_src": [], "price_src": []}
    fx = gt.get("fx_usd_krw") or 1
    for g, h in matched:
        nm = g["name"][:20]
        gn, hn = norm(g["name"]), norm(h.get("name"))
        if not (gn and hn and (gn == hn or gn in hn or hn in gn)):
            err["name"].append(f"{nm}: 추출='{h.get('name')}'")   # 금액으로 매칭된 건 = 이름 표기 불일치
        # 정답표는 금액을 원화로 적었고(화면 표기), 파이프라인은 USD 자산을 네이티브(달러)로 낸다
        # (앱이 fx로 환산하므로). 표현이 아니라 **경제적 실질**을 비교한다 → 원화로 맞춰 대조.
        conv = fx if (h.get("currency") == "USD" and h.get("fx_applied")) else 1
        for k, tol in (("value", VAL_TOL), ("price", PRICE_TOL), ("cost", VAL_TOL)):
            gv, hv = g.get(k), h.get(k)
            if k in ("value", "cost") and hv is not None:
                hv = hv * conv
            if gv is None and hv is None:
                continue
            if gv is None or hv is None:
                err[k].append(f"{nm}: GT={gv} 추출={hv}")
            elif gv and abs(gv - hv) / abs(gv) > tol:
                err[k].append(f"{nm}: GT={gv:,.2f} 추출={hv:,.2f}")
        if (g.get("qty") or 0) != (h.get("qty") or 0):
            err["qty"].append(f"{nm}: GT={g.get('qty')} 추출={h.get('qty')}")
        for k in ("broker", "accountType"):
            if g.get(k) != h.get(k):
                err[k].append(f"{nm}: GT={g.get(k)} 추출={h.get(k)}")
        for k in ("qty_src", "price_src"):
            if src_kind(g.get(k)) != src_kind(h.get(k)):
                err[k].append(f"{nm}: GT={src_kind(g.get(k))} 추출={src_kind(h.get(k))}")
    return matched, missing, halluc, err


def report(rows, gate, screens, gt):
    matched, missing, halluc, err = score(rows, gt)
    n = len(gt["holdings"])
    print(f"\n{'='*72}\n화면 유형: " + ", ".join(f"{s['file']}={s['type']}" for s in screens))
    print(f"재현율   {len(matched)}/{n}" + ("  ✅" if len(matched) == n else "  ❌"))
    print(f"환각     {len(halluc)}" + ("  ✅" if not halluc else "  ❌ " +
          ", ".join(str(h.get('name'))[:18] for h in halluc)))
    if missing:
        print("  누락: " + ", ".join(g["name"][:20] for g in missing))
    for k in ("name", "value", "qty", "price", "cost", "broker", "accountType",
              "qty_src", "price_src"):
        e = err[k]
        ok = len(matched) - len(e)
        print(f"{k:12} {ok:>2}/{len(matched):<2} " + ("✅" if not e else "❌ " + " | ".join(e[:3])))
    print(f"게이트 경고 {len(gate['warnings'])}건" + ("  (침묵 — 정상)" if not gate["warnings"] else ""))
    for w in gate["warnings"]:
        print("  ⚠ " + w)
    ok = (len(matched) == n and not halluc and not any(err[k] for k in
          ("value", "qty", "broker", "accountType", "qty_src")))
    print(f"\n판정: {'PASS ✅' if ok else 'FAIL ❌'}\n{'='*72}")
    return ok


def controls(screens, gt, use_llm):
    """게이트 대조군 — 정상은 침묵, 훼손은 반드시 경고. 못 잡으면 게이트가 아니다."""
    print("\n\n### 게이트 대조군 (음성 대조: 훼손 입력 → 경고 필수)")

    def warns(scr, tag):
        _, gate, sc = run_pipeline(scr, use_llm=use_llm)
        w = gate["warnings"]
        print(f"  {tag:28} 경고 {len(w)}건 " + ("❌ 못 잡음" if not w else "✅ 잡음"))
        for x in w[:2]:
            print(f"      ⚠ {x}")
        return len(w)

    # 1) 화면 하나 누락(가장 큰 상세) — 미대조 총액 경고가 떠야
    big = max(screens, key=lambda s: len(F.parse_rows(s["raw"])))
    warns([s for s in screens if s is not big], f"화면 누락({big['file']})")
    # 2) 빈 화면 주입 — 빈 상세를 조용히 통과시키면 안 됨
    warns(screens + [{"file": "empty.jpg", "raw": "[]"}], "빈 화면 주입")
    # 3) 환각행 주입 — 상세합이 총액과 어긋나야
    bad = [dict(s) for s in screens]
    tgt = max(bad, key=lambda s: len(F.parse_rows(s["raw"])))
    rows = F.parse_rows(tgt["raw"])
    rows.append({"broker": rows[0].get("broker"), "name": "환각종목", "currency": "KRW",
                 "qty": 999, "value": 99000000, "cost": 99000000})
    tgt["raw"] = json.dumps(rows, ensure_ascii=False)
    warns(bad, "환각행 주입(+99,000,000)")
    # 4) 순서 셔플 — 결과가 같아야(일반화: 투입 순서 의존성 0)
    sh = list(screens)
    random.Random(7).shuffle(sh)
    r1, _, _ = run_pipeline(screens, use_llm=use_llm)
    r2, _, _ = run_pipeline(sh, use_llm=use_llm)
    key = lambda rs: sorted((str(h.get("name")), h.get("broker"), h.get("value")) for h in rs)
    print(f"  {'순서 셔플 → 동일 결과':28} " + ("✅ 동일" if key(r1) == key(r2) else "❌ 순서 의존!"))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_llm = "--no-llm" not in sys.argv
    d = args[0] if args else None
    if not d or not os.path.isdir(d):
        sys.exit(f"사용: parity.py <results_dir> [--no-llm] [--controls]\n(없음: {d})")
    gt = json.load(open(GT_PATH))
    screens = load_screens(d)
    print(f"입력 {len(screens)}화면 ← {d}")
    rows, gate, sc = run_pipeline(screens, use_llm=use_llm)
    ok = report(rows, gate, sc, gt)
    if "--controls" in sys.argv:
        controls(screens, gt, use_llm)
    sys.exit(0 if ok else 1)
