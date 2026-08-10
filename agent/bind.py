#!/usr/bin/env python3
"""기하 바인더 — OCR 박스 → `finalize.COMPACT_COLUMNS` 11칸 배열. **추출 지능의 단일 출처.**

비전 LLM이 하던 일을 좌표로 되돌린다. VLM은 박스 좌표를 버린 뒤 프롬프트로 그 좌표를 자연어
복원하라고 지시받았다(“먼저 헤더 행을 읽어 각 열이 무엇인지 파악하라”, “한 칸에 숫자가 위아래로
쌓이면 헤더 순서대로 배정하라”). OCR은 좌표를 버리지 않으므로 그 규칙들이 그대로 기하 연산이 된다.

단계:
  1) 화면 크롬 제거 — 상태바·내비바·시장지수 줄(프롬프트 규칙 2의 어휘를 그대로 쓴다)
  2) 줄 그룹핑 — y 중심 클러스터
  3) 헤더 탐지 → 열 x밴드 + **밴드 내 y 순서 = 데이터 스택 순서**
  4) 행 밴드 — 오른쪽 금액 열을 앵커로 스택 깊이만큼 묶는다(모든 보유행엔 금액이 있다)
  5) 이름 조립 — 왼쪽 열의 조각을 이어붙인다(표는 종목명을 2~3줄로 감는다)
  6) 숫자 정규화(`finalize._sanitize` 규약과 동일) + assetClass/accountType 키워드 표

출력은 파서(`finalize.parse_rows`)가 그대로 받는 형식이라, 이 아래(게이트·계좌합계 대조·enrich
수량 사다리)는 **한 줄도 바뀌지 않는다**.
"""
import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import finalize as F                                    # noqa: E402  COMPACT_COLUMNS 단일 출처
import resolve_broker as RB                              # noqa: E402  증권사 정규명 어휘(단일 출처)

# ── 어휘 (전부 프롬프트에 이미 적혀 있던 것을 코드로 옮긴 것 — 화면별 하드코딩 아님) ──────
H_NAME = ("종목명", "상품명", "종목")
H_VALUE = ("평가금액", "평가액", "잔고", "평가")
H_COST = ("매수금액", "매입금액", "매입원금", "취득금액")
H_PNL = ("평가손익", "손익", "평가손실")
H_RATE = ("수익률", "손익률", "등락률")
H_QTY = ("수량", "보유수량", "잔고수량")
# 매수 단가 열 — 현재가와 **다른 의미**다(주당 취득가). mPOP 종합잔고의 수량 모드는
# '매수단가/현재가'를 한 밴드에 쌓는데, 둘 다 price로 매핑하면 덮어쓰기 순서 운에 걸리고
# 매수 정보가 유실된다. 수량이 있으면 화면 자신의 산식이 성립한다:
# cost = 수량×매수단가, value = 수량×현재가(평가금액 열이 없는 모드의 표시 값과 원단위 일치).
H_COST_UNIT = ("매수단가", "평균단가", "매입단가", "평단")
H_PRICE = ("현재가", "단가")
# 분류 열 — **금액이 아니라 범주**가 들어가는 칸(값이 비거나 '매도가능' 같은 글자다).
# 열로 잡으면 행 앵커가 망가진다: `잔고구분`이 `잔고`(H_VALUE)에 걸려 value 열이 되면
# 밴드 깊이가 2가 되고, 한 행에 숫자가 하나뿐이라 **다음 행의 수량이 이 행의 평가금액으로**
# 들어간다(실측 2026-08-06 mPOP 퇴직연금 화면: qty=3,146,613 / value=30).
H_CATEGORY = ("구분", "유형", "상태")
HEADER_VOCAB = H_NAME + H_VALUE + H_COST + H_PNL + H_RATE + H_QTY + H_COST_UNIT + H_PRICE

# 시장지수 표시줄 — 보유종목이 아니다(프롬프트 규칙 2).
INDEX_TOKENS = ("코스피", "KOSPI", "코스닥", "KOSDAQ", "애프터마켓", "장마감", "장중", "다우",
                "나스닥종합", "S&P500지수")
# assetClass 키워드 표 = 프롬프트 규칙 (6) 그대로.
ASSET_RULES = ((("나스닥", "S&P", "성장", "테크", "AI", "반도체", "휴머노이드", "소프트웨어"), "성장주"),
               (("고배당", "배당"), "배당주"),
               (("채권혼합",), "혼합(주식·채권)"),
               (("채권",), "채권"),
               (("금현물", "원자재", "금선물"), "금·원자재"),
               (("리츠", "부동산"), "부동산"),
               (("예수금", "현금", "CMA", "예금", "증거금"), "현금"))
# accountType 어휘는 **finalize._ATYPE 하나만** 쓴다. 여기 사본을 두었더니 순서가 갈려
# '퇴직연금(다이렉트IRP)'을 퇴직연금으로 냈다(GT=IRP) — finalize는 IRP를 먼저 본다.

NUM_RE = re.compile(r"^[+\-]?[\d,]+(?:\.\d+)?$")
PCT_RE = re.compile(r"[+\-]?[\d,.]+\s*%")
# 소수 수량 허용 — 소수점 매매 앱(토스 '0.056979주', 카카오페이 '0.08주')은 수량이 정수가
# 아니다. 정수 전용이면 그 텍스트가 이름에 붙고, `\d\.\d{2}` 규칙('자기 값을 단 라벨')에
# 걸려 **행이 통째로 사라진다**(실측 08-10: 카카오페이 4행 전수 누락).
QTY_RE = re.compile(r"^([\d,]+(?:\.\d+)?)\s*주$")
# 외화 예수금 — 프롬프트 규칙 (8): qty=외화 금액, value=원화 환산액.
# '6,923.28 USD'는 숫자도 'N주'도 아니라 그냥 무시됐다(측정 160229: qty 유실).
FX_QTY_RE = re.compile(r"^([\d,]+(?:\.\d+)?)\s*(USD|JPY|EUR|CNY|HKD)$")


def _cy(b):
    return b["y"] + b["h"] / 2.0


def _right(b):
    return b["x"] + b["w"]


def _clean_num(s):
    """'+1,234원' · '-72,150' → float. 쉼표·통화기호·단위·선행 + 제거(프롬프트의 숫자 표기 규칙)."""
    a = _amount_of(s)
    if a is None:
        a = re.sub(r"[원$,\s주]", "", str(s)).rstrip("|")
    # 마침표 뒤 **정확히 세 자리로 끝나는** 자리수구분 숫자는 쉼표 오독이다 — 자리수 구분은
    # 세 자리씩이고 소수부가 세 자리인 금액은 이 화면들에 없다(원화는 정수, 달러는 두 자리).
    # 실측 2026-07-29: 화면 `+3,662,786원`을 OCR이 `+3,662.786원`(conf 0.98)으로 읽어 손익이
    # **1000분의 1**이 됐고, cost가 파생값이라 회계 항등식은 그대로 성립해 아무도 못 잡았다.
    # `IVV`→`IWV`와 같은 글리프 혼동이지만 이쪽은 **금액의 크기**를 바꾼다.
    if re.search(r"[.,]\d{3}\.\d{3}$", a):
        a = a[::-1].replace(".", ",", 1)[::-1]
    a = a.replace(",", "")
    if not a or a in ("-", "+", "."):
        return None
    try:
        return float(a)
    except ValueError:
        return None


def _amount_of(t):
    """박스 텍스트 → 금액 문자열(숫자부) 또는 None.

    화면은 금액과 수익률을 **한 박스에** 담는다 — '-2,054,000원 |-13.54%', '0원(0%)',
    '-212,500원(-17.2%)'. '%'가 있다고 버리면 손익이 통째로 사라지고(→ cost 계산 불가)
    회계 항등식이 깨진다. 구분자에서 잘라 앞의 금액만 취한다.

    **수익률-우선 표기**('+33.28%(74,920원)' — 카카오페이·토스)는 금액이 괄호 안이다.
    앞 조각이 %면 버리지 말고 뒤 조각에서 금액을 찾는다(부호는 앞 조각 것 — 괄호 안은
    부호가 벗겨져 있다: '-16.71%(2,737원)'의 손익은 -2,737)."""
    s = re.sub(r"[원\s]", "", str(t))
    segs = [x.rstrip(")|") for x in re.split(r"[|(]", s)]
    if segs and "%" not in segs[0] and NUM_RE.match(segs[0]):
        return segs[0]
    if segs and "%" in segs[0]:
        sign = "-" if segs[0].startswith("-") else ""
        for seg in segs[1:]:
            if seg and "%" not in seg and NUM_RE.match(seg):
                return sign + seg.lstrip("+-")
    return None


def _is_amount(t):
    return _amount_of(t) is not None


def is_index_line(texts):
    joined = "".join(texts)
    return any(k in joined for k in INDEX_TOKENS)


def asset_class(name):
    n = str(name or "")
    for keys, cls in ASSET_RULES:
        if any(k.lower() in n.lower() for k in keys):
            return cls
    return None


def account_type(text):
    """finalize의 단일 어휘를 재사용. 근거 없는 화면은 None으로 남긴다(finalize가 상속·기본값
    '일반'을 정한다) — 여기서 '일반'을 박으면 상속을 막는다."""
    t = str(text or "")
    if not any(k in t for k, _ in F._ATYPE):
        return None
    return F.norm_atype(t)


# ── 1) 크롬 제거 ──────────────────────────────────────────────────────────────
def metrics(boxes):
    """화면이 스스로 내는 단위 — **절대 픽셀 대신 이걸 쓴다.**

    기기마다 해상도·폭·글꼴 크기가 다르고, 스크롤 스티칭 화면은 세로로 무한히 길어진다.
    기기별 분기로 대응하면 기기 목록을 영원히 쫓아야 한다. 대신 두 값을 화면 자신에게서 뽑는다:

      h    글자 높이(중앙값) — 해상도·글꼴 크기와 **함께 움직이는 유일한 세로 단위**.
           이미지 높이는 못 쓴다: 스티칭되면 종목 수에 따라 늘어나 같은 배율이 아니다.
      x0·x1·span  내용의 가로 범위 — 이미지 폭이 아니라 **글자가 실제로 놓인 범위**라
           여백·기기 폭과 무관하고, 호출부가 이미지 크기를 알 필요도 없다.

    실측 근거(§4.10): 이 단위를 쓰기 전 bind.py에는 절대 픽셀 90·120·70과 `width=1080`
    기본값이 있었고, 균일 확대 1.333배(=1440폭 기기)에서 재현율 31/31 → 27/31로 무너졌다.
    세 상수는 픽스처 8장에서 각각 1.7·2.2·1.3 × 글자높이로 **일정했다** — 애초에 글자 높이를
    절대 픽셀로 적어둔 것이었다.
    """
    hs = sorted(b["h"] for b in boxes)
    x0 = min(b["x"] for b in boxes)
    x1 = max(b["x"] + b["w"] for b in boxes)
    return {"h": hs[len(hs) // 2] or 1, "x0": x0, "x1": x1, "span": max(1, x1 - x0)}


def _large_mode(vals, min_ratio=1.8, min_frac=0.25):
    """정렬된 값들이 **이봉**이면 큰 쪽 무리만 돌려준다. 아니면 전부.

    쓰는 곳: 금액들의 세로 간격에는 '한 행 안의 평가금액↔손익 간격'과 '행 사이 간격'이
    섞여 있다. 둘을 갈라야 행 피치를 얻는데, 예전엔 `> 글자높이 × 2`로 갈랐다. 그건 두
    간격의 비가 레이아웃마다 다르다는 걸 무시한 것이라, 촘촘한 화면(피치/글자높이 = 1.50)
    에서 **행 간격을 1.5배만 늘려도 11행이 1행으로 무너졌다**(§4.10).

    경계를 **비율이 뛰는 자리**에서 찾으면 상수가 무차원이 된다 — 해상도·폭·행 간격 어느
    것이 바뀌어도 같은 자리를 찾는다. `min_ratio`는 '무리가 갈리는가'만 판정하므로 거리
    단위가 없다.

    **가장 큰 도약이 아니라 첫 도약을 쓴다.** 무리는 둘이 아닐 수 있다: 계좌 구분자가 있는
    화면은 `행 안 | 행 사이 | 블록 사이`로 셋이고, 가장 큰 도약을 고르면 블록 경계를 집어
    **한 블록이 통째로 한 행이 된다**(측정 §4.10: 헤더 화면 복제 ×8에서 72행 대신 8행).
    작은 쪽에서 첫 도약을 고르면 '행 안'만 떨어져 나가고 나머지는 전부 행 경계로 남는다.

    또 하나: **큰 무리가 극소수면 무리가 아니라 이상치다.** 표본의 일부(min_frac)를 넘지
    못하는 꼬리에서 갈랐다가는 그 몇 개가 행 피치를 통째로 정한다(측정: 간격 33개 중 큰
    값 3개(9%)에서 갈려 피치가 113 → 968로 뛰고 화면이 0행이 됐다). 이것도 비율 조건이라
    거리 단위가 없다."""
    if len(vals) < 2:
        return vals
    for i in range(1, len(vals)):
        if vals[i] / max(vals[i - 1], 1e-9) >= min_ratio and (len(vals) - i) >= len(vals) * min_frac:
            return vals[i:]
    return vals


def strip_chrome(boxes, m):
    """상태바(시계+배터리) · 내비바 · 시장지수 줄을 뺀다. 위치 비율이 아니라 **내용**으로 판정한다
    (스크롤 스티칭된 4928px 화면에서 비율 규칙은 무너진다).

    상단 경계도 이미지 높이 비율이 아니라 **글자 높이**로 잡는다 — 비율로 잡으면 긴 화면일수록
    경계가 아래로 내려가 본문을 먹는다(4928px면 148px, 2340px면 70px로 제각각이다)."""
    top = min(b["y"] for b in boxes) + m["h"] * 2.5 if boxes else 0
    out = []
    for b in boxes:
        t = b["text"].strip()
        # 상태바: 화면 최상단 줄의 시계/배터리/통신 아이콘 텍스트
        if b["y"] < top and (re.match(r"^\d{1,2}[:.]\d{2}", t) or "%" in t
                             or len(t) <= 6):
            continue
        out.append(b)
    return out


# ── 2) 줄 그룹핑 ──────────────────────────────────────────────────────────────
def group_lines(boxes, tol=0.6):
    """y 중심이 줄 대표값과 (글자높이 × tol) 안이면 같은 줄. 표의 한 행은 보통 2줄로 이뤄진다.

    **직전 박스와 비교하지 않는다.** 예전엔 `abs(_cy(b) - _cy(cur[-1])) <= med_h*tol`로 이어
    붙였는데, 그러면 한 칸씩 조금씩 밀리며 **서로 다른 줄이 한 줄로 이어진다**(chaining drift).
    실측(§4.11): 픽스처 8장 중 2장에서 한 '줄'의 세로 폭이 **112px(글자높이 53의 2.1배)** 까지
    벌어졌다. 대표값(누적 평균) 기준으로 바꾸면 전 화면에서 30px 이하로 잡힌다.

    같은 결함을 `ocr.merge_lines`와 ML Kit 시뮬레이터에서도 한 번씩 만났다 — 그래서 줄
    묶기는 여기 하나만 두고 나머지가 가져다 쓴다(단일 출처)."""
    if not boxes:
        return []
    med_h = sorted(b["h"] for b in boxes)[len(boxes) // 2] or 1
    rows = []
    for b in sorted(boxes, key=_cy):
        for r in rows:
            if abs(_cy(b) - r["cy"]) <= med_h * tol:
                r["items"].append(b)
                r["cy"] = sum(_cy(x) for x in r["items"]) / len(r["items"])
                break
        else:
            rows.append({"cy": _cy(b), "items": [b]})
    return [sorted(r["items"], key=lambda x: x["x"]) for r in rows]


# ── 3) 헤더 → 열 정의 ────────────────────────────────────────────────────────
_HDR_EXACT = tuple(v.replace(" ", "") for v in HEADER_VOCAB)


def _hdr_hit(text):
    """이 박스가 **컬럼 헤더인가** — 괄호·공백을 벗긴 뒤 헤더 어휘와 **완전일치**해야 한다.

    부분일치는 못 쓴다: 정렬·표시 토글 줄('평가금액 순'·'평가금'·'현재가' 버튼 —
    카카오페이·토스)이 '평가'·'현재가'에 걸려 가짜 헤더가 되고, 그러면 유일한 금액 열이
    맨오른쪽 밴드('현재가')에 붙어 **평가금액이 전부 price 칸으로** 가고 value가 비어
    finalize가 전 행을 버린다(실측 08-10: 카카오페이 5행 → 1행, 토스 1행 → 0행).
    진짜 헤더는 열 이름 그 자체다('종목명'·'평가금액'·'수익률(%)' — 괄호 주석까지만)."""
    bare = re.sub(r"\(.*?\)", "", str(text)).replace(" ", "").strip()
    return bare in _HDR_EXACT


def find_header(lines):
    """헤더 어휘와 완전일치하는 박스를 2개 이상 담은 첫 줄 묶음. 헤더 자체가 세로로 쌓여
    있어(평가금액 위 / 매수금액 아래) 인접 2줄까지 합쳐 본다. 반환: 헤더 박스 리스트 또는 None."""
    # 하단 내비바가 헤더로 오인된다 — '잔고'(H_VALUE)·'현재가'(H_PRICE)가 메뉴 이름이라
    # 어휘만 보면 2히트가 난다(측정 160139: ['메뉴','HOME','국내','잔고','현재가',…]).
    # 구조로 배제한다: **진짜 헤더는 아래에 금액이 있다.** 내비바 밑에는 아무것도 없다.
    below_amounts = [0] * len(lines)
    n_amt = 0
    for i in range(len(lines) - 1, -1, -1):
        below_amounts[i] = n_amt
        n_amt += sum(1 for b in lines[i] if _is_amount(b["text"]))
    for i, ln in enumerate(lines):
        hits = [b for b in ln if _hdr_hit(b["text"])]
        if len(hits) >= 2 and below_amounts[i] >= 2:
            merged = list(ln)
            if i + 1 < len(lines):                     # 스택된 헤더 2번째 줄 흡수
                nxt = lines[i + 1]
                if any(_hdr_hit(b["text"]) for b in nxt) and \
                        not any(_is_amount(b["text"]) for b in nxt):
                    merged += nxt
            return merged
    return None


def _kind_of(text):
    """헤더 텍스트 → 의미 열. 긴 어휘부터 봐야 '평가손익'이 '평가금액'에 먹히지 않는다."""
    t = str(text)
    if any(k in t for k in H_CATEGORY):      # 분류 열은 어떤 의미 열도 아니다 → 밴드를 만들지 않는다
        return None
    for keys, kind in ((H_PNL, "pnl"), (H_RATE, "rate"), (H_COST, "cost"), (H_VALUE, "value"),
                       (H_QTY, "qty"), (H_COST_UNIT, "cost_unit"), (H_PRICE, "price"),
                       (H_NAME, "name")):
        if any(k in t for k in keys):
            return kind
    return None


def columns_from_header(header, m):
    """헤더 박스 → [{kinds:[의미,...], x0, x1}] — kinds는 **밴드 내 y 순서**(=데이터 스택 순서).
    금액 열은 우측 정렬이라 오른쪽 끝(right)으로 밴드를 만든다."""
    marks = []
    for b in header:
        k = _kind_of(b["text"])
        if k:
            marks.append({"kind": k, "y": _cy(b), "x0": b["x"], "x1": _right(b),
                          "anchor": _right(b)})
    marks.sort(key=lambda mk: mk["anchor"])
    bands = []
    for mk in marks:
        if bands and abs(mk["anchor"] - bands[-1]["anchor"]) < m["h"] * 1.7:  # 같은 열의 스택된 헤더
            bands[-1]["marks"].append(mk)
            bands[-1]["x0"] = min(bands[-1]["x0"], mk["x0"])
            bands[-1]["x1"] = max(bands[-1]["x1"], mk["x1"])
        else:
            bands.append({"marks": [mk], "x0": mk["x0"], "x1": mk["x1"],
                          "anchor": mk["anchor"]})
    for b in bands:
        b["marks"].sort(key=lambda mk: mk["y"])          # 위→아래 = 데이터 스택 순서
        b["kinds"] = [mk["kind"] for mk in b["marks"]]
    return bands


# ── 4~5) 행 조립 ─────────────────────────────────────────────────────────────
def _assign_band(cells, kinds):
    """한 열 밴드에 떨어진 셀들(y순) → {의미: 값}. 헤더 스택 순서대로 배정한다."""
    out = {}
    cells = sorted(cells, key=_cy)
    # %가 붙은 값은 rate로 확정(순서 오배정 방지 — 수익률은 유일하게 단위가 다르다)
    rate_cells = [c for c in cells if "%" in c["text"]]
    plain = [c for c in cells if "%" not in c["text"]]
    ks = [k for k in kinds if k != "rate"]
    for k, c in zip(ks, plain):
        out[k] = c["text"]
    if rate_cells and "rate" in kinds:
        out["rate"] = rate_cells[0]["text"]
    return out


def _nearest_band(b, anchors):
    """박스의 오른쪽 끝이 가장 가까운 열 밴드 — **허용치 상수 없이** 열을 정한다.

    예전엔 `abs(right - anchor) < 2.2 × 글자높이`였는데, 이 편차는 글자 크기가 아니라
    **레이아웃 거리**다: 데이터 박스가 헤더 박스보다 넓어 오른쪽 끝이 60~65px 어긋난다
    (밴드 간격 348~381px의 0.18배). 화면이 넓어지면 이 어긋남도 같이 커지는데 허용치는
    글자높이에 묶여 있어 폭 2배에서 **모든 금액이 탈락하고 헤더 화면이 0행이 됐다**(§4.10).
    최근접 분할은 배율에 불변이고 상수가 없다 — `rows_from_list`가 이미 쓰는 방식이다."""
    r = _right(b)
    return min(range(len(anchors)), key=lambda i: abs(r - anchors[i]))


def rows_from_headered(lines, bands, name_x1, m):
    """헤더가 있는 표: 금액 앵커 열(가장 오른쪽 밴드)의 셀 묶음이 행을 정의한다."""
    anchor = bands[-1]
    anchors = [bd["anchor"] for bd in bands]
    ai = len(bands) - 1
    depth = max(1, len(anchor["kinds"]))
    amounts = [b for ln in lines for b in ln
               if _is_amount(b["text"]) and _nearest_band(b, anchors) == ai]
    amounts.sort(key=_cy)
    # 행 경계는 **간격이 벌어지는 곳**이지 개수가 아니다. 예전엔 `depth`개씩 잘랐는데, 그러면
    # 한 행에서 금액이 하나만 더 잡히거나 빠져도 **그 뒤 모든 행이 통째로 밀린다**(측정 §4.10:
    # 행 간격을 10%만 좁혀도 재현율은 31/31인데 value 22/31 — 값이 이웃 행에서 온다).
    # 앵커 열 금액의 세로 간격은 '행 안(평가금액↔손익)'과 '행 사이'로 이봉이므로, 큰 쪽 무리의
    # 최솟값을 경계로 쓰면 개수 가정 없이 행이 갈린다. 이봉이 아니면 옛 방식으로 물러난다.
    a_gaps = sorted(_cy(b) - _cy(a) for a, b in zip(amounts, amounts[1:]))
    big = _large_mode(a_gaps)
    thr = big[0] if len(big) < len(a_gaps) else None
    if thr is None:
        groups = [amounts[i:i + depth] for i in range(0, len(amounts), depth)]
    else:
        groups, cur = [], [amounts[0]] if amounts else []
        for prev, b in zip(amounts, amounts[1:]):
            if _cy(b) - _cy(prev) >= thr:
                groups.append(cur)
                cur = [b]
            else:
                cur.append(b)
        if cur:
            groups.append(cur)
    # 행에 딸린 조각(종목명·수량)을 걷을 세로 창. 이름이 두 금액 사이에 중앙정렬될 수 있어
    # 행의 금액 범위보다 넓어야 하지만, **이웃 행을 넘보면 안 된다.** 글자높이로 잡으면
    # (예전 `1.3 × h`) 행이 촘촘한 레이아웃에서 옆 행 조각을 빨아들여 수량이 유실된다
    # (측정 §4.11: 행 간격 0.85배에서 qty 29/31 — 가장 촘촘한 화면 두 행). 행 경계 간격의
    # **절반**으로 잡으면 정의상 이웃 행에 닿을 수 없고, 레이아웃 밀도를 따라 같이 움직인다.
    win = thr * 0.5 if thr else m["h"] * 1.3
    rows = []
    for g in groups:
        if not g:
            continue
        y0, y1 = _cy(g[0]), _cy(g[-1])
        lo, hi = y0 - win, y1 + win
        row = {}
        for bi, band in enumerate(bands):
            if band["kinds"] == ["name"]:
                continue
            # 앵커(우측 정렬 끝)로 판정한다 — 데이터 박스는 헤더 박스보다 넓어서 헤더의
            # x1로 자르면 몇 px 차이로 전부 탈락한다(측정: 헤더 right=1002, 데이터 right=1064).
            cells = [b for ln in lines for b in ln
                     if lo <= _cy(b) <= hi and _nearest_band(b, anchors) == bi
                     and (_is_amount(b["text"]) or "%" in b["text"])]
            if cells:
                row.update(_assign_band(cells, band["kinds"]))
        # 이름 열은 숫자를 포함한다('KODEX 미국S&P500'이 '500'으로 줄바꿈된다) → 숫자라고
        # 빼면 이름이 잘린다. 대신 'N주'만 수량으로 걷어낸다(프롬프트: qty엔 수량만).
        frags, qty_txt = [], None
        # 조각은 **읽는 순서(위→아래, 왼→오른)로** 이어야 한다. 줄 그룹핑이 두 시각적 줄을
        # 하나로 묶을 수 있어(같은 x, 가까운 y) 전역 (y, x) 정렬로 순서를 확정한다 —
        # 안 하면 'TIME미국나스닥1' + '00채권혼합50액티' + '브'가 '…1브00…'으로 섞인다.
        cand = [b for ln in lines for b in ln
                if lo <= _cy(b) <= hi and _right(b) <= name_x1]
        for b in sorted(cand, key=lambda b: (_cy(b), b["x"])):
            t = b["text"].strip()
            if QTY_RE.match(t):
                qty_txt = t
            elif "%" not in t:
                frags.append(t)
        if qty_txt:
            row["qty"] = qty_txt
        row["name"] = re.sub(r"\s+", " ", "".join(frags)).strip()
        if row.get("name") or row.get("value"):
            rows.append(row)
    return rows


def rows_from_list(lines, m):
    """헤더 없는 목록형(상품별 총액·계좌 잔고·예수금 상세): 왼쪽 라벨 + 오른쪽 금액.

    라벨이 자기 금액과 **같은 줄에 없다** — 금액 두 개(평가금액·손익) 사이에 세로 중앙정렬되기
    때문이다(측정 160139: 금액 cy=1146/1220, 라벨 '국내주식' cy=1180). 그래서 줄 단위로 짝짓지
    않고 **라벨이 행을 정의하고 금액은 cy가 가장 가까운 라벨에 붙는다**. 한 라벨에 붙은 금액을
    y로 정렬하면 그게 곧 프롬프트의 **위=평가금액, 아래=평가손익** 규칙이다.
    """
    boxes = [b for ln in lines for b in ln]
    # 'N주'는 라벨이 아니라 **수량**이다. 라벨로 두면 자기 금액과 cy가 거의 같아(측정 160155:
    # '6주' cy=1280 vs 금액 cy=1279.5) 진짜 종목명('SK하이닉스' cy=1200)을 이기고 name을 뺏는다.
    qtys = [b for b in boxes if QTY_RE.match(b["text"].strip())]
    # 자기 값을 단 라벨('환율 1,482.00')과 어포던스('접기›', '…보기>')는 행 이름이 될 수 없다.
    # 최종 필터(_is_field_label)에만 두면 **짝짓기에서 남의 금액을 훔친 뒤** 버려져 그 금액까지
    # 유실된다(실측 G4 외화예수금 탭: '환율 1,482.00'이 환산액을 가져가 '미국 달러' 행이 통째로
    # 사라졌다. 같은 폼이 G1에선 '환율1,466.20'이 두 번 나와 크롬 필터에 걸린 덕에 — 운으로 — 살았다).
    labels = [b for b in boxes
              if not _is_amount(b["text"]) and "%" not in b["text"]
              and b["x"] < m["x0"] + m["span"] * 0.5
              and not QTY_RE.match(b["text"].strip())
              and not re.search(r"\d,\d{3}|\d\.\d{2}", b["text"])
              and not re.search(r"[›»⌄˅>]\s*$", b["text"].strip())
              # 접기/펼치기류는 어포던스 기호가 OCR에서 떨어져 나가면 맨글자로 남는다 —
              # 라벨이 되면 예수금 화면의 시점 행 값을 훔쳐 '접기'라는 자산이 생긴다(실측 08-10).
              and b["text"].strip() not in ("접기", "펼치기", "더보기", "더 보기")]
    # 한 시각적 줄의 라벨은 하나다 — 종목 로고의 글자 조각('S&P'·'500' 원형 배지, 토스)이
    # 이름 왼쪽에 따로 박스로 잡히면 최근접 분할에서 수량·손익을 훔친다(실측 08-10: SPY의
    # 수량이 'S&P'에 붙어 유실). 같은 줄에서는 **가장 넓은 박스**(그 줄의 지배적 텍스트)만
    # 라벨로 남긴다. 줄 묶음은 group_lines 결과를 그대로 쓴다(단일 출처).
    keep = set()
    for ln in lines:
        cand = [b for b in ln if any(b is l for l in labels)]
        if cand:
            keep.add(id(max(cand, key=lambda b: b["w"])))
    labels = [b for b in labels if id(b) in keep]
    # 한 화면에서 **똑같은 문구가 반복되면 UI 크롬**이다(계좌마다 붙는 '이체'·'거래내역'·'주식주문').
    # 행 이름은 화면에서 유일하다('한 자산 = 한 행' 불변식). 안 걸러내면 이 크롬이 최근접 분할에서
    # 금액을 훔쳐 가짜 행을 만들고, 화면 유형 판정까지 뒤집는다(측정: 160333이
    # account_summary → detail 로 오분류).
    seen = {}
    for b in labels:
        seen[b["text"].strip()] = seen.get(b["text"].strip(), 0) + 1
    amounts = [b for b in boxes
               if _is_amount(b["text"]) and _right(b) > m["x0"] + m["span"] * 0.45]
    if not labels or not amounts:
        return []
    # **라벨이 행을 정의하고, 각 금액은 cy가 가장 가까운 라벨에 붙는다** — 거리 상한 없음.
    # 상한을 두려던 시도는 전부 실패했다: 라벨-금액 간격은 레이아웃마다 다르고(34px vs 98px)
    # 방향도 다르고(금액이 위/아래), 같은 행 안의 value-pnl 간격이 행 간격보다 작아서
    # '행 피치'로 정규화하면 pnl이 잡히는 순간 상한이 스스로 붕괴한다. 라벨은 행마다 하나이므로
    # 최근접 분할(Voronoi)이 상수 없이 세 레이아웃을 모두 만족한다.
    # 라벨을 잘못 얻은 행은 값이 0이거나 총액이라 finalize의 게이트가 걸러낸다.
    # 허용 거리는 **행 간 피치**로 정한다. 연속 금액 간격에는 같은 행 안의 value↔pnl 간격(~70px)과
    # 행 사이 간격(~430px)이 섞여 있어, 전체 중앙값을 쓰면 pnl을 잡는 순간 상한이 스스로 붕괴한다
    # (측정: 160333 상한 225→70으로 무너져 전 행 탈락). 글리프 높이의 2배를 넘는 간격만 = 행 간.
    med_h = m["h"]
    acy = sorted(_cy(a) for a in amounts)
    gaps = sorted(b - a for a, b in zip(acy, acy[1:]))
    inter = _large_mode(gaps)
    pitch = inter[len(inter) // 2] if inter else med_h * 4
    limit = max(med_h * 1.6, pitch * 0.60)

    # ── 계좌 구분자 분리 ─────────────────────────────────────────────────────
    # '전체계좌' 화면은 종목마다 **그 위에** 계좌 라벨을 다시 찍는다(측정
    # 20260731T085311Z img1: '1234-5678-01(Super365)'가 380px 간격으로 10회, 각 라벨 바로
    # 아래에 종목명·수량·평가금액·손익 한 블록). 이건 행 이름이 아니라 **행 그룹의 머리**이고,
    # 그 행의 broker 근거다. 안 갈라내면 값은 다 맞는데 broker가 통째로 None이 된다(실측 15/15).
    #
    # **반복 횟수로 판별하면 안 된다.** 처음엔 `반복 2회 이상 + 계좌토큰`으로 잡았는데,
    # 종목이 **하나뿐인 계좌**는 구분자가 1회만 나와 걸리지 않는다 → 그 라벨이 종목명 행으로
    # 둔갑해 아래 행의 손익을 훔친다(측정 9:1 합성: 가짜 행 `<계좌번호>X(Super365)`
    # value=-1,633,630 생성, 행 수 10→11, 다음 종목의 계좌도 오배정).
    #
    # **'아래 한 행 안에 종목명이 있으면 구분자'도 안 된다.** 계좌요약 화면도 계좌 라벨 아래
    # 어딘가에 UI 라벨이 있어서 같이 걸린다 → 계좌요약이 통째로 무너진다
    # (측정 160333: 3행 → 2행, 이름이 '상품구성'·'계좌현황'으로 바뀌고 broker 27/31로 FAIL).
    #
    # 두 폼의 **실측 기하**가 답을 준다 — 계좌 라벨에서 아래로 내려갈 때 무엇이 먼저 오는가:
    #   계좌요약(160333)  y=579 라벨 → y=665 **금액 '5원'**      (자기 값. 사이에 이름 없음)
    #   전체계좌(img1)    y=1128 라벨 → y=1192 **이름 'AIPO'** → y=1304 금액
    # 즉 **계좌 라벨과 그 아래 첫 금액 사이에 종목명이 끼면 구분자**, 금액이 먼저 오면
    # 그 라벨 자신이 행이다. 상수 없이 두 폼을 가른다.
    def _is_sep(b):
        if not F.acct_tokens(b["text"]):
            return False
        cy = _cy(b)
        nxt_amt = min([_cy(a) for a in amounts if _cy(a) > cy], default=None)
        if nxt_amt is None:
            return False
        return any(cy < _cy(l) < nxt_amt and not F.acct_tokens(l["text"]) for l in labels)

    seps = [b for b in labels if _is_sep(b)]
    sep_ids = {id(b) for b in seps}
    # 남은 라벨에서 **반복되는 문구는 UI 크롬**이다(계좌마다 붙는 '이체'·'거래내역'·'주식주문').
    # 행 이름은 화면에서 유일하다('한 자산 = 한 행'). 안 걸러내면 크롬이 최근접 분할에서 금액을
    # 훔쳐 가짜 행을 만들고 화면 유형 판정까지 뒤집는다(측정: 160333이 detail로 오분류).
    labels = [b for b in labels
              if id(b) not in sep_ids and seen[b["text"].strip()] == 1]
    if not labels:
        return []

    def _nearest_label(b):
        near = min(labels, key=lambda l: abs(_cy(l) - _cy(b)))
        return near if abs(_cy(near) - _cy(b)) <= limit else None

    def _nearest_label_left(b):
        """수량·외화 조각의 짝 — 이 조각들은 이름의 **아랫줄, 같은 왼쪽 정렬**에 온다
        ('존슨앤드존슨' 밑 '0.08주'가 x까지 같다). cy만 보면 세로로 더 가까운 다른 라벨
        (로고 조각·이웃 행)이 훔친다 → 왼쪽 끝이 정렬된(글자높이 2배 이내) 라벨을 먼저 찾고,
        없으면 cy 최근접으로 물러난다. 금액 짝짓기는 cy만 쓴다(금액은 오른쪽 정렬이라 x가
        원래 다르다)."""
        aligned = [l for l in labels if abs(l["x"] - b["x"]) <= med_h * 2]
        pool = aligned or labels
        near = min(pool, key=lambda l: abs(_cy(l) - _cy(b)))
        return near if abs(_cy(near) - _cy(b)) <= limit else None

    buckets = {}
    for a in amounts:
        near = _nearest_label(a)
        if near is not None:
            buckets.setdefault(id(near), (near, []))[1].append(a)
    qty_by_label = {}
    for q in qtys:
        nq = _nearest_label_left(q)
        if nq is not None:
            qty_by_label.setdefault(id(nq), q)
    fx_by_label = {}
    for b in boxes:                                     # 외화 잔액('6,923.28 USD') → qty + 통화
        m = FX_QTY_RE.match(b["text"].strip())
        if not m or float(m.group(1).replace(",", "")) == 0:   # 잔액 0 행은 제외(규칙 8)
            continue
        nb = _nearest_label_left(b)
        if nb is not None:
            fx_by_label.setdefault(id(nb), m)
    # 외화 행의 원화 환산액은 라벨의 **아랫줄**(환율 줄)에 온다 — 최근접 분할이 그 금액을
    # 못 붙였으면(카드 금액이 더 가깝거나 거리 상한 밖) 아직 주인 없는 금액 중 라벨 바로
    # 아래 것을 붙인다. 규칙 (8): qty=외화 금액, value=원화 환산액 — value 없이는 행이
    # finalize에서 버려져 외화 예수금이 통째로 누락된다(실측 G4: 미국달러 6,928.52 유실).
    taken = {id(a) for _, amts in buckets.values() for a in amts}
    for key, mfx in fx_by_label.items():
        if key in buckets:
            continue
        lab = next((l for l in labels if id(l) == key), None)
        if lab is None:
            continue
        free = [a for a in amounts if id(a) not in taken
                and 0 < _cy(a) - _cy(lab) <= limit * 2]
        if free:
            a = min(free, key=lambda x: _cy(x) - _cy(lab))
            buckets[key] = (lab, [a])
            taken.add(id(a))
    def _sep_above(cy):
        """이 행을 덮는 계좌 구분자 = **바로 위**의 구분자. 화면이 계좌별로 쪼개져 있으면
        행마다 다른 계좌가 잡힌다(사용자 지적: '각 계좌로 나눌 수 있으면 되는 거야').
        위에 아무것도 없으면 None — 지어내지 않는다."""
        above = [s for s in seps if _cy(s) <= cy]
        return max(above, key=_cy)["text"].strip() if above else None

    rows = []
    for key, (near, amts) in sorted(buckets.items(), key=lambda kv: _cy(kv[1][0])):
        amts.sort(key=_cy)
        # **부호 규칙** — 평가금액·잔고는 부호가 없다. '+13,339원'·'+60원(0.1%)' 같은 부호
        # 금액은 변화량(손익)이지 값이 아니다. 부호 금액이 value 자리에 앉으면 상단 지표
        # 카드('총 수익'·'일간 수익')가 어휘 없이는 보유행으로 둔갑한다(실측 08-10 토스).
        # value = 첫 무부호 금액(위=평가금액 규칙 유지), pnl = 첫 부호 금액(없으면 종전대로
        # 두 번째 무부호). 무부호가 없으면 행이 아니다 — 값 없는 행은 finalize가 버린다.
        unsigned = [a for a in amts if not re.match(r"^[+\-]", a["text"].strip())]
        signed = [a for a in amts if re.match(r"^[+\-]", a["text"].strip())]
        if not unsigned:
            continue
        row = {"name": near["text"].strip(), "value": unsigned[0]["text"]}
        sep = _sep_above(_cy(near))
        if sep:
            row["broker"] = sep          # 정규화는 resolve_broker가 한다(여기서 판정 안 함)
        if signed:
            row["pnl"] = signed[0]["text"]
        elif len(unsigned) > 1:
            row["pnl"] = unsigned[1]["text"]
        if key in qty_by_label:
            row["qty"] = qty_by_label[key]["text"]
        if key in fx_by_label:
            m = fx_by_label[key]
            row["qty"], row["currency"] = m.group(1), m.group(2)
        rows.append(row)
    return rows


FIELD_LABEL_EXTRA = ("출금가능", "총자산", "총 자산", "신용", "대출", "예상금액")
# 상단 지표 카드의 라벨 — 종목이 아니라 화면 요약 수치의 이름이다(토스 '원금·총 수익·일간 수익',
# 카카오페이 '총 투자금'). 부분일치는 위험해서('원금'이 '원금보장 채권'을 먹는다) **완전일치만**.
FIELD_LABEL_EXACT = ("원금", "총수익", "총 수익", "일간수익", "일간 수익", "총투자금",
                     "총 투자금", "내투자", "내 투자", "투자금")


def _is_field_label(name):
    """행 이름이 **필드 라벨**인가('평가금액'·'평가손익'·'출금가능금액'). 이건 종목이 아니라
    화면 합계의 이름이라 보유행이 되면 안 된다(프롬프트 규칙 2의 '합계·헤더 행 제외').
    단 카테고리('원화예수금'·'국내주식')는 요약화면의 **정당한 행**이므로 보호한다."""
    n = (name or "").strip()
    if F.is_category(n):
        return False
    # **부분일치는 못 쓴다** — H_VALUE의 '잔고'가 정당한 종목명 '현금잔고(예수금)'에 걸려
    # 그 행을 통째로 지웠다(그 결과 파리티에서 다른 현금행과 오매칭돼 값·재현율이 연쇄로 깨졌다).
    # 필드 라벨은 그 자체가 이름인 경우만 잡는다(괄호주석 제거 후 완전일치).
    bare = re.sub(r"\(.*?\)", "", n).strip()
    if bare in HEADER_VOCAB or bare.replace(" ", "") in \
            tuple(v.replace(" ", "") for v in FIELD_LABEL_EXACT):
        return True
    # UI 어포던스 기호가 붙은 이름은 종목이 아니라 **누를 수 있는 것**이다('접기›', '더보기>').
    # 종목명에는 이 기호가 안 붙는다 — 앱별 어휘를 늘리지 않고 형태로 가른다.
    if re.search(r"[›»⌄˅>]\s*$", n):
        return True
    # 이름 안에 자릿수 구분 쉼표·소수점이 있는 수 = 그 라벨이 **자기 값을 달고 있다**
    # ('환율 1,482.00'). 종목명의 숫자는 지수·배수라 이런 서식이 없다('나스닥100', 'S&P500').
    if re.search(r"\d,\d{3}|\d\.\d{2}", n):
        return True
    return any(v in n for v in FIELD_LABEL_EXTRA)


def _coherent(rows):
    """**화면 내 일관성 필터** — 한 화면의 행들은 같은 종류다.

    계좌목록 화면이면 행마다 계좌토큰이 있고, 상품요약이면 행마다 카테고리다. UI 라벨이 최근접
    분할에서 금액을 훔쳐 만든 가짜 행은 그 성질이 없다. 다수결로 화면 성격을 정하고 벗어난 행을
    버린다 — `finalize.classify`의 account_summary 판정이 **모든** 행에 계좌토큰을 요구하므로
    (`all(keys)`) 가짜 행 하나가 화면 유형을 통째로 뒤집는다(측정: 160333이 detail로 오분류).
    판정 어휘는 finalize의 것을 그대로 쓴다(단일 출처)."""
    if len(rows) < 2:
        return rows
    accts = [r for r in rows if F.acct_tokens(r.get("name") or "")]
    cats = [r for r in rows if F.is_category((r.get("name") or "").strip())]
    if len(accts) >= 2 and len(accts) >= len(rows) * 0.5:
        return accts
    if len(cats) >= 2 and len(cats) >= len(rows) * 0.5:
        return cats
    return rows


# 상단 '총액 카드'의 라벨 어휘 — 화면 자신의 총액이 실리는 자리. H_VALUE와 분리한 이유:
# H_VALUE는 컬럼 헤더 판정에도 쓰여서 '자산'을 넣으면 '자산현황' 같은 제목이 헤더로 오인된다.
CARD_TOTAL_VOCAB = ("자산", "총자산", "총 자산", "평가금액", "평가액", "원화예수금", "외화예수금",
                    "보유주식", "내투자", "내 투자")


def _card_total(lines, below_y=None):
    """상단 총액 카드 → 화면 자신의 총액. 라벨(자산/평가금액/원화·외화 예수금)과 금액이
    같은 줄이거나, 금액이 바로 아랫줄에 단독으로 온다(큰 글씨 카드).

    필요한 이유(실측): mPOP 종합잔고는 컬럼헤더 위의 '자산 35,517,145원'이 화면 총액인데,
    헤더드 경로가 헤더 위 영역을 통째로 버려 총액이 유실됐다 — 상세행 합이 원 단위까지
    일치하는 화면이 '대조 불가'로 남아 다른 화면의 예수금과 비교되는 오경보를 냈다.
    예수금 탭(SMART)의 '원화 예수금 (D+2)' 카드도 같은 자리다 — 이 총액이 곧
    finalize._drop_deposit_echoes의 접기 근거(어느 시점이 자산인가)가 된다."""
    top = [ln for ln in lines
           if below_y is None or max(_cy(b) for b in ln) < below_y][:8]
    vocab = tuple(v.replace(" ", "") for v in CARD_TOTAL_VOCAB)

    def _is_card_label(t):
        bare = re.sub(r"\(.*?\)", "", t).replace(" ", "").strip()
        # 세 글자 이상 어휘는 접두 일치 허용 — 카드 라벨엔 계좌 한정어가 붙는다
        # ('보유주식 종합계좌', '내 투자?'). 두 글자 어휘('자산')는 완전일치만
        # ('자산현황' 같은 섹션 제목을 총액 카드로 오인하지 않게).
        return bare in vocab or any(len(v) >= 3 and bare.startswith(v) for v in vocab)

    for i, ln in enumerate(top):
        lab = next((b for b in ln if _is_card_label(b["text"])), None)
        if lab is None:
            continue
        amt = next((b for b in ln if b is not lab and _is_amount(b["text"])), None)
        if amt is None and i + 1 < len(top) and len(top[i + 1]) == 1 \
                and _is_amount(top[i + 1][0]["text"]):
            amt = top[i + 1][0]                  # 큰 금액이 다음 줄 단독으로(카드 레이아웃)
        if amt is not None:
            v = _clean_num(amt["text"])
            if v:
                return v
    return None


def _sum_match_total(lines, below_y, out_rows, m):
    """어휘 없는 카드 총액 — 상단 영역의 금액 중 **보유행 합과 2% 이내로 일치**하는 것.

    라벨 어휘(`CARD_TOTAL_VOCAB`)는 아는 앱까지만 간다. 총액의 정체는 라벨이 아니라
    **수치 자신**이 증명한다: Σ행과 일치하는 상단 금액은 우연이 아니면 총액이다(자기검증).
    어휘 매치가 우선이다 — 어휘 총액은 행 누락(스크롤에 잘린 화면)도 검출하지만, sum-match는
    이미 맞는 화면에서만 성립하므로 검출력이 없다. 행 2개 미만이면 하지 않는다(행 하나의
    화면은 그 행 값 자신과 '일치'하는 동어반복이 된다)."""
    name_i, val_i = F.COMPACT_COLUMNS.index("name"), F.COMPACT_COLUMNS.index("value")
    vals = [r[val_i] for r in out_rows if r[name_i] != F.SCREEN_TOTAL and r[val_i]]
    s = sum(vals)
    if len(vals) < 2 or s <= 0:
        return None
    top = [ln for ln in lines
           if below_y is None or max(_cy(b) for b in ln) < below_y][:8]
    for ln in top:
        for b in ln:
            v = _clean_num(b["text"]) if _is_amount(b["text"]) else None
            if v and abs(v - s) / s <= 0.02:
                return v
    return None


# ── broker/accountType 라벨 ──────────────────────────────────────────────────
def screen_label(lines):
    """화면 상단의 증권사·계좌 라벨 줄. '[Super365] 1234-5678-01' 처럼 브랜드+계좌번호가 온다 —
    정규화는 `resolve_broker`가 한다(여기서 판정하지 않는다)."""
    for ln in lines[:6]:
        t = " ".join(b["text"] for b in ln)
        if re.search(r"\d{4}-\d{4}-\d{2}|\d{8,}", t) or "[" in t:
            return t
    return ""


def bind(boxes):
    """OCR 박스 → 11칸 배열 리스트(finalize.parse_rows 입력 형식).

    **이미지 크기를 인자로 받지 않는다.** 예전엔 `width=1080` 기본값이 있었고 호출부는 아무도
    폭을 넘기지 않아, 어떤 기기의 화면이든 1080폭으로 간주됐다. 단위는 `metrics()`가 화면
    자신에게서 뽑는다(단일 출처)."""
    if not boxes:
        return []
    m = metrics(boxes)
    boxes = strip_chrome(boxes, m)
    lines = [ln for ln in group_lines(boxes) if not is_index_line([b["text"] for b in ln])]
    label = screen_label(lines)
    atype = account_type(label) or account_type(" ".join(
        b["text"] for ln in lines[:8] for b in ln))
    # broker는 **여기서 판정하지 않는다.** 화면 라벨을 그대로 넘기고 정규화는 `resolve_broker`가
    # 한다(정규명 직채택 / 브랜드→검색 / 계좌번호·별칭→같은 계좌 요약행 상속). 대괄호 안이
    # 브랜드인 화면('[Super365] 1234-5678-01')도 있고 상품명인 화면
    # ('1234567890-29[퇴직연금(다이렉트IRP)(비대면)]')도 있어, 여기서 고르면 단일 출처가 깨진다.
    # 화면 어딘가에 증권사 **정규명**이 떠 있으면(계좌목록 상단의 '삼성증권' 등) 라벨에 붙여준다.
    # finalize는 행 텍스트만 보고 `canonical_in`을 돌리는데, 그 정규명은 금액이 없는 라벨이라
    # 행이 되지 못해 finalize까지 도달하지 못한다 → 계좌목록의 broker가 비고, 그 계좌를 상속받는
    # 상세화면들까지 연쇄로 미해석된다(측정: ISA·IRP 화면 broker 26/30).
    canon = RB.canonical_in(" ".join(b["text"] for ln in lines for b in ln))
    broker = (f"{canon} {label}".strip() if canon else label.strip()) or None

    header = find_header(lines)
    hdr_y = None
    if header:
        bands = columns_from_header(header, m)
        name_x1 = max([b["x"] + b["w"] for b in header if _kind_of(b["text"]) == "name"]
                      + [int(m["x0"] + m["span"] * 0.42)])
        hdr_y = max(_cy(b) for b in header)
        body = [[b for b in ln if _cy(b) > hdr_y] for ln in lines]
        body = [ln for ln in body if ln]
        raw_rows = rows_from_headered(body, bands, name_x1, m) if bands else []
    else:
        raw_rows = rows_from_list(lines, m)

    raw_rows = _coherent(raw_rows)
    out = []
    for r in raw_rows:
        name = (r.get("name") or "").strip()
        if not name or len(name) < 2:
            continue
        if _is_field_label(name):
            # 필드 라벨은 보유행이 아니다. 단 **화면 자신의 평가금액 총액**은 버리지 말고
            # 예약행으로 넘긴다 — 게이트가 대조할 유일한 근거인 화면이 있다(전체계좌 폼처럼
            # 계좌요약 화면이 함께 올라오지 않는 경우). finalize가 이 행을 홀딩에서 빼고
            # 그 화면의 대조 기준으로 쓴다. VLM 경로는 이 행을 내지 않으므로 동작이 안 바뀐다.
            bare = re.sub(r"\(.*?\)", "", name).strip()
            if bare in H_VALUE and r.get("value"):
                tot = dict.fromkeys(F.COMPACT_COLUMNS)
                tot["name"] = F.SCREEN_TOTAL
                tot["value"] = _clean_num(r.get("value"))
                tot["confidence"] = 0.95
                out.append([tot[c] for c in F.COMPACT_COLUMNS])
            continue
        qty = None
        mq = QTY_RE.match(name.split()[-1]) if name.split() else None
        for key in ("qty",):
            if r.get(key):
                mq2 = QTY_RE.match(str(r[key]).strip())
                qty = _clean_num(mq2.group(1)) if mq2 else _clean_num(r[key])
        # 행마다 계좌 구분자가 붙은 화면(전체계좌 목록)에서는 **그 행의 구분자가 화면 라벨을
        # 이긴다** — 한 화면에 여러 계좌가 섞여 있을 수 있고, 화면 상단 라벨은 그중 하나거나 없다.
        r_broker = r.get("broker")
        row = {
            "broker": r_broker or broker,
            "accountType": (account_type(r_broker) if r_broker else None) or atype,
            "name": name,
            "assetClass": asset_class(name), "currency": r.get("currency"),
            "qty": qty, "price": _clean_num(r.get("price")) if r.get("price") else None,
            "value": _clean_num(r.get("value")) if r.get("value") else None,
            "cost": _clean_num(r.get("cost")) if r.get("cost") else None,
            "pnl": _clean_num(r.get("pnl")) if r.get("pnl") else None,
            "confidence": 0.95,
        }
        # 단가 모드 화면(mPOP '보유수량|매수단가/현재가' — 평가금액 열 없음)은 화면 자신의
        # 산식으로 값을 완성한다: 화면이 다른 모드에서 보여주는 바로 그 수치다(실측 08-10
        # ISA: 2×94,000=188,000, 2×138,900=277,800 — 값-모드 캡처와 원단위 일치). 값 열이
        # 있는 화면은 건드리지 않는다. 값 없이 내보내면 finalize가 행을 버려, 수량 모드로만
        # 찍은 계좌가 전수 누락된다.
        cu = _clean_num(r.get("cost_unit")) if r.get("cost_unit") else None
        if qty is not None and cu is not None and row["cost"] is None:
            row["cost"] = round(qty * cu, 2)
        if qty and row["price"] is not None and row["value"] is None:
            row["value"] = round(qty * row["price"], 2)
        out.append([row[c] for c in F.COMPACT_COLUMNS])
    # 화면 자신의 총액이 행 경로에서 안 나왔으면 상단 카드에서 줍는다(mPOP 종합잔고의 '자산',
    # 예수금 탭의 '원화 예수금 (D+2)'). 이미 있으면 그대로 — 진실의 출처는 하나면 된다.
    name_i = F.COMPACT_COLUMNS.index("name")
    if not any(r[name_i] == F.SCREEN_TOTAL for r in out):
        tot = _card_total(lines, hdr_y)
        if tot is None:
            tot = _sum_match_total(lines, hdr_y, out, m)
        if tot:
            tr = dict.fromkeys(F.COMPACT_COLUMNS)
            tr["name"], tr["value"], tr["confidence"] = F.SCREEN_TOTAL, tot, 0.95
            out.insert(0, [tr[c] for c in F.COMPACT_COLUMNS])
    return out


if __name__ == "__main__":
    import json
    import ocr
    for p in sys.argv[1:]:
        rows = bind(ocr.recognize(p))
        print(f"== {os.path.basename(p)}  ({len(rows)} rows)")
        print(json.dumps(rows, ensure_ascii=False, indent=1))
