#!/usr/bin/env python3
"""기기 다양성 시뮬레이터 — 박스 좌표만 변환해 **bind.py의 배율 의존성**을 OCR과 분리해 잰다.

기기가 바뀌면 화면은 세 가지로 달라진다. 셋은 서로 다른 변환이고 섞으면 안 된다:

  scale   해상도(DPI)가 다르다 — **전부 비례**한다(위치·크기·글자높이). 가장 단순.
  wider   폭이 넓다 — **글자 크기는 그대로**이고 열만 벌어진다. 좌측 정렬은 왼쪽 기준,
          우측 정렬은 오른쪽 기준으로 움직인다.
  looser  행 간격이 성기다/촘촘하다 — **줄 사이 간격만** 달라진다. 한 줄 안에서 박스들의
          y가 몇 px씩 흔들리는 것은 글꼴 렌더링 속성이라 **같이 늘리면 안 된다**.

마지막 두 개는 순진하게 구현하면 안 된다. 실제로 두 번 거짓 경보를 냈다 —
  · x만 늘렸더니 박스 너비가 안 따라와 **우측 정렬이 깨졌다.** '넓은 화면'이 아니라
    '정렬이 무너진 화면'을 재고 있었다.
  · x와 w를 같이 늘렸더니 이번엔 **글자가 두 배로 넓어졌다**(폭 넓은 기기는 글꼴을
    늘리지 않는다). 헤더 화면이 0행으로 무너졌다.
  · y를 통째로 늘렸더니 **줄 안의 y 흔들림까지 늘어나** 한 줄이 두 줄로 쪼개졌다.
어느 경우든 bind.py가 아니라 시뮬레이터가 틀린 것이었다. 도구를 의심하는 편이 빨랐다.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "agent"))
import bind as B                                     # noqa: E402  줄 그룹핑은 프로덕션 것을 쓴다


def scale(boxes, s):
    """해상도만 다른 기기 — 전부 비례 확대(글자높이 포함)."""
    return [{**b, "x": round(b["x"] * s), "y": round(b["y"] * s),
             "w": round(b["w"] * s), "h": round(b["h"] * s)} for b in boxes]


def wider(boxes, s):
    """폭만 다른 기기 — 글자 크기는 그대로, 열이 벌어진다.

    정렬 기준을 보존한다: 화면 왼쪽 절반의 박스는 **왼쪽 끝**을, 오른쪽 절반의 박스는
    **오른쪽 끝**을 기준으로 옮긴다. 그래야 벌어진 뒤에도 금액 열의 우측 정렬이 유지된다
    (표는 금액을 우측 정렬하고 bind는 그 오른쪽 끝을 열 앵커로 쓴다).
    """
    m = B.metrics(boxes)
    mid = m["x0"] + m["span"] / 2.0
    out = []
    for b in boxes:
        if b["x"] + b["w"] / 2.0 < mid:
            x = b["x"] * s
        else:
            x = (b["x"] + b["w"]) * s - b["w"]
        out.append({**b, "x": round(x)})
    return out


def looser(boxes, s):
    """행 간격만 다른 레이아웃 — 줄 **사이** 간격만 늘리고 줄 **안**은 그대로 둔다.

    한 줄 안에서 박스들의 y가 몇 px 다른 것은 글꼴 렌더링이지 레이아웃이 아니다.
    같이 늘리면 줄 그룹핑이 한 줄을 두 줄로 쪼개 행 수가 배로 뛴다(실측).
    """
    lines = B.group_lines(boxes)
    if not lines:
        return boxes
    cys = [sum(B._cy(x) for x in ln) / len(ln) for ln in lines]
    base = cys[0]
    out = []
    for ln, cy in zip(lines, cys):
        shift = round((cy - base) * (s - 1))
        out += [{**b, "y": b["y"] + shift} for b in ln]
    return out


def apply(boxes, s_scale=1.0, s_wide=1.0, s_loose=1.0):
    if s_scale != 1.0:
        boxes = scale(boxes, s_scale)
    if s_wide != 1.0:
        boxes = wider(boxes, s_wide)
    if s_loose != 1.0:
        boxes = looser(boxes, s_loose)
    return boxes
