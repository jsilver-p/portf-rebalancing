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
H_PRICE = ("현재가", "평균단가", "매입단가", "단가")
HEADER_VOCAB = H_NAME + H_VALUE + H_COST + H_PNL + H_RATE + H_QTY + H_PRICE

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
QTY_RE = re.compile(r"^([\d,]+)\s*주$")
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
    회계 항등식이 깨진다. 구분자에서 잘라 앞의 금액만 취한다."""
    s = re.sub(r"[원\s]", "", str(t))
    s = re.split(r"[|(]", s)[0].rstrip("|")
    if "%" in s or not s:
        return None
    return s if NUM_RE.match(s) else None


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
def strip_chrome(boxes, height):
    """상태바(시계+배터리) · 내비바 · 시장지수 줄을 뺀다. 위치 비율이 아니라 **내용**으로 판정한다
    (스크롤 스티칭된 4928px 화면에서 비율 규칙은 무너진다)."""
    out = []
    for b in boxes:
        t = b["text"].strip()
        # 상태바: 화면 최상단 줄의 시계/배터리/통신 아이콘 텍스트
        if b["y"] < height * 0.03 and (re.match(r"^\d{1,2}[:.]\d{2}", t) or "%" in t
                                       or len(t) <= 6):
            continue
        out.append(b)
    return out


# ── 2) 줄 그룹핑 ──────────────────────────────────────────────────────────────
def group_lines(boxes, tol=0.6):
    """y 중심이 서로 (평균 높이 × tol) 안이면 같은 줄. 표의 한 행은 보통 2줄로 이뤄진다."""
    if not boxes:
        return []
    bs = sorted(boxes, key=_cy)
    med_h = sorted(b["h"] for b in bs)[len(bs) // 2]
    lines, cur = [], [bs[0]]
    for b in bs[1:]:
        if abs(_cy(b) - _cy(cur[-1])) <= med_h * tol:
            cur.append(b)
        else:
            lines.append(sorted(cur, key=lambda x: x["x"]))
            cur = [b]
    lines.append(sorted(cur, key=lambda x: x["x"]))
    return lines


# ── 3) 헤더 → 열 정의 ────────────────────────────────────────────────────────
def find_header(lines):
    """헤더 어휘를 2개 이상 담은 첫 줄 묶음. 헤더 자체가 세로로 쌓여 있어(평가금액 위 / 매수금액
    아래) 인접 2줄까지 합쳐 본다. 반환: 헤더 박스 리스트 또는 None."""
    # 하단 내비바가 헤더로 오인된다 — '잔고'(H_VALUE)·'현재가'(H_PRICE)가 메뉴 이름이라
    # 어휘만 보면 2히트가 난다(측정 160139: ['메뉴','HOME','국내','잔고','현재가',…]).
    # 구조로 배제한다: **진짜 헤더는 아래에 금액이 있다.** 내비바 밑에는 아무것도 없다.
    below_amounts = [0] * len(lines)
    n_amt = 0
    for i in range(len(lines) - 1, -1, -1):
        below_amounts[i] = n_amt
        n_amt += sum(1 for b in lines[i] if _is_amount(b["text"]))
    for i, ln in enumerate(lines):
        hits = [b for b in ln if any(v in b["text"] for v in HEADER_VOCAB)]
        if len(hits) >= 2 and below_amounts[i] >= 2:
            merged = list(ln)
            if i + 1 < len(lines):                     # 스택된 헤더 2번째 줄 흡수
                nxt = lines[i + 1]
                if any(any(v in b["text"] for v in HEADER_VOCAB) for b in nxt) and \
                        not any(_is_amount(b["text"]) for b in nxt):
                    merged += nxt
            return merged
    return None


def _kind_of(text):
    """헤더 텍스트 → 의미 열. 긴 어휘부터 봐야 '평가손익'이 '평가금액'에 먹히지 않는다."""
    t = str(text)
    for keys, kind in ((H_PNL, "pnl"), (H_RATE, "rate"), (H_COST, "cost"), (H_VALUE, "value"),
                       (H_QTY, "qty"), (H_PRICE, "price"), (H_NAME, "name")):
        if any(k in t for k in keys):
            return kind
    return None


def columns_from_header(header):
    """헤더 박스 → [{kinds:[의미,...], x0, x1}] — kinds는 **밴드 내 y 순서**(=데이터 스택 순서).
    금액 열은 우측 정렬이라 오른쪽 끝(right)으로 밴드를 만든다."""
    marks = []
    for b in header:
        k = _kind_of(b["text"])
        if k:
            marks.append({"kind": k, "y": _cy(b), "x0": b["x"], "x1": _right(b),
                          "anchor": _right(b)})
    marks.sort(key=lambda m: m["anchor"])
    bands = []
    for m in marks:
        if bands and abs(m["anchor"] - bands[-1]["anchor"]) < 90:   # 같은 열의 스택된 헤더
            bands[-1]["marks"].append(m)
            bands[-1]["x0"] = min(bands[-1]["x0"], m["x0"])
            bands[-1]["x1"] = max(bands[-1]["x1"], m["x1"])
        else:
            bands.append({"marks": [m], "x0": m["x0"], "x1": m["x1"], "anchor": m["anchor"]})
    for b in bands:
        b["marks"].sort(key=lambda m: m["y"])           # 위→아래 = 데이터 스택 순서
        b["kinds"] = [m["kind"] for m in b["marks"]]
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


def rows_from_headered(lines, bands, name_x1):
    """헤더가 있는 표: 금액 앵커 열(가장 오른쪽 밴드)의 셀 묶음이 행을 정의한다."""
    anchor = bands[-1]
    depth = max(1, len(anchor["kinds"]))
    amounts = [b for ln in lines for b in ln
               if _is_amount(b["text"]) and abs(_right(b) - anchor["anchor"]) < 120]
    amounts.sort(key=_cy)
    groups = [amounts[i:i + depth] for i in range(0, len(amounts), depth)]
    rows = []
    for g in groups:
        if not g:
            continue
        y0, y1 = _cy(g[0]), _cy(g[-1])
        lo, hi = y0 - 70, y1 + 70                       # 이름이 두 줄 사이에 중앙정렬될 수 있다
        row = {}
        for band in bands:
            if band["kinds"] == ["name"]:
                continue
            # 앵커(우측 정렬 끝)로 판정한다 — 데이터 박스는 헤더 박스보다 넓어서 헤더의
            # x1로 자르면 몇 px 차이로 전부 탈락한다(측정: 헤더 right=1002, 데이터 right=1064).
            cells = [b for ln in lines for b in ln
                     if lo <= _cy(b) <= hi and abs(_right(b) - band["anchor"]) < 120
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


def rows_from_list(lines, width=1080):
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
    labels = [b for b in boxes
              if not _is_amount(b["text"]) and "%" not in b["text"] and b["x"] < width * 0.5
              and not QTY_RE.match(b["text"].strip())]
    # 한 화면에서 **똑같은 문구가 반복되면 UI 크롬**이다(계좌마다 붙는 '이체'·'거래내역'·'주식주문').
    # 행 이름은 화면에서 유일하다('한 자산 = 한 행' 불변식). 안 걸러내면 이 크롬이 최근접 분할에서
    # 금액을 훔쳐 가짜 행을 만들고, 화면 유형 판정까지 뒤집는다(측정: 160333이
    # account_summary → detail 로 오분류).
    seen = {}
    for b in labels:
        seen[b["text"].strip()] = seen.get(b["text"].strip(), 0) + 1
    amounts = [b for b in boxes if _is_amount(b["text"]) and _right(b) > width * 0.45]
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
    med_h = sorted(b["h"] for b in boxes)[len(boxes) // 2]
    acy = sorted(_cy(a) for a in amounts)
    inter = [b - a for a, b in zip(acy, acy[1:]) if (b - a) > med_h * 2]
    pitch = sorted(inter)[len(inter) // 2] if inter else med_h * 4
    limit = max(med_h * 1.6, pitch * 0.60)

    # ── 계좌 구분자 분리 ─────────────────────────────────────────────────────
    # '전체계좌' 화면은 종목마다 **그 위에** 계좌 라벨을 다시 찍는다(측정
    # 20260731T085311Z img1: '1234-5678-01(Super365)'가 380px 간격으로 10회, 각 라벨 바로
    # 아래에 종목명·수량·평가금액·손익 한 블록). 이건 행 이름이 아니라 **행 그룹의 머리**이고,
    # 그 행의 broker 근거다. 안 갈라내면 값은 다 맞는데 broker가 통째로 None이 된다(실측 15/15).
    #
    # **반복 횟수로 판별하면 안 된다.** 처음엔 `반복 2회 이상 + 계좌토큰`으로 잡았는데,
    # 종목이 **하나뿐인 계좌**는 구분자가 1회만 나와 걸리지 않는다 → 그 라벨이 종목명 행으로
    # 둔갑해 아래 행의 손익을 훔친다(측정 9:1 합성: 가짜 행 `1234-5678-77(Super365)`
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

    buckets = {}
    for a in amounts:
        near = _nearest_label(a)
        if near is not None:
            buckets.setdefault(id(near), (near, []))[1].append(a)
    qty_by_label = {}
    for q in qtys:
        nq = _nearest_label(q)
        if nq is not None:
            qty_by_label.setdefault(id(nq), q)
    fx_by_label = {}
    for b in boxes:                                     # 외화 잔액('6,923.28 USD') → qty + 통화
        m = FX_QTY_RE.match(b["text"].strip())
        if not m or float(m.group(1).replace(",", "")) == 0:   # 잔액 0 행은 제외(규칙 8)
            continue
        nb = _nearest_label(b)
        if nb is not None:
            fx_by_label.setdefault(id(nb), m)
    def _sep_above(cy):
        """이 행을 덮는 계좌 구분자 = **바로 위**의 구분자. 화면이 계좌별로 쪼개져 있으면
        행마다 다른 계좌가 잡힌다(사용자 지적: '각 계좌로 나눌 수 있으면 되는 거야').
        위에 아무것도 없으면 None — 지어내지 않는다."""
        above = [s for s in seps if _cy(s) <= cy]
        return max(above, key=_cy)["text"].strip() if above else None

    rows = []
    for key, (near, amts) in sorted(buckets.items(), key=lambda kv: _cy(kv[1][0])):
        amts.sort(key=_cy)
        row = {"name": near["text"].strip(), "value": amts[0]["text"]}
        sep = _sep_above(_cy(near))
        if sep:
            row["broker"] = sep          # 정규화는 resolve_broker가 한다(여기서 판정 안 함)
        if len(amts) > 1:
            row["pnl"] = amts[1]["text"]
        if key in qty_by_label:
            row["qty"] = qty_by_label[key]["text"]
        if key in fx_by_label:
            m = fx_by_label[key]
            row["qty"], row["currency"] = m.group(1), m.group(2)
        rows.append(row)
    return rows


FIELD_LABEL_EXTRA = ("출금가능", "총자산", "총 자산", "신용", "대출", "예상금액")


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
    if bare in HEADER_VOCAB:
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


# ── broker/accountType 라벨 ──────────────────────────────────────────────────
def screen_label(lines):
    """화면 상단의 증권사·계좌 라벨 줄. '[Super365] 1234-5678-01' 처럼 브랜드+계좌번호가 온다 —
    정규화는 `resolve_broker`가 한다(여기서 판정하지 않는다)."""
    for ln in lines[:6]:
        t = " ".join(b["text"] for b in ln)
        if re.search(r"\d{4}-\d{4}-\d{2}|\d{8,}", t) or "[" in t:
            return t
    return ""


def bind(boxes, width=1080, height=None):
    """OCR 박스 → 11칸 배열 리스트(finalize.parse_rows 입력 형식)."""
    height = height or (max((b["y"] + b["h"]) for b in boxes) if boxes else 0)
    boxes = strip_chrome(boxes, height)
    lines = [ln for ln in group_lines(boxes) if not is_index_line([b["text"] for b in ln])]
    label = screen_label(lines)
    atype = account_type(label) or account_type(" ".join(
        b["text"] for ln in lines[:8] for b in ln))
    # broker는 **여기서 판정하지 않는다.** 화면 라벨을 그대로 넘기고 정규화는 `resolve_broker`가
    # 한다(정규명 직채택 / 브랜드→검색 / 계좌번호·별칭→같은 계좌 요약행 상속). 대괄호 안이
    # 브랜드인 화면('[Super365] 1234-5678-01')도 있고 상품명인 화면
    # ('1234567891-29[퇴직연금(다이렉트IRP)(비대면)]')도 있어, 여기서 고르면 단일 출처가 깨진다.
    # 화면 어딘가에 증권사 **정규명**이 떠 있으면(계좌목록 상단의 '삼성증권' 등) 라벨에 붙여준다.
    # finalize는 행 텍스트만 보고 `canonical_in`을 돌리는데, 그 정규명은 금액이 없는 라벨이라
    # 행이 되지 못해 finalize까지 도달하지 못한다 → 계좌목록의 broker가 비고, 그 계좌를 상속받는
    # 상세화면들까지 연쇄로 미해석된다(측정: ISA·IRP 화면 broker 26/30).
    canon = RB.canonical_in(" ".join(b["text"] for ln in lines for b in ln))
    broker = (f"{canon} {label}".strip() if canon else label.strip()) or None

    header = find_header(lines)
    if header:
        bands = columns_from_header(header)
        name_x1 = max([b["x"] + b["w"] for b in header if _kind_of(b["text"]) == "name"]
                      + [int(width * 0.42)])
        hdr_y = max(_cy(b) for b in header)
        body = [[b for b in ln if _cy(b) > hdr_y] for ln in lines]
        body = [ln for ln in body if ln]
        raw_rows = rows_from_headered(body, bands, name_x1) if bands else []
    else:
        raw_rows = rows_from_list(lines, width)

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
        out.append([row[c] for c in F.COMPACT_COLUMNS])
    return out


if __name__ == "__main__":
    import json
    import ocr
    for p in sys.argv[1:]:
        rows = bind(ocr.recognize(p))
        print(f"== {os.path.basename(p)}  ({len(rows)} rows)")
        print(json.dumps(rows, ensure_ascii=False, indent=1))
