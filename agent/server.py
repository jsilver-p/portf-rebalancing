#!/usr/bin/env python3
"""포트폴리오 추출 에이전트 — MVP 로컬 서버.
폰 등 외부에서 스크린샷을 올리면 로컬 Ollama(Qwen2.5-VL-7B + 헤더프롬프트)로
보유자산을 추출하고, 결정적 엔리치(주가=평가금액/수량)·계좌합계 검증을 붙여 JSON으로 돌려준다.

실행:  python3 agent/server.py         (기본 포트 8899, 모델 qwen2.5vl:7b)
환경:  MODEL, PORT, OLLAMA 로 조정.
외부접속: 별도로  cloudflared tunnel --url http://localhost:8899  (public https URL)
주의: 이 맥은 CPU라 이미지당 수 분 소요(정상). Orin GPU에선 초 단위.
"""
import base64, json, os, re, sys, threading, time, urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                     # 형제 모듈 import
import fetch_prices                          # noqa: E402
import resolve                               # noqa: E402

ROOT = os.path.dirname(HERE)
MODEL = os.environ.get("MODEL", "qwen2.5vl:7b")
PORT = int(os.environ.get("PORT", "8899"))
OLLAMA = os.environ.get("OLLAMA", "http://127.0.0.1:11434") + "/api/generate"
PROMPT = open(os.path.join(ROOT, "eval/harness/prompt2.txt")).read().strip()

# 시세: 서버 전용 데이터(레포 밖). 결정론적 페치 — LLM 무관.
DATA_DIR = os.environ.get("DATA_DIR", os.path.expanduser("~/portf-agent/data"))
PRICES_PATH = os.path.join(DATA_DIR, "prices.json")
WATCHLIST_PATH = os.path.join(DATA_DIR, "watchlist.json")
# 마감 후 UTC 시각(EOD): KRX 06:30 마감 +15분, NYSE 20:00~21:00 마감 이후로 안전하게.
FETCH_TIMES_UTC = os.environ.get("FETCH_TIMES_UTC", "06:45,21:30").split(",")

PAGE = """<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>포트폴리오 추출 에이전트 (MVP)</title>
<style>
body{font-family:-apple-system,system-ui,"Apple SD Gothic Neo",sans-serif;margin:0;background:#0f1116;color:#e8eaf0;padding:16px}
h1{font-size:1.15rem;margin:.2rem 0 1rem}
.card{background:#171a21;border:1px solid #262b36;border-radius:12px;padding:16px;margin-bottom:14px}
input[type=file]{width:100%;color:#9aa1b2}
button{width:100%;padding:14px;border:0;border-radius:10px;background:#4f5bd5;color:#fff;font-size:1rem;font-weight:700;margin-top:12px}
button:disabled{opacity:.5}
table{width:100%;border-collapse:collapse;font-size:.82rem;margin-top:10px}
th,td{padding:6px 6px;border-bottom:1px solid #262b36;text-align:right;font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}
th{color:#9aa1b2;font-size:.7rem;text-transform:uppercase}
.muted{color:#9aa1b2;font-size:.8rem}
.warn{color:#e0b45a}
label{display:block;margin-top:12px;color:#9aa1b2;font-size:.8rem}
input[type=date]{width:100%;margin-top:4px;padding:8px;border:1px solid #262b36;border-radius:8px;background:#0f1116;color:#e8eaf0;font-size:.95rem}
.badge{display:inline-block;font-size:.6rem;padding:1px 5px;border-radius:6px;background:#5a4a1e;color:#e0b45a;margin-left:4px;vertical-align:middle}
.badge.low{background:#4a2e2e;color:#e08a8a}
.est{color:#e0b45a}
pre{white-space:pre-wrap;word-break:break-all;font-size:.7rem;color:#9aa1b2}
</style></head><body>
<h1>📸 포트폴리오 추출 에이전트 <span class=muted>MVP</span></h1>
<div class=card>
  <input id=f type=file accept="image/*" capture=environment>
  <label>스크린샷을 캡처한 날짜 (수량 추정 기준)
    <input id=cap type=date>
  </label>
  <button id=go>추출하기</button>
  <div id=status class=muted style="margin-top:10px"></div>
</div>
<div id=out></div>
<script>
const f=document.getElementById('f'),go=document.getElementById('go'),st=document.getElementById('status'),out=document.getElementById('out'),cap=document.getElementById('cap');
cap.value=new Date().toISOString().slice(0,10);   // 기본: 오늘
const esc=s=>String(s).replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
function qtyCell(x){
  if(x.qty==null) return '<span class=muted>—</span>';
  if(x.confidence==='estimated') return '<span class=est>≈'+x.qty+'</span><span class=badge title="'+esc(x.qty_src||'')+'">추정</span>';
  if(x.confidence==='estimated-low') return '<span class=est>≈'+x.qty+'</span><span class="badge low" title="'+esc(x.qty_src||'')+'">추정·낮음</span>';
  return x.qty;
}
go.onclick=async()=>{
  if(!f.files[0]){st.textContent='이미지를 선택하세요';return;}
  go.disabled=true;out.innerHTML='';
  const t0=Date.now();
  const tick=setInterval(()=>{st.textContent='추출 중… '+Math.round((Date.now()-t0)/1000)+'s (이 맥은 CPU라 수 분 걸립니다)';},1000);
  try{
    const b64=await new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(r.result.split(',')[1]);r.onerror=rej;r.readAsDataURL(f.files[0]);});
    const r=await fetch('/extract',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({image:b64,captureDate:cap.value})});
    const j=await r.json();clearInterval(tick);
    if(j.error){st.innerHTML='<span class=warn>오류: '+esc(j.error)+'</span>';go.disabled=false;return;}
    const nEst=j.holdings.filter(x=>x.confidence&&x.confidence.startsWith('estimated')).length;
    st.textContent=j.holdings.length+'개 추출 · '+j.seconds+'s'+(nEst?' · 추정 '+nEst+'건':'');
    let h='<div class=card><table><tr><th>종목</th><th>수량</th><th>주가</th><th>평가금액</th></tr>';
    for(const x of j.holdings){h+=`<tr><td>${esc(x.name||'')}</td><td>${qtyCell(x)}</td><td>${x.price!=null?Number(x.price).toLocaleString():'—'}</td><td>${x.value!=null?Number(x.value).toLocaleString():'—'}</td></tr>`;}
    h+='</table>';
    if(nEst)h+='<div class="muted" style="margin-top:8px">≈ 표시는 캡처일('+esc(j.captureDate)+') 종가로 <b>역산한 추정 수량</b>입니다. 화면에 수량이 없어 시세로 추정했습니다.</div>';
    if(j.warnings&&j.warnings.length)h+='<div class="warn muted" style="margin-top:8px">⚠ '+esc(j.warnings.join(' · '))+'</div>';
    h+='</div>';out.innerHTML=h;
  }catch(e){clearInterval(tick);st.innerHTML='<span class=warn>실패: '+esc(e)+'</span>';}
  go.disabled=false;
};
</script></body></html>"""

def num(x):
    if x is None: return None
    if isinstance(x, (int, float)): return float(x)
    try: return float(re.sub(r"[^\d.\-]", "", str(x)))
    except Exception: return None

def parse_json(raw):
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if m: raw = m.group(1)
    i, j = raw.find("["), raw.rfind("]")
    if i < 0 or j < 0: return None
    try: return json.loads(raw[i:j+1])
    except Exception:
        try: return json.loads(re.sub(r",\s*([\]}])", r"\1", raw[i:j+1]))
        except Exception: return None

RESID_GATE = 0.02   # T4 수량 추정 채택 잔차 상한(정수 근접). KRW는 통상 0.000, USD는 대부분 걸러짐.

def enrich(rows, capture_date):
    """엔리치 사다리(추측한 값은 반드시 confidence/qty_src로 표시 — 사용자 오해 방지):
      T1 화면 수량            → confidence=exact
      T2 수량·평가금액 → 주가   → price_src=computed:value/qty
      T4 수량 없음+평가금액+심볼 → 캡처일 종가로 수량 역산(잔차 게이트) → confidence=estimated(-low)
      복원 실패              → confidence=unreproducible (재평가 불가로 명시)"""
    cache = resolve.load_cache()
    fx_cap = ["unset"]  # 캡처일 USD/KRW (lazy)
    def get_fx():
        if fx_cap[0] == "unset":
            try:
                fx_cap[0] = fetch_prices.history_close("KRW=X", capture_date)[0]
            except Exception:
                fx_cap[0] = None
        return fx_cap[0]

    for h in rows:
        for k in ("qty", "value", "cost", "price"):
            if k in h: h[k] = num(h[k])
        if isinstance(h.get("qty"), float) and h["qty"].is_integer():
            h["qty"] = int(h["qty"])   # 화면 수량은 정수로 표기
        try:
            rec = resolve.resolve(h.get("name"), h.get("currency"), cache)
        except Exception:
            rec = None
        if rec:
            h["symbol"], h["market"] = rec["symbol"], rec["market"]
        # T1
        if h.get("qty") is not None:
            h.setdefault("qty_src", "screen"); h.setdefault("confidence", "exact")
        # T2
        if h.get("price") is None and h.get("qty") and h.get("value"):
            h["price"] = round(h["value"] / h["qty"], 2); h["price_src"] = "computed:value/qty"
        # T4
        if (not h.get("qty")) and h.get("value") and rec:
            usd = h.get("currency") == "USD"
            try:
                close, cday = fetch_prices.history_close(rec["symbol"], capture_date)
            except Exception:
                close, cday = None, None
            denom = (close * get_fx() if usd and get_fx() else (None if usd else close)) if close else None
            if denom:
                rawq = h["value"] / denom
                q = round(rawq); resid = round(abs(rawq - q), 3)
                if q > 0 and resid < RESID_GATE:
                    h["qty"] = q
                    h["qty_src"] = f"추정:캡처일({cday}) 종가 역산" + ("(USD·환율포함)" if usd else "")
                    h["confidence"] = "estimated-low" if usd else "estimated"
                    h["qty_resid"] = resid
                    if h.get("price") is None:
                        h["price"] = round(close, 2); h["price_src"] = f"capture-close:{cday}"
                else:
                    h["confidence"] = "unreproducible"
                    h["qty_note"] = f"수량 추정 실패(잔차 {resid}) — 재평가 불가"
            else:
                h["confidence"] = "unreproducible"
                h["qty_note"] = "캡처일 종가 미취득 — 재평가 불가"
        if not h.get("qty") and not h.get("confidence"):
            h["confidence"] = "unreproducible"
    resolve.save_cache(cache)
    return rows


def update_watchlist(rows):
    """추출된 보유자산의 해석된 심볼을 watchlist에 합집합으로 반영(중복 제거).
    시세 페처가 보유 종목을 자동 추종하게 하는 고리. 실패해도 무시."""
    try:
        wl = json.load(open(WATCHLIST_PATH)) if os.path.exists(WATCHLIST_PATH) else []
        have = {x["symbol"] if isinstance(x, dict) else x for x in wl}
        added = False
        for h in rows:
            s = h.get("symbol")
            if s and s not in have:
                wl.append({"symbol": s, "name": h.get("name")}); have.add(s); added = True
        if added:
            os.makedirs(DATA_DIR, exist_ok=True)
            json.dump(wl, open(WATCHLIST_PATH, "w"), ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"· watchlist 갱신 실패: {e}")


def reprice(holdings, capture_date):
    """앱의 보유자산 → 현재가로 재평가. 심볼 해석·T4(수량 복원)·현재가 합성.
    반환: {fx, asOf, holdings:[... value=수량×현재가×(환율 if USD)]}."""
    rows = enrich(holdings, capture_date)      # symbol + qty(T4) + confidence
    update_watchlist(rows)
    syms = list({h["symbol"] for h in rows if h.get("symbol")})
    pdata = fetch_prices.build(syms) if syms else {"fx": {"USDKRW": None}, "prices": {}, "asOf": None}
    fx = pdata["fx"].get("USDKRW")
    for h in rows:
        s = h.get("symbol")
        pr = pdata["prices"].get(s) if s else None
        if pr and pr.get("price") is not None and h.get("qty"):
            cur = pr.get("currency") or h.get("currency")
            price = pr["price"]
            h["price"] = price                 # 네이티브 통화(.KS=KRW, US=USD)
            h["currency"] = cur
            h["value"] = round(price * h["qty"], 2)   # 네이티브 — KRW 환산은 앱이 fx로 수행
            h["stale"] = pr.get("stale")
            h["value_src"] = "reprice:qty*price@current"
        else:
            h["value_src"] = "kept"  # 현금·미해석·수량없음 → 재평가 불가(기존 값 유지)
    return {"fx": fx, "asOf": pdata.get("asOf"), "holdings": rows}


def extract(b64, capture_date):
    body = json.dumps({"model": MODEL, "prompt": PROMPT, "images": [b64],
                       "stream": False, "options": {"temperature": 0, "num_ctx": 8192}}).encode()
    t0 = time.time()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        out = json.loads(r.read())
    raw = out.get("response", "")
    rows = parse_json(raw) or []
    warnings = []
    if not rows:
        warnings.append("JSON 파싱 실패 — 원문 확인 필요")
    rows = enrich(rows, capture_date)
    update_watchlist(rows)
    return {"holdings": rows, "seconds": round(time.time() - t0, 1),
            "warnings": warnings, "model": MODEL, "raw": raw, "captureDate": capture_date}

# ── 시세 페치 (결정론적, LLM 무관) ─────────────────────────────
def refresh_prices():
    """watchlist.json → Yahoo → prices.json. 실패해도 서버는 계속 돈다."""
    try:
        syms = fetch_prices.load_watchlist(WATCHLIST_PATH) if os.path.exists(WATCHLIST_PATH) else []
        if not syms:
            return
        result = fetch_prices.build(syms)
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = PRICES_PATH + ".tmp"
        json.dump(result, open(tmp, "w"), ensure_ascii=False, indent=2)
        os.replace(tmp, PRICES_PATH)  # 원자적 교체 — 반쯤 쓰인 파일 서빙 방지
        print(f"· 시세 갱신: {len(result['prices'])} OK, "
              f"{len(result.get('errors', {}))} 실패, USDKRW={result['fx']['USDKRW']}")
    except Exception as e:
        print(f"· 시세 갱신 실패: {e}")

def _seconds_until_next(now=None):
    now = now or datetime.now(timezone.utc)
    best = None
    for hm in FETCH_TIMES_UTC:
        try:
            h, m = (int(x) for x in hm.strip().split(":"))
        except Exception:
            continue
        tgt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if tgt <= now:
            tgt = tgt.timestamp() + 86400
        else:
            tgt = tgt.timestamp()
        best = tgt if best is None else min(best, tgt)
    return max(60, int((best or now.timestamp() + 86400) - now.timestamp()))

def scheduler():
    """부팅 시 1회 갱신 후, 지정 UTC 시각마다 갱신. EOD(마감 후) 시세를 받기 위함."""
    if not os.path.exists(PRICES_PATH):
        refresh_prices()  # 최초 부팅: 파일 없으면 즉시 채움
    while True:
        time.sleep(_seconds_until_next())
        refresh_prices()

class H(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()
    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            b = PAGE.encode(); self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8"); self._cors()
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        elif self.path == "/health":
            self.send_response(200); self._cors(); self.end_headers(); self.wfile.write(b'{"ok":true}')
        elif self.path.split("?")[0] == "/prices":
            # 서버가 저장한 시세 파일을 그대로 서빙(정적). 에이전트(LLM) 무관.
            if os.path.exists(PRICES_PATH):
                b = open(PRICES_PATH, "rb").read(); code = 200
            else:
                b = b'{"error":"prices.json not ready"}'; code = 503
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8"); self._cors()
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        else:
            self.send_response(404); self._cors(); self.end_headers()
    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/extract", "/reprice"):
            self.send_response(404); self._cors(); self.end_headers(); return
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n))
            cap = data.get("captureDate") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if path == "/reprice":
                result = reprice(data.get("holdings", []), cap)
            else:
                result = extract(data["image"], cap)
        except Exception as e:
            result = {"error": str(e)}
        b = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8"); self._cors()
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass

if __name__ == "__main__":
    print(f"에이전트 서버 → http://0.0.0.0:{PORT}  (모델 {MODEL})")
    print(f"· 시세 데이터 {DATA_DIR}  갱신시각(UTC) {FETCH_TIMES_UTC}")
    threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
