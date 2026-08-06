#!/usr/bin/env python3
"""긴 화면 프로브 — **종목이 많아 화면이 길어질 때** bind.py가 견디는지.

정답(GT)이 없는 질문이라 자기일관성으로 심판한다: 본문 블록을 k번 복제하면
행 수는 k배가 되어야 하고, 각 행의 회계 항등식(value = cost + pnl)은 그대로여야 한다.

복제는 **구별되게** 해야 한다. 같은 문구를 그대로 반복하면 `rows_from_list`가 설계대로
'반복 = UI 크롬'으로 지우고, `finalize`도 같은 금액을 한 자산의 다른 표기로 합친다.
그래서 사본마다 종목명에 접미사를 붙이고 금액에 서로 다른 배수를 곱한다 — 배수는 세 값
(평가금액·매수금액·손익)에 똑같이 걸리므로 항등식은 보존된다.

사용: python3 eval/harness/probe_long.py [배수...]
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "agent"))
import bind                                              # noqa: E402
import ocr                                               # noqa: E402

SHOTS = os.environ.get("SHOTS", os.path.join(ROOT, "test-fixtures", "screenshots"))
NUM = re.compile(r"^([+\-]?)([\d,]+)(.*)$")


def _mul(text, f):
    """박스 텍스트 안의 첫 숫자에 배수를 건다(부호·꼬리 보존)."""
    m = NUM.match(text.strip())
    if not m or not m.group(2).replace(",", "").isdigit():
        return text
    v = int(m.group(2).replace(",", ""))
    if v == 0:
        return text
    return f"{m.group(1)}{v * f:,}{m.group(3)}"


def replicate(boxes, k):
    """본문 블록을 아래로 k−1번 덧붙인다. 사본마다 이름·금액을 구별되게 바꾼다."""
    m = bind.metrics(boxes)
    lines = bind.group_lines(bind.strip_chrome(boxes, m))
    hdr = bind.find_header(lines)
    cut = max(bind._cy(b) for b in hdr) if hdr else min(b["y"] for b in boxes)
    # 본문은 **마지막 금액까지**다. 하단 내비바까지 복제하면 사본마다 가짜 라벨 행이 생겨
    # 행 수가 부풀고, 그건 bind의 실패가 아니라 프로브의 실패다(실측 +64% 과다계수).
    last = max((b["y"] + b["h"] for b in boxes if bind._is_amount(b["text"])), default=None)
    body = [b for b in boxes if cut < b["y"] and (last is None or b["y"] <= last)]
    if not body:
        return boxes
    span = max(b["y"] + b["h"] for b in body) - min(b["y"] for b in body)
    out = list(boxes)
    for i in range(1, k):
        for b in body:
            t = b["text"]
            t = _mul(t, i + 1) if bind._is_amount(t) else (t + f"사본{i}" if t.strip() else t)
            out.append({**b, "text": t, "y": b["y"] + round(span * i) + m["h"] * 2 * i})
    return out


def main():
    ks = [int(a) for a in sys.argv[1:]] or [2, 4, 8, 16]
    imgs = sorted(f for f in os.listdir(SHOTS) if f.lower().endswith((".jpg", ".png")))
    worst = 0.0
    for f in imgs:
        boxes = ocr.recognize(os.path.join(SHOTS, f))
        base = bind.bind(boxes)
        if not base:
            continue
        cols = {c: i for i, c in enumerate(__import__("finalize").COMPACT_COLUMNS)}
        out = [f"{f[:22]:22s} 기본 {len(base):2d}행"]
        for k in ks:
            rows = bind.bind(replicate(boxes, k))
            exp = len(base) * k
            bad = 0
            for r in rows:                                # 회계 항등식 — 이웃 행 값 혼입 탐지
                v, c, p = (r[cols["value"]], r[cols["cost"]], r[cols["pnl"]])
                if None in (v, c, p):
                    continue
                if float(c) == 0 and float(p) == 0:
                    continue          # 현금·예수금은 취득원가가 없다 — 항등식 대상이 아니다
                if abs(float(v) - float(c) - float(p)) > max(1.0, abs(float(v)) * 1e-6):
                    bad += 1
            err = abs(len(rows) - exp) / exp
            worst = max(worst, err)
            out.append(f"×{k}: {len(rows):3d}/{exp:3d}행({err:+.0%}) 항등식위반 {bad}")
        print("  ".join(out), flush=True)
    print(f"\n최대 행수 오차 {worst:.0%}")


if __name__ == "__main__":
    main()
