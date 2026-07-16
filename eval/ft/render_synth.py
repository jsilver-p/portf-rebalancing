#!/usr/bin/env python3
"""합성 학습 데이터 렌더러 — 실패 화면 2종 (mPOP 종합잔고 병합셀표, SMART 외화예수금).

실화면(민감정보)은 기하·팔레트 참조로만 씀. 여기서 나가는 픽셀·값은 전부 가짜.
출력: 1080x2340 렌더 → LANCZOS ×0.5 28px 스냅(532x1176) JPEG — 실화면 평가 파이프라인과 동일 처리.
정답: prompt4e 규칙을 그대로 따르는 11칸 JSON 배열 (자동투자 중복·잔액0 증거금 제외).

사용: python3 render_synth.py <n_mpop> <n_fx> <outdir> [seed]
산출: <outdir>/images/*.jpg, <outdir>/synth.json (LLaMA-Factory sharegpt+images),
      <outdir>/gt.json (자체 검증용)
"""
import json, os, random, sys
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 2340
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

def snap28(v):
    return max(28, round(v / 28) * 28)

def downhalf(im):
    return im.resize((snap28(W * 0.5), snap28(H * 0.5)), Image.LANCZOS)

def status_bar(d):
    d.text((40, 22), f"{random.randint(1,12)}:{random.randint(0,59):02d}", font=F(34), fill=DARK)
    d.text((W - 200, 22), f"5G {random.randint(15,99)}%", font=F(30), fill=DARK)

# ---------- 종목명 풀 (규칙6 키워드 → assetClass 확정) ----------
GROWTH = ["미국나스닥100", "미국S&P500", "미국테크TOP10", "반도체TOP10", "2차전지산업",
          "차이나휴머노이드로봇", "K방산산업", "AI코리아액티브", "미국성장기업", "글로벌테크액티브"]
DIVID = ["고배당주", "은행고배당플러스", "미국배당다우존스", "고배당커버드콜"]
BOND = ["국고채10년", "미국채30년액티브", "종합채권액티브", "단기채권플러스"]
GOLD = ["KRX금현물", "골드선물(H)", "원자재선물특별자산"]
REIT = ["리츠부동산인프라", "미국리츠부동산"]
MIX = ["채권혼합밸런스", "TDF2045채권혼합"]
STOCKS = ["한빛중공업", "대양전자", "미래바이오텍", "한류엔터", "동방물산", "청담제약", "신라정밀"]
PREFIX = ["TIGER", "ACE", "KODEX", "PLUS", "SOL", "RISE"]

def pick_holding():
    r = random.random()
    if r < 0.25:
        return random.choice(STOCKS), "성장주"
    theme, cls = random.choice(
        [(GROWTH, "성장주"), (DIVID, "배당주"), (BOND, "채권"),
         (GOLD, "금·원자재"), (REIT, "부동산"), (MIX, "혼합(주식·채권)")])
    return f"{random.choice(PREFIX)} {random.choice(theme)}", cls

ACCT_TYPES = [("ISA(평생혜택 중개형)(비대면)", "ISA"), ("연금저축계좌", "연금저축"),
              ("퇴직연금(다이렉트IRP)(비대면)", "퇴직연금"), ("종합매매(비대면)", "일반")]

def wrap2(d, text, font, maxw):
    if d.textlength(text, font=font) <= maxw:
        return [text]
    for i in range(len(text) - 1, 0, -1):
        if d.textlength(text[:i], font=font) <= maxw:
            return [text[:i], text[i:]]
    return [text]

def rtext(d, xr, y, s, font, fill):
    d.text((xr - d.textlength(s, font=font), y), s, font=font, fill=fill)

# ---------- 화면 1: mPOP 종합잔고 (병합셀 표) ----------
def render_mpop():
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    status_bar(d)
    d.text((40, 110), "<", font=F(52, True), fill=DARK)
    d.text((110, 112), "종합잔고", font=F(48, True), fill=DARK)
    for dy in (0, 22, 44):  # ⋮
        d.ellipse([W - 76, 118 + dy, W - 64, 130 + dy], fill=DARK)

    label_full, acct_short = random.choice(ACCT_TYPES)
    accno = f"{random.randint(10**9,10**10-1)}-{random.randint(10,99)}"
    acct_label = f"{accno} [{label_full}]"
    d.text((60, 255), acct_label, font=F(36), fill=(70, 70, 74))

    # 표 행 값 생성 (산술 일관)
    n = random.randint(2, 4)
    rows, gt = [], []
    for _ in range(n):
        name, cls = pick_holding()
        cost = random.randint(30, 45000) * 10
        pnl = int(cost * random.uniform(-0.45, 0.45) / 10) * 10
        value = cost + pnl
        pct = pnl / cost * 100
        rows.append((name, cls, value, cost, pnl, pct))
    cash = None
    if random.random() < 0.75:
        cash = random.choice([random.randint(1, 9), random.randint(10, 99999)])

    total_v = sum(r[2] for r in rows) + (cash or 0)
    total_c = sum(r[3] for r in rows)
    total_p = sum(r[4] for r in rows)

    # 파란 배너
    d.rectangle([0, 330, W, 560], fill=BLUE_BANNER)
    d.text((60, 380), "자산", font=F(44, True), fill="white")
    big = comma(total_v)
    xr = W - 150
    rtext(d, xr, 365, big, F(72, True), "white")
    d.text((xr + 12, 392), "원", font=F(44), fill="white")
    sub = f"{comma(total_p)}원({total_p/total_c*100:+.1f}%)".replace("+", "" if total_p < 0 else "+")
    rtext(d, W - 80, 470, sub, F(38), (225, 235, 250))

    d.text((60, 615), "예금자보호안내", font=F(32), fill=GRAY_TXT)
    d.ellipse([310, 618, 344, 652], outline=GRAY_TXT, width=2)
    d.text((321, 618), "i", font=F(26), fill=GRAY_TXT)
    for k in range(3):  # 목록 아이콘 3개 근사
        x0 = W - 230 + k * 70
        d.rectangle([x0, 618, x0 + 40, 652], outline=(90, 110, 200), width=3)

    # 표 헤더 (병합 2줄)
    d.rectangle([0, 740, W, 860], fill=HDR_BG)
    d.text((60, 775), "종목명", font=F(34), fill=(80, 80, 84))
    rtext(d, 760, 752, "평가손익", F(34), (80, 80, 84))
    rtext(d, 760, 800, "수익률", F(34), (80, 80, 84))
    rtext(d, W - 60, 752, "평가금액", F(34), (80, 80, 84))
    rtext(d, W - 60, 800, "매수금액", F(34), (80, 80, 84))

    y = 860
    for name, cls, value, cost, pnl, pct in rows:
        rh = 200
        lines = wrap2(d, name, F(40), 480)
        ny = y + (rh - 52 * len(lines)) // 2
        for ln in lines:
            d.text((60, ny), ln, font=F(40), fill=DARK)
            ny += 52
        color = PNL_RED if pnl > 0 else PNL_BLUE if pnl < 0 else DARK
        rtext(d, 760, y + 45, comma(pnl), F(40), color)
        rtext(d, 760, y + 105, f"{pct:+.2f}%".replace("+", "" if pnl < 0 else "+"), F(36), color)
        rtext(d, W - 60, y + 45, comma(value), F(40), DARK)
        rtext(d, W - 60, y + 105, comma(cost), F(36), GRAY_TXT)
        y += rh
        d.line([0, y, W, y], fill=DIVIDER, width=2)
    if cash is not None:
        rh = 170
        d.text((60, y + 55), "현금잔고(예수금)", font=F(40), fill=DARK)
        rtext(d, 760, y + 35, "0", F(40), DARK)
        rtext(d, 760, y + 95, "0.00%", F(36), GRAY_TXT)
        rtext(d, W - 60, y + 35, comma(cash), F(40), DARK)
        rtext(d, W - 60, y + 95, "0", F(36), GRAY_TXT)
        y += rh
        d.line([0, y, W, y], fill=DIVIDER, width=2)

    # 하단 KOSPI + 내비
    ks = random.uniform(2400, 7500)
    kd = random.uniform(-60, 60)
    d.text((40, 1985), "KOSPI", font=F(34, True), fill=DARK)
    d.text((200, 1985), f"{ks:,.2f}", font=F(34), fill=PNL_RED if kd >= 0 else PNL_BLUE)
    arrow = "▲" if kd >= 0 else "▼"
    d.text((400, 1985), f"{arrow}{abs(kd):.2f} ({kd/ks*100:+.2f}%)".replace("+-", "-"),
           font=F(34), fill=PNL_RED if kd >= 0 else PNL_BLUE)
    d.rectangle([0, 2060, W, H], fill=NAVY)
    for i, t in enumerate(["홈", "관심종목", "주식현재가", "종합차트", "주식주문", "메뉴"]):
        d.text((30 + i * 178, 2110), t, font=F(30), fill=(210, 214, 228))

    for name, cls, value, cost, pnl, pct in rows:
        gt.append([acct_label, acct_short, name, cls, "KRW", None, None, value, cost, pnl, 1])
    if cash is not None:
        gt.append([acct_label, acct_short, "현금잔고(예수금)", "현금", "KRW", None, None, cash, 0, 0, 1])
    return im, gt

# ---------- 화면 2: SMART 외화예수금 ----------
BRANDS = ["Super365", "Prime100", "Star247", "Hero365", "Plus100"]

def render_fx():
    im = Image.new("RGB", (W, H), SMART_BG)
    d = ImageDraw.Draw(im)
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

    tabs = ["국내주식", "해외주식", "원화예수금", "외화예수금"]
    xs = [30, 290, 550, 810]
    for t, x in zip(tabs, xs):
        active = t == "외화예수금"
        d.text((x, 395), t, font=F(38, active), fill=DARK if active else (165, 165, 170))
        if active:
            wpx = d.textlength(t, font=F(38, True))
            d.line([x, 465, x + wpx, 465], fill=DARK, width=5)

    usd = round(random.uniform(3, 60000), 2)
    fxr = round(random.uniform(1150, 1650), 2)
    krw = int(usd * fxr)

    d.rounded_rectangle([30, 510, W - 30, 950], 24, fill="white")
    d.text((80, 555), "평가금액", font=F(36), fill=GRAY_TXT)
    rtext(d, W - 90, 555, f"26.{random.randint(1,12):02d}.{random.randint(1,28):02d}", F(34), GRAY_TXT)
    d.text((80, 650), f"{comma(krw)}원", font=F(80, True), fill=DARK)
    d.rounded_rectangle([560, 840, W - 80, 915], 14, fill=(252, 243, 212))
    d.text((600, 858), "자동투자 수익 보기 ›", font=F(34), fill=(120, 95, 30))

    d.rectangle([0, 1000, W, H], fill="white")
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

    has_auto = random.random() < 0.7
    y = 1300
    if has_auto:
        d.text((60, y + 40), "자동투자", font=F(40), fill=DARK)
        rtext(d, W - 60, y + 40, f"{usd:,.2f} USD", F(40), DARK)
        y += 150
        d.line([0, y, W, y], fill=DIVIDER, width=2)

    if random.random() < 0.8:  # 기타: 잔액 0 증거금 (target 제외 대상)
        d.rectangle([0, 1950, W, 2020], fill=(238, 238, 240))
        d.text((60, 1965), "기타", font=F(34, True), fill=(60, 90, 180))
        d.text((60, 2050), "외화증거금", font=F(38), fill=DARK)
        d.text((60, 2115), f"환율 {fxr:,.2f}", font=F(32), fill=GRAY_TXT)
        rtext(d, W - 60, 2045, "0.00 USD", F(38), DARK)
        rtext(d, W - 60, 2110, "0원", F(34), GRAY_TXT)

    gt = [[brand, None, "미국 달러", "현금", "USD", usd, None, krw, None, None, 1]]
    return im, gt

# ---------- 데이터셋 조립 ----------
def main():
    n_mpop, n_fx, outdir = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    random.seed(int(sys.argv[4]) if len(sys.argv) > 4 else 20260716)
    imgdir = os.path.join(outdir, "images")
    os.makedirs(imgdir, exist_ok=True)
    prompt = open(os.path.join(os.path.dirname(__file__),
                  "../portf-rebalancing/eval/harness/prompt4e.txt")).read().strip()
    samples, gts = [], {}
    for i in range(n_mpop + n_fx):
        im, gt = (render_mpop if i < n_mpop else render_fx)()
        fn = f"synth_{i:04d}.jpg"
        downhalf(im).save(os.path.join(imgdir, fn), quality=92)
        target = json.dumps(gt, ensure_ascii=False, separators=(", ", ", "))
        samples.append({
            "messages": [
                {"role": "user", "content": "<image>" + prompt},
                {"role": "assistant", "content": target},
            ],
            "images": [os.path.abspath(os.path.join(imgdir, fn))],
        })
        gts[fn] = gt
    with open(os.path.join(outdir, "synth.json"), "w") as f:
        json.dump(samples, f, ensure_ascii=False, indent=1)
    with open(os.path.join(outdir, "gt.json"), "w") as f:
        json.dump(gts, f, ensure_ascii=False, indent=1)
    print(f"OK {n_mpop}+{n_fx} -> {outdir}")

if __name__ == "__main__":
    main()
