#!/usr/bin/env python3
"""ML Kit 박스 단위 시뮬레이터 — A3의 유일한 미검증 위험을 **폰 없이** 잰다.

RapidOCR은 구절 단위 검출 박스를 내고 ML Kit은 Block > Line > Element 계층을 낸다.
`bind._amount_of`는 "금액과 수익률이 **한 박스**"를 전제한다(그 함수 주석 참조) — 박스
경계가 달라지면 손익이 사라지고 회계 항등식이 깨진다. 지금까지 "기기 없이는 확인 불가"로
적어두었지만, 확인할 수 없는 것은 **ML Kit의 실제 그룹핑**이고 확인할 수 있는 것은
**bind.py가 견디는 그룹핑의 범위**다. 후자를 여기서 잰다.

모델: ML Kit은 Element를 만든 뒤 Line으로 묶는다. 그래서 시뮬레이터도 같은 순서다 —
공백 토큰으로 쪼갠 뒤(=Element), 같은 줄에서 가로 간격이 `R × 글자높이` 이하인 것을
다시 합친다(=Line). **R 하나가 element(R=0)에서 block(R=∞)까지를 잇는 단조 노브**다.

읽는 법:
  · `element`  R 없이 쪼개기만 — ML Kit Element 근사
  · `line:R`   쪼갠 뒤 R로 재병합 — ML Kit Line 근사
  · R을 쓸어 **어디서 깨지는지**를 얻는다. 기기에서는 실제 간격 분포만 재서 이 봉투
    안에 드는지 보면 된다. 양성 대조군: 중간 R에서 RapidOCR 구절이 대략 복원되므로
    거기서 파리티가 통과해야 한다 — 통과하지 않으면 **시뮬레이터가 고장난 것**이다.

정직하게: 이건 근사다. **깨지면 결론적**이고(bind가 박스 경계에 의존함이 증명된다),
버티면 약한 증거다(실제 ML Kit이 다르게 자를 수 있다).
"""
import re

_CJK = re.compile(r"[ᄀ-ᇿ　-〿㄰-㆏가-힯一-鿿]")


def _cw(c):
    """글자 폭 가중치 — 한글·한자는 라틴·숫자의 두 배로 본다(x 보간용)."""
    return 2.0 if _CJK.match(c) else 1.0


def split_elements(boxes):
    """공백 토큰 단위로 쪼갠다 — ML Kit Element 근사.

    x는 글자 폭 가중치로 보간한다. 실제 글리프 어드밴스가 아니라 근사지만, 열 배정은
    박스 중심의 x 밴드로 이뤄지므로 토큰 경계만 맞으면 밴드는 유지된다.
    """
    out = []
    for b in boxes:
        text = str(b["text"])
        total = sum(_cw(c) for c in text)
        if total <= 0:
            continue
        cum = 0.0
        for part in re.split(r"(\s+)", text):
            wgt = sum(_cw(c) for c in part)
            if part.strip():
                x0 = b["x"] + b["w"] * cum / total
                x1 = b["x"] + b["w"] * (cum + wgt) / total
                out.append({**b, "text": part,
                            "x": int(round(x0)), "w": max(1, int(round(x1 - x0)))})
            cum += wgt
    return out


# 줄 묶기·병합은 **프로덕션과 같은 코드**를 쓴다 — 시뮬레이터가 자기 사본을 갖고 있으면
# 여기서 잰 봉투(R∈[0.6,1.8])가 기기에서 도는 코드의 봉투라는 보장이 사라진다.
from ocr import _rows_of, merge_lines            # noqa: E402  (run_ocr.py가 agent/를 경로에 넣는다)


def apply(spec, boxes):
    """spec: 'element' | 'line:R' | 'off'."""
    if not spec or spec == "off":
        return boxes
    if spec == "element":
        return split_elements(boxes)
    if spec.startswith("line:"):
        return merge_lines(split_elements(boxes), float(spec.split(":", 1)[1]))
    raise ValueError(f"알 수 없는 MLKIT_SIM: {spec}")


def gap_stats(boxes):
    """같은 줄 이웃 간 (간격 / 글자높이) 분포 — 기기에서 잰 값과 비교할 봉투의 좌표."""
    gaps = []
    for r in _rows_of(split_elements(boxes)):
        r.sort(key=lambda b: b["x"])
        for a, b in zip(r, r[1:]):
            gaps.append((b["x"] - (a["x"] + a["w"])) / max(a["h"], b["h"], 1))
    return sorted(gaps)
