#!/usr/bin/env python3
"""여러 계좌가 섞인 '전체계좌' 화면에서 행이 계좌별로 갈리는가.

합성이지만 **실제 박스 기하를 그대로 쓴다** — 진짜 img1의 OCR 결과에서 계좌 구분자
텍스트만 바꾼다. 좌표·줄 구조·금액 배치는 손대지 않으므로 시험되는 건 바인더의
'바로 위 구분자' 로직 하나다.
"""
import copy, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "agent"))
import ocr, bind  # noqa: E402

IMG = "/home/omr/portf-agent/captures/20260731T085311Z_792d11/img1.png"
A = "1234-5678-01(Super365)"
B = "1234-5678-77(Super365)"      # 같은 브랜드, 다른 계좌번호

base = ocr.recognize(IMG)


def run(name, mutate):
    boxes = copy.deepcopy(base)
    seps = sorted([b for b in boxes if A in b["text"]], key=lambda b: b["y"])
    mutate(seps)
    rows = bind.bind(boxes)
    print(f"\n=== {name}  (구분자 {len(seps)}개 → 행 {len(rows)}개)")
    for r in rows:
        print(f"   {str(r[0]):26s} {str(r[2])[:24]:24s} qty={r[5]} value={r[7]}")
    return rows


# 1) 절반씩 두 계좌 — 각 5행
run("두 계좌 5:5", lambda s: [b.update(text=B) for b in s[5:]])

# 2) 마지막 계좌에 종목이 **하나뿐** — 구분자가 1회만 등장한다
run("두 계좌 9:1 (B는 단일 종목)", lambda s: [b.update(text=B) for b in s[9:]])

# 3) 계좌가 번갈아 나오는 경우
def alt(s):
    for i, b in enumerate(s):
        if i % 2:
            b.update(text=B)
run("교대 A/B/A/B", alt)
