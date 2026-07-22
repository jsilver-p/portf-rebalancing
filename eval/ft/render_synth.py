#!/usr/bin/env python3
"""합성 학습 데이터 렌더러 v2 — 전 화면 클래스 6종.

실화면(민감정보)은 기하·팔레트 참조로만 씀. 여기서 나가는 픽셀·값은 전부 가짜.
출력: 1080xH 렌더 → LANCZOS ×0.5 28px 스냅 JPEG — 실화면 평가 파이프라인과 동일 처리.
정답: 32B e0_fresh PASS 런의 필드 패턴을 클래스별로 그대로 따르는 11칸 JSON.
name 규칙 명시 반영: 항목명만 — 계좌번호·예금주 이름 금지(화면엔 렌더하되 정답에서 배제).

클래스: mpop_detail(종합잔고 병합셀표) fx(SMART 외화예수금) smart_krw(원화예수금)
        smart_detail(SMART 보유잔고 국내/해외, 톨 변형) smart_summary(MY자산현황)
        mpop_accounts(mPOP 계좌별)

사용: python3 render_synth.py <outdir> <seed> [n_per_class]
"""
import json, os, random, sys
from PIL import Image, ImageDraw, ImageFont

W = 1080
FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_B = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"

BLUE_BANNER = (34, 136, 237)
HDR_BG = (230, 230, 230)
SMART_BG = (244, 244, 244)
GRAY_TXT = (124, 124, 124)
DARK = (30, 30, 34)
PNL_BLUE = (47, 105, 226)
PNL_RED = (229, 52, 52)
DIVIDER = (225, 225, 228)
NAVY = (26, 32, 58)

_fc = {}
def F(size, bold=False):
    k = (size, bold)
    if k not in _fc:
        _fc[k] = ImageFont.truetype(FONT_B if bold else FONT, size, index=0)
    return _fc[k]

def comma(n):
    neg = n < 0
    s = f"{abs(n):,}"
    return ("-" if neg else "") + s

def signed(n):
    return ("+" if n > 0 else "") + comma(n)

def snap28(v):
    return max(28, round(v / 28) * 28)

def downhalf(im):
    out = im.resize((snap28(im.width * 0.5), snap28(im.height * 0.5)), Image.LANCZOS)
    if random.random() < 0.7:  # 증강: 밝기·대비 지터 (수치 경로 민감성에 대한 마진 확보)
        from PIL import ImageEnhance
        out = ImageEnhance.Brightness(out).enhance(random.uniform(0.96, 1.04))
        out = ImageEnhance.Contrast(out).enhance(random.uniform(0.96, 1.04))
    return out

def jpeg_q():
    return random.randint(85, 95)

def pnl_color(n):
    return PNL_RED if n > 0 else PNL_BLUE if n < 0 else DARK

def status_bar(d):
    d.text((40, 22), f"{random.randint(1,12)}:{random.randint(0,59):02d}", font=F(34), fill=DARK)
    d.text((W - 200, 22), f"5G {random.randint(15,99)}%", font=F(30), fill=DARK)

def wrap2(d, text, font, maxw):
    if d.textlength(text, font=font) <= maxw:
        return [text]
    for i in range(len(text) - 1, 0, -1):
        if d.textlength(text[:i], font=font) <= maxw:
            return [text[:i], text[i:]]
    return [text]

def rtext(d, xr, y, s, font, fill):
    d.text((xr - d.textlength(s, font=font), y), s, font=font, fill=fill)

# ---------- 이름 풀 ----------
GROWTH = ["미국나스닥100", "미국S&P500", "미국테크TOP10", "반도체TOP10", "2차전지산업",
          "차이나휴머노이드로봇", "K방산산업", "AI코리아액티브", "미국성장기업", "글로벌테크액티브",
          "엔비디아밸류체인액티브", "차이나AI소프트웨어"]
DIVID = ["고배당주", "은행고배당플러스", "미국배당다우존스", "고배당커버드콜"]
BOND = ["국고채10년", "미국채30년액티브", "종합채권액티브", "단기채권플러스"]
GOLD = ["KRX금현물", "골드선물(H)", "원자재선물특별자산"]
REIT = ["리츠부동산인프라", "미국리츠부동산"]
MIX = ["채권혼합밸런스", "TDF2045채권혼합", "미국나스닥100채권혼합50액티브", "고배당주채권혼합"]
STOCKS = ["한빛중공업", "대양전자", "미래바이오텍", "한류엔터", "동방물산", "청담제약", "신라정밀",
          "누리반도체", "제일화학", "동해조선"]
US_NAMES = ["엔비디아", "알파벳 A", "마이크로소프트", "테슬라", "팔란티어 테크", "아이온큐",
            "메타 플랫폼스(페이스북)", "리게티 컴퓨팅", "솔리드 파워", "브로드컴",
            "AIPO", "ARKF", "IVV", "SCHD", "VOO", "QQQM", "SMH", "SCHG", "RKLB", "TSLY"]
PREFIX = ["TIGER", "ACE", "KODEX", "PLUS", "SOL", "RISE", "TIME"]
HOLDERS = ["김철수", "이영희", "박민준", "정수아", "최동현"]
BRANDS = ["Super365", "Prime100", "Star247", "Hero365", "Plus100"]
KR_BROKERS = ["삼성증권", "미래에셋증권", "한국투자증권", "NH투자증권"]

def asset_class(name):
    """규칙 6 키워드 → assetClass. 키워드 없으면 개별 한글주=성장주, 그 외 null."""
    for pool, cls in [(GOLD, "금·원자재"), (MIX, "혼합(주식·채권)"), (BOND, "채권"),
                      (DIVID, "배당주"), (REIT, "부동산"), (GROWTH, "성장주")]:
        if any(t in name for t in pool):
            return cls
    if name in STOCKS:
        return "성장주"
    if "현금" in name or "예수금" in name or "CMA" in name:
        return "현금"
    return None

def pick_etf():
    theme = random.choice([GROWTH, DIVID, BOND, GOLD, REIT, MIX])
    name = f"{random.choice(PREFIX)} {random.choice(theme)}"
    return name, asset_class(name)

def pick_kr():
    if random.random() < 0.4:
        n = random.choice(STOCKS)
        return n, asset_class(n)
    return pick_etf()

ACCT_TYPES = [("ISA(평생혜택 중개형)(비대면)", "ISA"), ("연금저축계좌", "연금저축"),
              ("퇴직연금(다이렉트IRP)(비대면)", "퇴직연금"), ("종합매매(비대면)", "일반")]

def money_pair():
    # 자릿수 다양화: 수천원~수천만원. 실화면 평가금액은 대개 6~7자리 —
    # 구 분포(≤45만)는 큰 값(2천만+) OOD 오독의 원인이라 6~7자리 비중을 높인다.
    mag = 10 ** random.choice([4, 5, 5, 6, 6, 6, 7, 7])
    cost = round(random.uniform(0.3, 9.9) * mag / 10) * 10
    pnl = round(cost * random.uniform(-0.45, 0.45) / 10) * 10
    return cost + pnl, cost, pnl

# ---------- 공용: SMART 헤더(계좌박스+탭) ----------
SMART_TABS = ["국내주식", "해외주식", "원화예수금", "외화예수금"]

def smart_header(d, active):
    d.rectangle([0, 0, W, 470], fill="white")
    status_bar(d)
    d.text((40, 105), "<", font=F(50, True), fill=DARK)
    d.text((110, 108), "보유계좌 상품별 자산현황", font=F(44, True), fill=DARK)
    d.ellipse([W - 190, 115, W - 150, 155], outline=(120, 120, 124), width=3)
    d.ellipse([W - 110, 115, W - 70, 155], outline=(120, 120, 124), width=3)
    brand = random.choice(BRANDS)
    accno = f"{random.randint(1000,9999)}-{random.randint(1000,9999)}-{random.randint(10,99):02d}"
    d.rectangle([40, 215, W - 40, 330], outline=(200, 200, 204), width=2)
    d.text((70, 245), f"[{brand}] {accno}", font=F(40), fill=DARK)
    d.polygon([(W - 120, 258), (W - 80, 258), (W - 100, 288)], fill=GRAY_TXT)
    xs = [30, 290, 550, 810]
    for t, x in zip(SMART_TABS, xs):
        a = t == active
        d.text((x, 395), t, font=F(38, a), fill=DARK if a else (165, 165, 170))
        if a:
            wpx = d.textlength(t, font=F(38, True))
            d.line([x, 465, x + wpx, 465], fill=DARK, width=5)
    return brand

def smart_date(d, xr, y):
    rtext(d, xr, y, f"26.{random.randint(1,12):02d}.{random.randint(1,28):02d}", F(34), GRAY_TXT)

# ---------- 클래스 A: SMART 보유잔고 상세 (국내/해외, 톨 변형) ----------
def render_smart_detail():
    domestic = random.random() < 0.5
    n = random.randint(3, 5)
    rows = []
    for _ in range(n):
        if domestic:
            name, cls = pick_kr()
        else:
            name = random.choice(US_NAMES)
            cls = asset_class(name)
        qty = random.choice([random.randint(1, 20), random.randint(20, 300)])
        value, cost, pnl = money_pair()
        rows.append((name, cls, qty, value, cost, pnl))
    total_v = sum(r[3] for r in rows)
    total_p = sum(r[5] for r in rows)
    total_c = sum(r[4] for r in rows)

    H = 1000 + n * 255 + 120
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    brand = smart_header(d, "국내주식" if domestic else "해외주식")

    d.rectangle([0, 470, W, 1000], fill=SMART_BG)
    d.rounded_rectangle([30, 500, W - 30, 960], 24, fill="white")
    d.text((80, 545), "평가금액", font=F(36), fill=GRAY_TXT)
    smart_date(d, W - 90, 545)
    d.text((80, 620), f"{comma(total_v)}원", font=F(76, True), fill=DARK)
    d.text((80, 760), "평가손익", font=F(36), fill=GRAY_TXT)
    rtext(d, W - 90, 760, f"{signed(total_p)}원  |  {total_p/total_c*100:+.2f}%",
          F(36), pnl_color(total_p))
    d.text((80, 850), "신용/대출총액", font=F(36), fill=GRAY_TXT)
    rtext(d, W - 90, 850, "0원", F(36), DARK)

    d.text((60, 1030), "보유잔고", font=F(40, True), fill=(60, 90, 180))
    rtext(d, W - 60, 1030, "체결내역 ›", F(38), DARK)
    d.line([0, 1105, W, 1105], fill=(90, 100, 140), width=3)

    y = 1130
    for name, cls, qty, value, cost, pnl in rows:
        if not domestic:
            d.ellipse([40, y + 22, 68, y + 50], fill=(60, 190, 170))
        nx = 90 if not domestic else 60
        lines = wrap2(d, name, F(42, True), 620)
        ny = y + 10
        for ln in lines:
            d.text((nx, ny), ln, font=F(42, True), fill=DARK)
            ny += 54
        d.text((nx, ny + 4), f"{qty}주", font=F(36), fill=GRAY_TXT)
        rtext(d, W - 60, y + 55, f"{comma(value)}원", F(42, True), DARK)
        rtext(d, W - 60, y + 125,
              f"{signed(pnl)}원  |  {pnl/cost*100:+.2f}%", F(36), pnl_color(pnl))
        y += 255
        d.line([40, y - 18, W - 40, y - 18], fill=DIVIDER, width=2)

    gt = []
    for name, cls, qty, value, cost, pnl in rows:
        if domestic:
            gt.append([brand, "일반", name, cls, "KRW", qty, None, value, None, pnl, 1])
        else:
            gt.append([brand, "일반", name, cls, "KRW", qty,
                       round(value / qty), value, cost, pnl, 1])
    return im, gt

# ---------- 클래스 B: SMART MY자산현황 (상품요약) ----------
def render_smart_summary():
    im = Image.new("RGB", (W, 2340), "white")
    d = ImageDraw.Draw(im)
    status_bar(d)
    d.text((40, 105), "<", font=F(50, True), fill=DARK)
    d.text((110, 108), "MY자산현황", font=F(44, True), fill=DARK)

    dom_v, dom_c, dom_p = money_pair()
    ovs_v, ovs_c, ovs_p = money_pair()
    krw_cash = random.randint(1000, 99999999)
    usd_krw = random.randint(1000, 30000000)
    total = dom_v + ovs_v + krw_cash + usd_krw
    total_p = dom_p + ovs_p
    total_c = dom_c + ovs_c

    d.rectangle([0, 200, W, 760], fill=(250, 249, 247))
    d.text((60, 240), "총 자산", font=F(40, True), fill=DARK)
    smart_date(d, W - 70, 240)
    d.text((60, 330), f"{comma(total)}원", font=F(72, True), fill=DARK)
    d.ellipse([W - 130, 340, W - 80, 390], outline=GRAY_TXT, width=3)
    d.text((60, 480), "평가손익", font=F(38), fill=(90, 90, 94))
    rtext(d, W - 70, 480, f"{signed(total_p)}원  |  {total_p/total_c*100:+.2f}%",
          F(38), pnl_color(total_p))
    d.text((60, 570), "출금가능금액", font=F(38), fill=(90, 90, 94))
    rtext(d, W - 70, 570, comma(krw_cash), F(38), DARK)
    d.ellipse([525, 690, 545, 710], fill=DARK)
    d.ellipse([560, 690, 580, 710], fill=(205, 205, 208))

    d.rectangle([0, 780, W, 950], fill=(252, 243, 212))
    d.text((60, 810), "신용/대출금액", font=F(36), fill=(110, 90, 40))
    rtext(d, W - 70, 810, "0원", F(36), DARK)
    d.text((60, 880), "MY대출예상액", font=F(36), fill=(110, 90, 40))
    rtext(d, W - 70, 880, "예상금액조회", F(36), (120, 120, 200))

    d.rounded_rectangle([40, 1000, 400, 1090], 18, outline=DARK, width=3)
    d.text((90, 1020), "상품별", font=F(38, True), fill=DARK)
    d.text((250, 1020), "계좌별", font=F(38), fill=(165, 165, 170))
    rtext(d, W - 60, 1020, "전체 ▾".replace("▾", ""), F(38), DARK)
    d.polygon([(W - 60, 1040), (W - 30, 1040), (W - 45, 1065)], fill=DARK)

    items = [("국내주식", dom_v, dom_p), ("해외주식", ovs_v, ovs_p),
             ("원화예수금", krw_cash, None), ("외화예수금", usd_krw, None)]
    y = 1130
    for name, v, p in items:
        d.text((60, y + 40), name, font=F(42, True), fill=DARK)
        rtext(d, W - 100, y + 15, f"{comma(v)}원", F(42, True), DARK)
        d.text((W - 70, y + 20), "›", font=F(40), fill=GRAY_TXT)
        if p is not None:
            rtext(d, W - 100, y + 85, f"{signed(p)}원", F(38), pnl_color(p))
        y += 175
        d.line([40, y - 12, W - 40, y - 12], fill=DIVIDER, width=2)

    d.rectangle([0, 2160, W, 2340], fill="white")
    d.line([0, 2160, W, 2160], fill=DIVIDER, width=2)
    for i, t in enumerate(["메뉴", "HOME", "국내\n잔고", "해외\n현재가", "해외\n관심", "입출금\n내역"]):
        d.multiline_text((40 + i * 170, 2195), t, font=F(30), fill=(90, 90, 94))

    gt = [[None, "일반", "국내주식", "성장주", "KRW", None, None, dom_v, None, dom_p, 1],
          [None, "일반", "해외주식", "성장주", "USD", None, None, ovs_v, None, ovs_p, 1],
          [None, "일반", "원화예수금", "현금", "KRW", None, None, krw_cash, None, None, 1],
          [None, "일반", "외화예수금", "현금", "USD", None, None, usd_krw, None, None, 1]]
    return im, gt

# ---------- 클래스 C: SMART 원화예수금 ----------
def render_smart_krw():
    im = Image.new("RGB", (W, 2340), SMART_BG)
    d = ImageDraw.Draw(im)
    brand = smart_header(d, "원화예수금")
    amt = random.randint(1000, 99999999)

    d.rounded_rectangle([30, 510, W - 30, 950], 24, fill="white")
    d.text((80, 555), "원화 예수금  (D+2)", font=F(36), fill=GRAY_TXT)
    smart_date(d, W - 90, 555)
    d.text((80, 650), f"{comma(amt)}원", font=F(80, True), fill=DARK)
    d.rounded_rectangle([560, 840, W - 80, 915], 14, fill=(252, 243, 212))
    d.text((600, 858), "자동투자 수익 보기 ›", font=F(34), fill=(120, 95, 30))

    d.rectangle([0, 1000, W, 2340], fill="white")
    d.text((60, 1030), "예수금 상세", font=F(38, True), fill=(60, 90, 180))
    rtext(d, W - 60, 1030, "이체하기 ›", F(36), DARK)
    d.line([0, 1100, W, 1100], fill=(90, 100, 140), width=3)
    y = 1120
    for label in ["예수금 (당일)", "추정예수금 (D+1)", "추정예수금 (D+2)"]:
        d.text((60, y + 25), label, font=F(38), fill=DARK)
        rtext(d, W - 60, y + 25, f"{comma(amt)}원", F(38), DARK)
        y += 105
        d.line([0, y, W, y], fill=DIVIDER, width=2)

    d.text((60, y + 40), "출금가능금액", font=F(38, True), fill=(60, 90, 180))
    d.line([0, y + 110, W, y + 110], fill=(90, 100, 140), width=3)
    d.text((60, y + 135), "출금가능금액", font=F(38), fill=DARK)
    rtext(d, W - 60, y + 135, f"{comma(amt)}원", F(38), DARK)
    y += 240
    d.rectangle([320, y, W - 320, y + 75], fill=(238, 238, 240))
    d.text((450, y + 15), "접기 ▴".replace(" ▴", ""), font=F(34), fill=(90, 90, 94))
    y += 95
    for label, v in [("현금", amt), (f"자동투자({brand}/CMA)", 0), ("수표", 0)]:
        d.text((60, y + 20), label, font=F(38), fill=DARK)
        rtext(d, W - 60, y + 20, f"{comma(v)}원", F(38), DARK)
        y += 100
        d.line([0, y, W, y], fill=DIVIDER, width=2)
    d.text((60, y + 40), "기타", font=F(38, True), fill=(60, 90, 180))
    d.text((60, y + 120), "청약증거금", font=F(38), fill=DARK)
    rtext(d, W - 60, y + 120, "0원", F(38), DARK)

    gt = [[brand, "일반", "원화예수금", "현금", "KRW", None, None, amt, None, None, 1]]
    return im, gt

# ---------- 클래스 D: SMART 외화예수금 (v1 유지) ----------
def render_fx():
    im = Image.new("RGB", (W, 2340), SMART_BG)
    d = ImageDraw.Draw(im)
    brand = smart_header(d, "외화예수금")
    usd = round(random.uniform(3, 60000), 2)
    fxr = round(random.uniform(1150, 1650), 2)
    krw = int(usd * fxr)

    d.rounded_rectangle([30, 510, W - 30, 950], 24, fill="white")
    d.text((80, 555), "평가금액", font=F(36), fill=GRAY_TXT)
    smart_date(d, W - 90, 555)
    d.text((80, 650), f"{comma(krw)}원", font=F(80, True), fill=DARK)
    d.rounded_rectangle([560, 840, W - 80, 915], 14, fill=(252, 243, 212))
    d.text((600, 858), "자동투자 수익 보기 ›", font=F(34), fill=(120, 95, 30))

    d.rectangle([0, 1000, W, 2340], fill="white")
    d.text((60, 1030), "보유 외화", font=F(38, True), fill=(60, 90, 180))
    d.ellipse([250, 1034, 286, 1070], outline=(60, 90, 180), width=2)
    d.text((262, 1034), "i", font=F(28), fill=(60, 90, 180))
    rtext(d, W - 60, 1030, "환전하기 ›", F(36), DARK)
    d.line([0, 1100, W, 1100], fill=(90, 100, 140), width=3)

    d.text((60, 1140), "미국 달러", font=F(42, True), fill=DARK)
    d.text((60, 1210), f"환율 {fxr:,.2f}", font=F(34), fill=GRAY_TXT)
    rtext(d, W - 60, 1135, f"{usd:,.2f} USD", F(42, True), DARK)
    rtext(d, W - 60, 1210, f"{comma(krw)}원", F(36), GRAY_TXT)
    d.line([0, 1280, W, 1280], fill=DIVIDER, width=2)

    y = 1300
    if random.random() < 0.7:
        d.text((60, y + 40), "자동투자", font=F(40), fill=DARK)
        rtext(d, W - 60, y + 40, f"{usd:,.2f} USD", F(40), DARK)
        y += 150
        d.line([0, y, W, y], fill=DIVIDER, width=2)
    if random.random() < 0.8:
        d.rectangle([0, 1950, W, 2020], fill=(238, 238, 240))
        d.text((60, 1965), "기타", font=F(34, True), fill=(60, 90, 180))
        d.text((60, 2050), "외화증거금", font=F(38), fill=DARK)
        d.text((60, 2115), f"환율 {fxr:,.2f}", font=F(32), fill=GRAY_TXT)
        rtext(d, W - 60, 2045, "0.00 USD", F(38), DARK)
        rtext(d, W - 60, 2110, "0원", F(34), GRAY_TXT)

    gt = [[brand, None, "미국 달러", "현금", "USD", usd, None, krw, None, None, 1]]
    return im, gt

# ---------- 클래스 E: mPOP 종합잔고 상세 (병합셀표, v1 확장: 가변 행·qty 서브라인) ----------
def render_mpop_detail():
    n = random.randint(2, 5)
    label_full, acct_short = random.choice(ACCT_TYPES)
    accno = f"{random.randint(10**9,10**10-1)}-{random.randint(10,99)}"
    acct_label = f"{accno} [{label_full}]"  # 상단 표시(계좌번호+상품) — broker 아님(음성 표본)

    rows = []
    for _ in range(n):
        name, cls = pick_kr()
        value, cost, pnl = money_pair()
        qty = random.randint(1, 300) if random.random() < 0.3 else None
        rows.append((name, cls, value, cost, pnl, qty))
    cash = random.choice([random.randint(1, 9), random.randint(10, 99999)]) \
        if random.random() < 0.7 else None
    # 증권사명은 '현금성자산(증권사)' 행에만 나타난다. 이 행이 있으면 broker=그 증권사(계좌 전 행 공통),
    # 없으면 broker=null. 계좌번호·상품라벨은 절대 broker가 아니다(단일 출처: prompt4f 규칙과 일치).
    firm = random.choice(KR_BROKERS)
    special = random.random() < 0.55  # 현금성자산(증권사) 행 (pnl 0, cost=value)
    if special:
        v = random.randint(100000, 9000000)
        rows.insert(0, (f"현금성자산({firm})", "현금", v, v, 0, None))
    broker = firm if special else None

    total_v = sum(r[2] for r in rows) + (cash or 0)
    total_c = sum(r[3] for r in rows)
    total_p = sum(r[4] for r in rows)

    H = 900 + (len(rows) + (1 if cash is not None else 0)) * 205 + 400
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    status_bar(d)
    d.text((40, 110), "<", font=F(52, True), fill=DARK)
    d.text((110, 112), "종합잔고", font=F(48, True), fill=DARK)
    for dy in (0, 22, 44):
        d.ellipse([W - 76, 118 + dy, W - 64, 130 + dy], fill=DARK)
    d.text((60, 255), acct_label, font=F(36), fill=(70, 70, 74))

    d.rectangle([0, 330, W, 560], fill=BLUE_BANNER)
    d.text((60, 380), "자산", font=F(44, True), fill="white")
    xr = W - 150
    rtext(d, xr, 365, comma(total_v), F(72, True), "white")
    d.text((xr + 12, 392), "원", font=F(44), fill="white")
    rtext(d, W - 80, 470, f"{signed(total_p)}원({total_p/total_c*100:+.1f}%)",
          F(38), (225, 235, 250))

    d.text((60, 615), "예금자보호안내", font=F(32), fill=GRAY_TXT)
    d.ellipse([310, 618, 344, 652], outline=GRAY_TXT, width=2)
    d.text((321, 618), "i", font=F(26), fill=GRAY_TXT)
    for k in range(3):
        x0 = W - 230 + k * 70
        d.rectangle([x0, 618, x0 + 40, 652], outline=(90, 110, 200), width=3)

    d.rectangle([0, 740, W, 860], fill=HDR_BG)
    d.text((60, 775), "종목명", font=F(34), fill=(80, 80, 84))
    rtext(d, 760, 752, "평가손익", F(34), (80, 80, 84))
    rtext(d, 760, 800, "수익률", F(34), (80, 80, 84))
    rtext(d, W - 60, 752, "평가금액", F(34), (80, 80, 84))
    rtext(d, W - 60, 800, "매수금액", F(34), (80, 80, 84))

    y = 860
    for name, cls, value, cost, pnl, qty in rows:
        rh = 205
        lines = wrap2(d, name, F(40), 480)
        if qty is not None:
            lines = lines[:1] + [f"{qty}주"] if len(lines) > 1 else lines
        ny = y + (rh - 52 * (len(lines) + (1 if qty is not None else 0))) // 2
        for ln in lines:
            d.text((60, ny), ln, font=F(40), fill=DARK)
            ny += 52
        if qty is not None:
            d.text((60, ny), f"{qty}주", font=F(34), fill=GRAY_TXT)
        d_c = pnl_color(pnl)
        rtext(d, 760, y + 45, comma(pnl), F(40), d_c)
        rtext(d, 760, y + 105, f"{pnl/cost*100:+.2f}%" if pnl else "0.00%", F(36), d_c)
        rtext(d, W - 60, y + 45, comma(value), F(40), DARK)
        rtext(d, W - 60, y + 105, comma(cost), F(36), GRAY_TXT)
        y += rh
        d.line([0, y, W, y], fill=DIVIDER, width=2)
    if cash is not None:
        d.text((60, y + 55), "현금잔고(예수금)", font=F(40), fill=DARK)
        rtext(d, 760, y + 35, "0", F(40), DARK)
        rtext(d, 760, y + 95, "0.00%", F(36), GRAY_TXT)
        rtext(d, W - 60, y + 35, comma(cash), F(40), DARK)
        rtext(d, W - 60, y + 95, "0", F(36), GRAY_TXT)
        y += 170
        d.line([0, y, W, y], fill=DIVIDER, width=2)

    ky = H - 350
    ks = random.uniform(700, 7500); kd = random.uniform(-60, 60)
    d.text((40, ky), random.choice(["KOSPI", "KOSDAQ"]), font=F(34, True), fill=DARK)
    d.text((240, ky), f"{ks:,.2f}", font=F(34), fill=pnl_color(kd))
    d.text((440, ky), f"{'▲' if kd>=0 else '▼'}{abs(kd):.2f} ({kd/ks*100:+.2f}%)",
           font=F(34), fill=pnl_color(kd))
    rtext(d, W - 40, ky, random.choice(["애프터마켓", "장마감", "장중"]), F(30), GRAY_TXT)  # 지수바(gt 제외 음성)
    d.rectangle([0, H - 280, W, H], fill=NAVY)
    for i, t in enumerate(["홈", "관심종목", "주식현재가", "종합차트", "주식주문", "메뉴"]):
        d.text((30 + i * 178, H - 230), t, font=F(30), fill=(210, 214, 228))

    gt = []
    for name, cls, value, cost, pnl, qty in rows:
        gt.append([broker, acct_short, name, cls, "KRW", qty, None, value, cost, pnl, 1])
    if cash is not None:
        gt.append([broker, acct_short, "현금잔고(예수금)", "현금", "KRW",
                   None, None, cash, 0, 0, 1])
    return im, gt

# ---------- 클래스 F: mPOP 계좌별 잔고 ----------
def render_mpop_accounts():
    broker = random.choice(KR_BROKERS)
    holder = random.choice(HOLDERS)
    n = random.randint(2, 3)
    types = random.sample(ACCT_TYPES, n)
    accounts = []
    for label_full, short in types:
        accno = f"{random.randint(10**9,10**10-1)}-{random.randint(10,99)}"
        if short == "연금저축":
            bracket = "[연금저축 CMA]"
            cls = "현금"
            v = random.choice([random.randint(1, 9), random.randint(10, 99999)])
            pnl = 0
        else:
            bracket = f"[{label_full}]"
            cls = "혼합(주식·채권)"
            v, c, pnl = money_pair()
        accounts.append((accno, bracket, short, cls, v, pnl))

    H = 700 + n * 490 + 400
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 90, W, 210], fill=BLUE_BANNER)
    status_bar(d)
    d.text((40, 118), "<", font=F(48, True), fill="white")
    d.text((110, 122), "종합잔고", font=F(44, True), fill="white")
    for dy in (0, 20, 40):
        d.ellipse([W - 72, 128 + dy, W - 62, 138 + dy], fill="white")

    tabs = [broker, "다른 금융사", "디지털 자산"]
    x = 60
    for i, t in enumerate(tabs):
        a = i == 0
        d.text((x, 250), t, font=F(38, a), fill=DARK if a else (150, 150, 154))
        wpx = d.textlength(t, font=F(38, a))
        if a:
            d.line([x, 320, x + wpx, 320], fill=DARK, width=5)
        x += wpx + 90
    d.line([0, 325, W, 325], fill=DIVIDER, width=2)

    x = 40
    for i, chip in enumerate(["계좌별", "종목별", "상품구성", "현금"]):
        wpx = d.textlength(chip, font=F(34)) + 60
        d.rounded_rectangle([x, 370, x + wpx, 445], 32,
                            fill=(240, 240, 243) if i else (60, 64, 72))
        d.text((x + 30, 385), chip, font=F(34), fill="white" if i == 0 else (90, 90, 94))
        x += wpx + 25

    y = 490
    gt = []
    for accno, bracket, short, cls, v, pnl in accounts:
        d.text((50, y + 25), f"{accno} {bracket} {holder}", font=F(36), fill=(70, 70, 74))
        for dy in (0, 18, 36):
            d.ellipse([W - 60, y + 25 + dy, W - 52, y + 33 + dy], fill=GRAY_TXT)
        xr = W - 120
        rtext(d, xr, y + 90, comma(v), F(64, True), DARK)
        d.text((xr + 10, y + 112), "원", font=F(40), fill=DARK)
        if pnl:
            c = v - pnl
            rtext(d, W - 80, y + 195, f"{signed(pnl)}원({pnl/c*100:+.1f}%)", F(36), pnl_color(pnl))
        else:
            rtext(d, W - 80, y + 195, "0원(0%)", F(36), GRAY_TXT)
        bx = 50
        labels = ["거래내역", "이체", "주식주문"] if short != "퇴직연금" else ["계좌현황", "이체", "주식주문"]
        for bl in labels:
            d.rounded_rectangle([bx, y + 270, bx + 300, y + 360], 10, fill=(240, 240, 243))
            d.text((bx + 85, y + 292), bl, font=F(36), fill=(60, 60, 64))
            bx += 330
        y += 490
        d.rectangle([0, y - 60, W, y - 20], fill=(246, 246, 248))
        gt.append([broker, short, bracket, cls, "KRW", None, None, v,
                   (v - pnl) if pnl else None, pnl, 1])

    ky = H - 350
    ks = random.uniform(700, 7500); kd = random.uniform(-30, 30)
    d.text((40, ky), random.choice(["KOSPI", "KOSDAQ"]), font=F(34, True), fill=DARK)
    d.text((240, ky), f"{ks:,.2f}", font=F(34), fill=pnl_color(kd))
    d.text((440, ky), f"{'▲' if kd>=0 else '▼'}{abs(kd):.2f} ({kd/ks*100:+.2f}%)",
           font=F(34), fill=pnl_color(kd))
    rtext(d, W - 40, ky, random.choice(["애프터마켓", "장마감", "장중"]), F(30), GRAY_TXT)  # 지수바(gt 제외 음성)
    d.rectangle([0, H - 280, W, H], fill=NAVY)
    for i, t in enumerate(["홈", "관심종목", "주식현재가", "종합차트", "주식주문", "메뉴"]):
        d.text((30 + i * 178, H - 230), t, font=F(30), fill=(210, 214, 228))
    return im, gt

# ---------- 데이터셋 조립 ----------
CLASSES = [("mpop_detail", render_mpop_detail), ("fx", render_fx),
           ("smart_krw", render_smart_krw), ("smart_detail", render_smart_detail),
           ("smart_summary", render_smart_summary), ("mpop_accounts", render_mpop_accounts)]

def main():
    outdir, seed = sys.argv[1], int(sys.argv[2])
    n_per = int(sys.argv[3]) if len(sys.argv) > 3 else 400
    random.seed(seed)
    imgdir = os.path.join(outdir, "images")
    os.makedirs(imgdir, exist_ok=True)
    prompt = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  "../portf-rebalancing/eval/harness/prompt4f.txt")).read().strip()
    samples, gts = [], {}
    i = 0
    for cname, fn_render in CLASSES:
        for _ in range(n_per):
            im, gt = fn_render()
            fn = f"synth_{cname}_{i:05d}.jpg"
            downhalf(im).save(os.path.join(imgdir, fn), quality=jpeg_q())
            target = json.dumps(gt, ensure_ascii=False, separators=(", ", ", "))
            samples.append({
                "messages": [
                    {"role": "user", "content": "<image>" + prompt},
                    {"role": "assistant", "content": target}],
                "images": [os.path.abspath(os.path.join(imgdir, fn))]})
            gts[fn] = gt
            i += 1
    random.shuffle(samples)
    with open(os.path.join(outdir, "synth.json"), "w") as f:
        json.dump(samples, f, ensure_ascii=False, indent=1)
    with open(os.path.join(outdir, "gt.json"), "w") as f:
        json.dump(gts, f, ensure_ascii=False, indent=1)
    print(f"OK {i} samples ({n_per}/class) -> {outdir}")

if __name__ == "__main__":
    main()
