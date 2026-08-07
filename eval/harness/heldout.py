#!/usr/bin/env python3
"""정답표가 없는 화면의 채점기 — **레포 자신의 불변식 + 두 추출 경로의 교차대조.**

`parity.py`는 `test-fixtures/ground-truth.json`이라는 정답 키가 있어야 한다. 그런데 우리가 가진
이미지 32장 중 24장은 정답이 없다(실사용 캡처·다른 날 캡처). 손으로 200행을 라벨링하는 대신
**틀리면 반드시 어긋나는 것**들만 본다:

  ① 회계 항등식 `value = cost + pnl`  (현금·예수금 행은 취득원가가 없어 제외)
  ② 게이트 경고 — 정상 입력엔 침묵해야 한다
  ③ 근거 없는 칸 — broker/symbol이 비었거나 `unverified`로 표시된 행
  ④ **두 경로(OCR vs 비전 LLM)의 불일치** ← 정답 없이 얻는 유일한 외부 신호

④가 핵심이다. 두 추출기는 서로를 모르고 실패 방식도 다르다(OCR=글리프 혼동, VLM=날조·행 누락).
**둘이 같은 값을 냈다면 그 값이 틀릴 확률은 낮고, 갈린 칸은 사람이 볼 목록이 된다.**
셋 다 아니면 자동으로 정답이 되는 건 없다 — 이 도구는 **판정이 아니라 조사 대상**을 낸다.

채점 로직은 새로 쓰지 않는다: `parity.run_pipeline`(=finalize→enrich, 서버와 같은 경로)을
그대로 부른다. 사본을 두면 여기서 잰 것이 서버에서 도는 것이라는 보장이 사라진다.

사용:
  SHOTS=<원본 이미지 디렉터리> python3 eval/harness/heldout.py <OCR결과dir> <VLM결과dir> [--json out]

`USE_LLM=1`이면 증권사 검색을 켠 Orin 조건, 기본(`0`)은 엣지 조건이다. 캐시가 없어진 뒤로는
이 스위치가 곧 **증권사가 풀리는가 아닌가**를 가르므로 조건을 명시해서 재야 한다.
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parity as P                                  # noqa: E402  채점 경로 단일 출처
sys.path.insert(0, os.path.join(P.ROOT, "agent"))
import finalize as F                                # noqa: E402

FIELDS = ("name", "broker", "accountType", "qty", "value", "cost", "price",
          "qty_src", "price_src", "symbol")
# 같은 자산으로 볼 평가금액 오차. **채점 허용치가 아니라 짝짓기 허용치다** — 넉넉히 잡아야
# 값이 갈린 행이 '한쪽에만 있는 행'으로 빠지지 않고 `칸 불일치`에 값 차이로 드러난다.
MATCH_TOL = 0.03


def is_cash(h):
    n = str(h.get("name") or "")
    return h.get("assetClass") == "현금" or any(k in n for k in ("예수금", "현금", "달러", "CMA"))


USE_LLM = os.environ.get("USE_LLM", "0") not in ("0", "", "no")


def run(d):
    screens = P.load_screens(d)
    rows, gate, _ = P.run_pipeline(screens, use_llm=USE_LLM)
    return rows, gate


def invariants(rows, gate):
    """틀리면 반드시 어긋나는 것들. 정답표가 필요 없다."""
    broken = []
    for h in rows:
        v, c, p = h.get("value"), h.get("cost"), h.get("pnl")
        if is_cash(h) or None in (v, c, p):
            continue
        if abs(v - c - p) > max(1.0, abs(v) * 0.001):
            broken.append(f"{str(h.get('name'))[:18]}: {v:,.0f} ≠ {c:,.0f}+{p:,.0f}")
    return {
        "행": len(rows),
        "항등식 위반": broken,
        "게이트 경고": gate["warnings"],
        "broker 미상": sum(1 for h in rows if not h.get("broker")),
        "symbol 미해석": sum(1 for h in rows if not h.get("symbol") and not is_cash(h)),
        "이름 미검증": sum(1 for h in rows if h.get("name_src") == "unverified"),
        "식별자 교정": [h["symbol_note"] for h in rows
                    if h.get("symbol_src") == "corrected:price-arbitration"],
    }


def pair(a_rows, b_rows):
    """두 경로의 행을 같은 자산끼리 짝짓는다 — 이름이 아니라 **계좌+평가금액**이 1차 키다
    (이름 표기는 경로마다 다르고, 갈린 이름 자체가 조사 대상이라 키로 쓰면 순환이다)."""
    used, pairs = [False] * len(b_rows), []
    for a in a_rows:
        hit = -1
        for i, b in enumerate(b_rows):
            if used[i] or not a.get("value") or not b.get("value"):
                continue
            if (a.get("broker") == b.get("broker") and a.get("accountType") == b.get("accountType")
                    and abs(a["value"] - b["value"]) / abs(b["value"]) < MATCH_TOL):
                hit = i
                break
        if hit < 0:                                  # 계좌 라벨이 갈렸을 수도 → 금액만으로 재시도
            for i, b in enumerate(b_rows):
                if used[i] or not a.get("value") or not b.get("value"):
                    continue
                if abs(a["value"] - b["value"]) / abs(b["value"]) < MATCH_TOL:
                    hit = i
                    break
        if hit >= 0:
            used[hit] = True
            pairs.append((a, b_rows[hit]))
        else:
            pairs.append((a, None))
    return pairs, [b for i, b in enumerate(b_rows) if not used[i]]


def diff(pairs, only_b):
    out = {"OCR에만": [], "VLM에만": [], "칸 불일치": []}
    for a, b in pairs:
        if b is None:
            out["OCR에만"].append({k: a.get(k) for k in ("name", "broker", "value")})
            continue
        for k in FIELDS:
            x, y = a.get(k), b.get(k)
            if k in ("value", "cost", "price", "qty") and None not in (x, y):
                if not x or abs(x - y) / abs(x) <= 0.01:
                    continue
            elif P.norm(x) == P.norm(y) if k == "name" else x == y:
                continue
            out["칸 불일치"].append({"자산": str(a.get("name"))[:22], "금액": a.get("value"),
                                 "칸": k, "OCR": x, "VLM": y})
    for b in only_b:
        out["VLM에만"].append({k: b.get(k) for k in ("name", "broker", "value")})
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        sys.exit(__doc__)
    (a_rows, a_gate), (b_rows, b_gate) = run(args[0]), run(args[1])
    rep = {"OCR": invariants(a_rows, a_gate), "VLM": invariants(b_rows, b_gate)}
    pairs, only_b = pair(a_rows, b_rows)
    rep["교차대조"] = diff(pairs, only_b)

    for tag in ("OCR", "VLM"):
        r = rep[tag]
        print(f"\n[{tag}] 행 {r['행']} · 항등식 위반 {len(r['항등식 위반'])} · 경고 {len(r['게이트 경고'])} "
              f"· broker 미상 {r['broker 미상']} · symbol 미해석 {r['symbol 미해석']} "
              f"· 이름 미검증 {r['이름 미검증']}")
        for x in r["항등식 위반"]:
            print("   ✗ 항등식 " + x)
        for x in r["게이트 경고"]:
            print("   ⚠ " + x)
        for x in r["식별자 교정"]:
            print("   ✎ " + x)
    d = rep["교차대조"]
    print(f"\n[교차대조] OCR에만 {len(d['OCR에만'])}행 · VLM에만 {len(d['VLM에만'])}행 "
          f"· 칸 불일치 {len(d['칸 불일치'])}건")
    for x in d["OCR에만"]:
        print(f"   ← {x}")
    for x in d["VLM에만"]:
        print(f"   → {x}")
    for x in d["칸 불일치"]:
        print(f"   ≠ {x['자산']:24} [{x['칸']}]  OCR={x['OCR']!r}  VLM={x['VLM']!r}")
    if "--json" in sys.argv:
        out = sys.argv[sys.argv.index("--json") + 1]
        json.dump(rep, open(out, "w"), ensure_ascii=False, indent=1, default=str)
        print(f"\n저장: {out}")


if __name__ == "__main__":
    main()
