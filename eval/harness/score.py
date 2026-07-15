#!/usr/bin/env python3
"""Gate 1 채점기. 모델 원출력을 expected.json(화면 정답)과 대조.
매칭: 종목명 정규화 후 최적 매칭 → holdings 재현율/정밀도 + 매칭행의 value/qty/cost 정확도.
하드코딩 없음: expected는 채점 키일 뿐 모델엔 미제공."""
import json, os, re, sys, glob

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXPECTED = json.load(open(os.path.join(ROOT, "eval/harness/expected.json")))

def norm(s):
    if not s: return ""
    s = re.sub(r"\(.*?\)", "", str(s))                 # 괄호 주석 제거
    return re.sub(r"[\s·\-_.,]", "", s).lower()

def parse_json(raw):
    if not raw or raw.startswith("__ERROR__"): return None
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if m: raw = m.group(1)
    i, j = raw.find("["), raw.rfind("]")
    if i < 0 or j < 0: return None
    try: return json.loads(raw[i:j+1])
    except Exception:
        try: return json.loads(re.sub(r",\s*([\]}])", r"\1", raw[i:j+1]))
        except Exception: return None

def num(x):
    if x is None: return None
    if isinstance(x, (int, float)): return float(x)
    try: return float(re.sub(r"[^\d.\-]", "", str(x)))
    except Exception: return None

def match_name(exp_name, rows, used):
    en = norm(exp_name)
    for idx, r in enumerate(rows):
        if idx in used: continue
        rn = norm(r.get("name"))
        if rn and (rn == en or rn in en or en in rn):
            return idx
    return None

def score_model(model_dir):
    files = glob.glob(os.path.join(model_dir, "*.json"))
    agg = {"img": 0, "sec": 0.0, "exp": 0, "found": 0, "extra": 0,
           "val_ok": 0, "val_tot": 0, "qty_ok": 0, "qty_tot": 0, "cost_ok": 0, "cost_tot": 0,
           "parse_fail": 0, "detail_rows_ok": 0}
    perimg = []
    for fp in sorted(files):
        d = json.load(open(fp))
        img = d["image"]
        if img not in EXPECTED: continue
        agg["img"] += 1
        if d["seconds"] and d["seconds"] > 0: agg["sec"] += d["seconds"]
        exp = EXPECTED[img]["expected"]
        rows = parse_json(d["raw"])
        if rows is None:
            agg["parse_fail"] += 1
            rows = []
        used, found = set(), 0
        vok = vtot = qok = qtot = cok = ctot = 0
        for e in exp:
            agg["exp"] += 1
            idx = match_name(e["name"], rows, used)
            if idx is None: continue
            used.add(idx); found += 1; agg["found"] += 1
            r = rows[idx]
            if "value" in e and e["value"] is not None:
                vtot += 1; agg["val_tot"] += 1
                if num(r.get("value")) == float(e["value"]): vok += 1; agg["val_ok"] += 1
            if "qty" in e and e["qty"] is not None:
                qtot += 1; agg["qty_tot"] += 1
                if num(r.get("qty")) == float(e["qty"]): qok += 1; agg["qty_ok"] += 1
            if "cost" in e and e.get("cost") is not None:
                ctot += 1; agg["cost_tot"] += 1
                if num(r.get("cost")) == float(e["cost"]): cok += 1; agg["cost_ok"] += 1
        extra = max(0, len(rows) - found)
        agg["extra"] += extra
        perimg.append({"image": img, "type": EXPECTED[img]["type"], "sec": d["seconds"],
                       "exp": len(exp), "found": found, "extra": extra,
                       "value": f"{vok}/{vtot}", "qty": f"{qok}/{qtot}", "cost": f"{cok}/{ctot}"})
    return agg, perimg

def pct(a, b): return f"{100*a/b:.0f}%" if b else "—"

def main():
    model_dir = sys.argv[1]
    agg, perimg = score_model(model_dir)
    print(f"\n===== {os.path.basename(model_dir)} =====")
    print(f"{'image':40s} {'type':8s} {'sec':>7s} {'exp':>4s} {'found':>6s} {'extra':>6s} {'value':>7s} {'qty':>6s} {'cost':>6s}")
    for p in perimg:
        print(f"{p['image'][:40]:40s} {p['type']:8s} {p['sec']:>7.0f} {p['exp']:>4d} {p['found']:>6d} {p['extra']:>6d} {p['value']:>7s} {p['qty']:>6s} {p['cost']:>6s}")
    print("-"*100)
    print(f"이미지 {agg['img']} · 총시간 {agg['sec']:.0f}s (평균 {agg['sec']/max(agg['img'],1):.0f}s/img) · 파싱실패 {agg['parse_fail']}")
    print(f"종목 재현율 found/exp = {agg['found']}/{agg['exp']} ({pct(agg['found'],agg['exp'])})  · 초과(환각) {agg['extra']}")
    print(f"평가금액 정확 {agg['val_ok']}/{agg['val_tot']} ({pct(agg['val_ok'],agg['val_tot'])})"
          f"  · 수량 {agg['qty_ok']}/{agg['qty_tot']} ({pct(agg['qty_ok'],agg['qty_tot'])})"
          f"  · 매수금액 {agg['cost_ok']}/{agg['cost_tot']} ({pct(agg['cost_ok'],agg['cost_tot'])})")

if __name__ == "__main__":
    main()
