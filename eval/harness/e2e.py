#!/usr/bin/env python3
"""실연결 E2E — 앱(브라우저) → 에이전트 서버 → 비전추출·게이트·엔리치 → 표 렌더까지 통과시킨다.

오프라인 파리티(parity.py)는 finalize+enrich까지의 점수일 뿐이다. 사용자가 실제로 타는 경로는
브라우저의 파일 업로드 → /extract/batch → 앱 상태(localStorage) → 표·출처칩이다. 그 사이에서
값이 유실·변형되지 않는지(예: USD 행에 환율이 두 번 곱해지는지) 확인해야 진짜 '연결'이다.

사전 조건: 서버(:8899)와 정적 서버(:8000)가 떠 있어야 한다. run_e2e.sh가 함께 띄운다.
검증: 앱 저장 상태(pf_rebalancer_v1) ↔ 정답표(31종목) 대조 + 출처칩 표시 + 스크린샷 증거.
"""
import json, os, re, sys, time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

# aarch64(Jetson)에선 Selenium Manager가 드라이버를 못 받는다 → 설치된 geckodriver를 직접 지정.
GECKO = os.environ.get("GECKODRIVER", "/snap/bin/geckodriver")
FIREFOX = os.environ.get("FIREFOX_BIN", "/usr/bin/firefox")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHOTS = os.path.join(ROOT, "test-fixtures", "screenshots")
APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8000/index.html")
AGENT = os.environ.get("AGENT_URL", "http://127.0.0.1:8899")
OUT = os.environ.get("E2E_OUT", "/tmp/e2e")


def main():
    os.makedirs(OUT, exist_ok=True)
    opts = Options()
    opts.add_argument("-headless")
    opts.binary_location = FIREFOX
    opts.set_preference("dom.webnotifications.enabled", False)
    d = webdriver.Firefox(options=opts, service=Service(executable_path=GECKO))
    d.set_window_size(1280, 2200)
    try:
        d.get(APP_URL)
        time.sleep(3)
        # 앱 좌하단 '에이전트 연결'과 동일한 계약: localStorage에 터널/로컬 URL을 넣는다.
        # 시작 상태는 실사용 첫 방문을 흉내낸다:
        #  · '직접입력' 센티넬 1건 — 스냅샷 병합이 보존해야 하는 사용자 자산(빈 배열로 두면
        #    앱이 데모 시드 13건을 채워 추출 결과와 뒤섞여 거짓 통과를 만든다)
        #  · 데모 시드 꼴 1건(id 's1') — 시드 계좌는 실계좌와 스코프가 안 겹쳐 스냅샷 규칙만으론
        #    살아남았다(추출 결과가 기존 표 아래 덧붙는 버그). 청소되는지를 여기서 상시 검증한다.
        d.execute_script("""
          localStorage.setItem('pf_agent_url', arguments[0]);
          localStorage.setItem('pf_rebalancer_v1', JSON.stringify({
            holdings: [{id:'e2e0', broker:'직접입력', accountType:'일반', name:'__E2E__',
                        cls:'cash', currency:'KRW', qty:null, price:null, value:1, cost:null,
                        updatedAt: Date.now()},
                       {id:'s1', broker:'토스증권', accountType:'일반', name:'AAPL 애플',
                        cls:'growth', currency:'USD', qty:12, price:212.5, value:null, cost:2280,
                        updatedAt: Date.now()}],
            target: null, fx: 1509.9 }));""", AGENT)
        d.get(APP_URL)
        time.sleep(3)
        n0 = len(json.loads(d.execute_script(
            "return localStorage.getItem('pf_rebalancer_v1');")).get("holdings", []))
        print(f"· 앱 로드 완료 — 에이전트 {AGENT} · 시작 보유자산 {n0}건(센티넬+데모시드)")

        imgs = sorted(os.path.join(SHOTS, f) for f in os.listdir(SHOTS)
                      if f.lower().endswith((".jpg", ".png")))
        inp = d.find_element(By.CSS_SELECTOR, "input[type=file]")
        d.execute_script("arguments[0].style.display='block';arguments[0].style.opacity=1;", inp)
        inp.send_keys("\n".join(imgs))                 # 실제 업로드 경로(사람이 파일 고르는 것과 동일)
        print(f"· 스크린샷 {len(imgs)}장 투입 — 서버 종합 분석 대기")

        holdings, waited = [], 0
        while waited < 1500:
            time.sleep(5); waited += 5
            state = d.execute_script("return localStorage.getItem('pf_rebalancer_v1');")
            rows = json.loads(state or "{}").get("holdings") or []
            holdings = [h for h in rows if h.get("name") != "__E2E__"          # 센티넬 제외
                        and not re.fullmatch(r"s\d+", str(h.get("id", "")))]   # 시드 제외(추출 도착 판정용)
            if holdings:
                break
            err = d.execute_script(
                "const m=document.body.innerText.match(/(추출에 실패|에이전트 추출 실패)[^\\n]*/);return m?m[0]:'';")
            if err:
                raise SystemExit(f"❌ 앱이 오류를 표시: {err}")
            if waited % 60 == 0:
                st = d.execute_script(
                    "const e=document.body.innerText.match(/화면 \\d+장 종합 분석 중[^\\n]*/);return e?e[0]:'';")
                print(f"   … {waited}s {st}")
        if not holdings:
            raise SystemExit("❌ 시간 초과 — 추출이 앱까지 도달하지 못함")
        # 스냅샷 계약 검증: 데모 시드(id s\d+)는 청소되고, 직접입력 자산은 보존돼야 한다.
        leftover = [h for h in rows if re.fullmatch(r"s\d+", str(h.get("id", "")))]
        if leftover:
            raise SystemExit(f"❌ 데모 시드가 추출 후에도 남음(표 아래 덧붙음 버그): "
                             f"{[h['name'] for h in leftover]}")
        if not any(h.get("name") == "__E2E__" for h in rows):
            raise SystemExit("❌ 직접입력 자산(센티넬)이 스냅샷 병합에서 유실됨")
        print("· 데모 시드 청소 ✅ · 직접입력 보존 ✅")
        d.save_screenshot(os.path.join(OUT, "app.png"))
        print(f"· 앱 표에 반영된 종목 {len(holdings)}개 (스크린샷 → {OUT}/app.png)")
        return holdings, d
    except Exception:
        d.save_screenshot(os.path.join(OUT, "error.png"))
        raise
    finally:
        pass


def verify(holdings):
    gt = json.load(open(os.path.join(ROOT, "test-fixtures", "ground-truth.json")))
    sys.path.insert(0, os.path.join(ROOT, "eval", "harness"))
    from parity import norm                      # 동일 정규화 규칙 재사용
    fx = gt["fx_usd_krw"]
    # 같은 종목이 여러 계좌에 있다(ACE KRX금현물 = 메리츠·ISA·IRP) → 이름만으로 매칭하면 안 된다.
    # 보유자산의 정체는 (증권사·계좌·종목)이고, 이름 표기가 달라도 (계좌·금액)이면 같은 자산이다.
    gleft = list(gt["holdings"])
    ok, bad = 0, []
    for h in holdings:
        key = (h.get("broker"), h.get("accountType"))
        g = next((x for x in gleft if (x["broker"], x["accountType"]) == key
                  and norm(x["name"]) == norm(h.get("name"))), None)
        if not g:                                   # 이름 표기 차이 → 계좌+금액으로 식별
            v0 = (h.get("value") or 0) * (fx if h.get("currency") == "USD" else 1)
            g = next((x for x in gleft if (x["broker"], x["accountType"]) == key and x["value"]
                      and abs(v0 - x["value"]) / x["value"] < 0.01), None)
        if not g:
            bad.append(f"GT에 없는 종목: {h.get('name')} ({key})"); continue
        gleft.remove(g)
        # 앱의 표시 공식 그대로 비교한다: qty×price가 value보다 우선(krw() 계약)이고 USD는 fx 환산.
        # value만 대조하면 '저장값은 맞는데 표는 틀린' 버그(현재가 오독 등)를 놓친다 — 실제로 두 번 놓쳤다.
        conv = fx if h.get("currency") == "USD" else 1
        native = (h["qty"] * h["price"] if h.get("qty") is not None and h.get("price") is not None
                  else (h.get("value") or 0))
        v = native * conv
        if g["value"] and abs(v - g["value"]) / g["value"] > 0.01:
            bad.append(f"{h['name']}: 앱 {v:,.0f} vs 정답 {g['value']:,.0f}")
        else:
            ok += 1
    chips = sum(1 for h in holdings if h.get("qty_src") or h.get("value_src"))
    est = sum(1 for h in holdings if "derived" in str(h.get("qty_src")))
    print(f"\n=== E2E 검증")
    print(f"  종목 수      {len(holdings)}/{len(gt['holdings'])} " +
          ("✅" if len(holdings) == len(gt["holdings"]) else "❌"))
    print(f"  평가금액 일치 {ok}/{len(holdings)} " + ("✅" if not bad else "❌"))
    print(f"  출처칩 보유   {chips}/{len(holdings)} (그중 추정 수량 {est}건) " +
          ("✅" if chips == len(holdings) else "❌"))
    for b in bad[:6]:
        print("   ❌ " + b)
    return not bad and len(holdings) == len(gt["holdings"]) and chips == len(holdings)


if __name__ == "__main__":
    holdings, drv = main()
    good = verify(holdings)
    json.dump(holdings, open(os.path.join(OUT, "app_holdings.json"), "w"),
              ensure_ascii=False, indent=2)
    drv.quit()
    print("\n판정:", "PASS ✅" if good else "FAIL ❌")
    sys.exit(0 if good else 1)
