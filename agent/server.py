#!/usr/bin/env python3
"""포트폴리오 추출 에이전트 — MVP 로컬 서버.
폰 등 외부에서 스크린샷을 올리면 로컬 Ollama(Qwen2.5-VL-7B + 헤더프롬프트)로
보유자산을 추출하고, 결정적 엔리치(주가=평가금액/수량)·계좌합계 검증을 붙여 JSON으로 돌려준다.

실행:  python3 agent/server.py         (기본 포트 8899, 모델 qwen2.5vl:7b)
환경:  MODEL, PORT, OLLAMA 로 조정.
외부접속: 별도로  cloudflared tunnel --url http://localhost:8899  (public https URL)
주의: 이 맥은 CPU라 이미지당 수 분 소요(정상). Orin GPU에선 초 단위.
"""
import base64, json, os, re, sys, threading, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                     # 형제 모듈 import
import fetch_prices                          # noqa: E402
import resolve                               # noqa: E402
import finalize as finalize_mod              # noqa: E402  종합(게이트·broker 정규화)

ROOT = os.path.dirname(HERE)
MODEL = os.environ.get("MODEL", "qwen2.5vl:7b-ft2-q4")
PORT = int(os.environ.get("PORT", "8899"))
OLLAMA = os.environ.get("OLLAMA", "http://127.0.0.1:11434") + "/api/generate"
NP = int(os.environ.get("NP", "2"))            # 동시 비전 요청 수 — ollama의 OLLAMA_NUM_PARALLEL과 일치시킬 것
PROMPT_FILE = os.environ.get("PROMPT_FILE", os.path.join(ROOT, "eval/harness/prompt4e.txt"))
PROMPT = open(PROMPT_FILE).read().strip()      # prompt4e = prompt4c + 외화예수금 규칙(8) — DECISION v2.3/v2.5 검증본

# 시세: 서버 전용 데이터(레포 밖). 결정론적 페치 — LLM 무관.
DATA_DIR = os.environ.get("DATA_DIR", os.path.expanduser("~/portf-agent/data"))
PRICES_PATH = os.path.join(DATA_DIR, "prices.json")
WATCHLIST_PATH = os.path.join(DATA_DIR, "watchlist.json")
LAST_CAPTURE_PATH = os.path.join(DATA_DIR, "last_capture.json")
# 마감 후 UTC 시각(EOD): KRX 06:30 마감 +15분, NYSE 20:00~21:00 마감 이후로 안전하게.
FETCH_TIMES_UTC = os.environ.get("FETCH_TIMES_UTC", "06:45,21:30").split(",")
# EXIF DateTimeOriginal에 tz가 없다 — 기기 로컬(대개 KST=UTC+9)로 간주. zoneinfo 없는 3.8 호환.
KST = timezone(timedelta(hours=int(os.environ.get("CAPTURE_UTC_OFFSET", "9"))))

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
    """비전 원문 → 행 리스트. 파서는 finalize 하나만 쓴다(단일 출처) — 단건·배치가 같은 견고성을 갖도록."""
    return finalize_mod.parse_rows(raw) or None

def resample_half_b64(b64):
    """×0.5 LANCZOS + 28px 스냅 리샘플 — DECISION v2.5 채택 구성(모델이 이 분포로 학습됨).
    이미지 토큰 ~1/4. PNG 무손실 재인코딩(검증 픽스처와 동일 조건). EXIF는 원본 b64에서
    따로 읽으므로(호출부 보장) 여기서 소실돼도 무관. 실패 시 원본 그대로 반환."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
        w = max(28, round(im.width * 0.5 / 28) * 28)
        h = max(28, round(im.height * 0.5 / 28) * 28)
        buf = io.BytesIO()
        im.resize((w, h), Image.LANCZOS).save(buf, "PNG")
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return b64


def exif_capture_dt(b64):
    """base64 이미지의 EXIF DateTimeOriginal → tz-aware datetime(CAPTURE_TZ) 또는 None.
    스크린샷(안드로이드 등)은 대개 이 태그를 남긴다 — 기기 로컬 시각이라 CAPTURE_TZ로 간주."""
    try:
        import io
        from PIL import Image, ExifTags
        ex = Image.open(io.BytesIO(base64.b64decode(b64))).getexif()
        val = None
        for k, v in ex.items():
            if ExifTags.TAGS.get(k) == "DateTime":
                val = v
        try:
            for k, v in ex.get_ifd(0x8769).items():
                if ExifTags.TAGS.get(k) in ("DateTimeOriginal", "DateTimeDigitized"):
                    val = v or val
        except Exception:
            pass
        if not val:
            return None
        return datetime.strptime(str(val), "%Y:%m:%d %H:%M:%S").replace(tzinfo=KST)
    except Exception:
        return None


def store_capture(dt):
    """최신 캡처 시각을 저장(추출 시 여러 장 중 가장 늦은 것 유지)."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        prev = None
        try:
            prev = datetime.fromisoformat(json.load(open(LAST_CAPTURE_PATH))["datetime"])
        except Exception:
            pass
        if prev is None or dt > prev:
            json.dump({"datetime": dt.isoformat(), "source": "exif"}, open(LAST_CAPTURE_PATH, "w"))
    except Exception:
        pass


def parse_capture(data):
    """캡처 datetime 결정: 요청 captureDateTime > 저장된 EXIF > captureDate(그날 15:30 KST) > now."""
    s = data.get("captureDateTime")
    if s:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            pass
    try:
        return datetime.fromisoformat(json.load(open(LAST_CAPTURE_PATH))["datetime"])
    except Exception:
        pass
    d = data.get("captureDate")
    if d:
        try:
            y, m, dd = map(int, d.split("-"))
            return datetime(y, m, dd, 15, 30, tzinfo=KST)
        except Exception:
            pass
    return datetime.now(KST)


def complete(body):
    """Anthropic messages 형식 → 로컬 모델 → Anthropic 형식 응답으로 프록시.
    앱의 api.anthropic.com 호출을 그대로 받아 처리(키 불필요). 이미지가 있으면 prompt2(정확 추출),
    없으면(재분류 등) 주어진 텍스트를 프롬프트로. 이미지의 EXIF 캡처시각은 저장해 재평가 기준으로 쓴다."""
    msgs = body.get("messages", []) if isinstance(body, dict) else []
    images, texts = [], []
    for m in msgs:
        c = m.get("content")
        if isinstance(c, str):
            texts.append(c)
        elif isinstance(c, list):
            for part in c:
                if part.get("type") == "image":
                    d = (part.get("source") or {}).get("data")
                    if d:
                        images.append(d)
                elif part.get("type") == "text":
                    texts.append(part.get("text", ""))
    for b in images:
        dt = exif_capture_dt(b)
        if dt:
            store_capture(dt)
    prompt = PROMPT if images else "\n".join(texts)
    images = [resample_half_b64(b) for b in images]
    req = json.dumps({"model": MODEL, "prompt": prompt, "images": images,
                      "stream": False, "keep_alive": -1, "options": {"temperature": 0, "num_ctx": 8192}}).encode()
    r = urllib.request.Request(OLLAMA, data=req, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=1800) as resp:
        out = json.loads(resp.read())
    return {"content": [{"type": "text", "text": out.get("response", "")}]}


# 비동기 잡: CPU 모델 추출은 수 분 걸려 100초 제한 퀵터널에서 단일 요청이 끊긴다.
# submit(즉시 id 반환) → 백그라운드 워커 → 짧은 result 폴링으로 쪼갠다. 폴링은 터널이
# 잠깐 끊겨도 재시도로 회복된다(서버 잡은 계속 진행).
_JOBS = {}                        # id -> {status:pending|done|error, content|error, ts}
_JOBS_LOCK = threading.Lock()

def _job_gc():
    now = time.time()
    with _JOBS_LOCK:
        for k in [k for k, v in _JOBS.items() if now - v.get("ts", now) > 1800]:
            _JOBS.pop(k, None)

def _job_run(jid, body):
    try:
        res = complete(body)
        with _JOBS_LOCK:
            _JOBS[jid] = {"status": "done", "content": res["content"], "ts": time.time()}
    except Exception as e:
        with _JOBS_LOCK:
            _JOBS[jid] = {"status": "error", "error": str(e), "ts": time.time()}

def submit_complete(body):
    jid = os.urandom(8).hex()
    with _JOBS_LOCK:
        _JOBS[jid] = {"status": "pending", "ts": time.time()}
    threading.Thread(target=_job_run, args=(jid, body), daemon=True).start()
    _job_gc()
    return {"id": jid}

def job_result(jid):
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
    if not j:
        return {"status": "unknown"}         # 서버 재시작 등으로 잡 소실
    if j["status"] == "done":
        return {"status": "done", "content": j["content"]}
    if j["status"] == "error":
        return {"status": "error", "error": j.get("error", "오류")}
    return {"status": "pending"}


# T4 노이즈 전파 게이트: 수량 오차 = 주식수 × δ(기준가 상대오차). 반올림이 노이즈에도
# 안 뒤집혀야 채택 →  잔차 + 주식수×δ < GATE.  주식수 적은(고가) 종목은 δ 커도 안전, 많은
# 종목은 작은 δ에도 위험 → 자동으로 KRW·소수주식은 채택, USD·다수주식은 거부.
# δ: KRW≈0(마감가라 기준가=화면가), USD≈장중가+환율 오차. (캡처시각 알면 축소 — EXIF 경로)
GATE = 0.33
DELTA = {"KRW": 0.0006, "USD": 0.015}
SYMBOL_TOL = 0.10   # 심볼 검증: 화면 단가 vs 캡처일 종가 허용 괴리(시간외가·장중가 여유)

def _is_cash(h):
    n = str(h.get("name") or "")
    return h.get("assetClass") == "현금" or any(k in n for k in ("예수금", "현금", "달러", "CMA"))


def _fix_cost(h):
    """cost/pnl 정리 — 회계 항등식(평가금액 = 매수금액 + 평가손익)을 강제한다.
    · 매수금액 열이 없는 화면에서 모델은 평가손익을 cost에 밀어넣는다(매수금액은 음수 불가).
    · 열이 없으면 값을 지어내기도 한다 → 항등식이 깨지면 화면에서 읽은 value·pnl을 믿고 cost를 계산."""
    v, c, p = h.get("value"), h.get("cost"), h.get("pnl")
    if c is not None and (c < 0 or (p is not None and abs(c - p) < 1)):
        p = c if p is None else p           # cost 자리에 있던 건 실은 손익
        h["pnl"], c = p, None
    if None not in (v, c, p) and abs(v - c - p) > max(1.0, abs(v) * 0.001):
        c = None                            # 항등식 위반 → 지어낸 매수금액(신뢰 불가)
    if c is None and p is not None and v is not None:
        h["cost"] = round(v - p, 2); h["cost_src"] = "computed:value-pnl"
    elif c is not None:
        h["cost"] = c; h["cost_src"] = "screen"
    else:
        h["cost"] = None


def enrich(rows, capture_dt):
    """엔리치 사다리 — **추측한 값은 반드시 출처(qty_src·confidence)로 표시**한다(사용자 오해 방지).
      T1 화면 수량                      → screen / exact
      T2 수량·평가금액 → 주가            → computed:value/qty (USD 자산인데 화면값이 원화면 ÷FX)
      T3 계좌간 동일종목 주가로 수량 역산   → derived:cross-account / high
      T4 캡처일(EXIF) 종가로 수량 역산    → capture-close / estimated (노이즈 게이트)
      실패                             → qty=null + unreproducible (지어내지 않는다)
    T3가 T4보다 위인 이유: 같은 캡처 시점·같은 종목의 주가라 시장 타이밍·환율 오차가 끼지 않는다.
    capture_dt: tz-aware datetime (스크린샷 캡처 시각)."""
    cache = resolve.load_cache()
    fx_cap = ["unset"]  # 캡처 시점 USD/KRW (lazy)
    def get_fx():
        if fx_cap[0] == "unset":
            fx = None
            for h in rows:      # 내부 근거 우선: 화면의 외화잔액 행(달러 잔액 ↔ 원화 평가금액)
                if h.get("currency") == "USD" and _is_cash(h) and h.get("qty") and h.get("value"):
                    r = h["value"] / h["qty"]
                    if 500 < r < 3000:
                        fx = round(r, 2); break
            if fx is None:
                try:
                    fx = fetch_prices.price_asof("KRW=X", capture_dt, "KRW")[0]
                except Exception:
                    fx = None
            fx_cap[0] = fx
        return fx_cap[0]

    def close_of(rec, cur):
        try:
            return fetch_prices.price_asof(rec["symbol"], capture_dt, cur)
        except Exception:
            return None, None

    for h in rows:
        for k in ("qty", "value", "cost", "price", "pnl"):
            if k in h: h[k] = num(h[k])
        if isinstance(h.get("qty"), float) and h["qty"].is_integer():
            h["qty"] = int(h["qty"])
        if h.get("value") is not None:
            h.setdefault("value_src", "screen")     # 평가금액은 화면값이 진실(재평가 전까지)
        _fix_cost(h)
        try:
            rec = resolve.resolve(h.get("name"), h.get("currency"), cache)
        except Exception:
            rec = None
        if rec:
            h["symbol"], h["market"] = rec["symbol"], rec["market"]
            if not _is_cash(h):                     # 통화는 자산의 시장이 정한다(모델 추측 아님)
                h["currency"] = "USD" if rec["market"] == "US" else "KRW"
        h["_native_usd"] = (h.get("currency") == "USD" and not _is_cash(h))
        # T1
        if h.get("qty") is not None:
            h.setdefault("qty_src", "screen"); h.setdefault("confidence", "exact")

    # 외화 현금의 짝(달러 잔액 행 + 원화 평가금액 행)을 한 자산으로 병합 — '한 자산 = 한 행'.
    # 화면이 같은 돈을 두 단위로 보여주면 모델은 두 행으로 낸다. 코드가 불변식으로 되돌린다.
    for f in {h.get("_file") for h in rows}:
        cash = [h for h in rows if h.get("_file") == f and _is_cash(h) and h.get("value")]
        usd = [h for h in cash if h.get("currency") == "USD" and h["value"] < 1e6]
        krw = [h for h in cash if h.get("currency") != "USD" and h["value"] > 1e5]
        for u in usd:
            for k in krw:
                r = k["value"] / u["value"]
                if 500 < r < 3000:               # 두 행의 비 = 환율 → 같은 돈의 두 표기
                    k.update({"qty": u["value"], "currency": "USD", "price": 1.0,
                              "qty_src": "screen", "price_src": "cash", "confidence": "exact"})
                    u["_drop"] = True
                    break
    rows[:] = [h for h in rows if not h.pop("_drop", False)]

    # 화면 수량 검증 게이트 — 모델이 수량 칸에 엉뚱한 열(평가손익 등)을 넣는 일이 잦다.
    # 캡처일 종가로 계산한 기대 수량과 크게 어긋나면 **그 수량을 채택하지 않는다**(→ T3/T4가 다시 도출).
    # 틀린 수량을 그대로 쓰면 리밸런싱 전체가 틀어진다 — 빈칸이 낫다.
    for h in rows:
        if h.get("qty") is None or not h.get("value") or not h.get("symbol") or _is_cash(h):
            continue
        close, _ = close_of(h, h.get("currency"))
        fx = get_fx()
        if not close:
            continue
        for denom in ((close * fx) if fx else None, close):     # 원화표기 / 네이티브 두 가정
            if denom and abs(h["value"] / denom - h["qty"]) / max(h["value"] / denom, 1) <= 0.02:
                break
        else:
            h["qty_note"] = (f"화면 수량 {h['qty']:,} 기각 — 캡처일 종가로 설명되지 않음"
                             f"(열 오매핑 의심)")
            h["qty"] = None
            h.pop("qty_src", None); h.pop("confidence", None)
            h["price"] = None; h.pop("price_src", None)         # 같은 행의 주가도 신뢰 불가

    # 심볼 검증 게이트 — 이름 검색은 엉뚱한 종목을 집을 수 있다('메타 플랫폼스'→국내 메타랩스).
    # 화면 단가(평가금액/수량)가 그 심볼의 캡처일 종가로 설명되지 않으면 **채택하지 않는다**.
    # 조용한 오매칭이 잘못된 수량·주가로 번지는 것을 막는다(틀린 값보다 빈칸이 낫다).
    for h in rows:
        if not h.get("symbol") or not h.get("qty") or not h.get("value") or _is_cash(h):
            continue
        close, _ = close_of(h, h.get("currency"))
        fx = get_fx()
        if not close:
            continue
        per = h["value"] / h["qty"]
        e_native = abs(per - close) / close                       # 화면값이 네이티브 통화
        e_krw = abs(per - close * fx) / (close * fx) if fx else 9  # 화면값이 원화(해외주식 원화표기)
        if min(e_native, e_krw) > SYMBOL_TOL:
            h["symbol_note"] = (f"심볼 불일치 — {h['symbol']} 캡처일 종가로 화면 단가({per:,.0f})가 "
                                f"설명되지 않음(오해석 의심)")
            h.pop("symbol", None); h.pop("market", None)
            h["_native_usd"] = False
            continue
        h["_value_krw"] = e_krw < e_native   # 화면 평가금액의 통화를 측정으로 판정(추측 아님)

    # T2 — 주가(네이티브 통화). 원화 평가금액이면 FX로 나눈다.
    for h in rows:
        if _is_cash(h):
            # 현금의 단가는 1(1달러는 1달러다). value/qty로 계산하면 '환율'이 주가 자리에 들어간다.
            if h.get("qty") and h.get("price") is None:
                h["price"], h["price_src"] = 1.0, "cash"
            continue
        if h.get("price") is None and h.get("qty") and h.get("value"):
            fx = get_fx()
            if h.get("_native_usd") and h.get("_value_krw") and fx:
                h["price"] = round(h["value"] / h["qty"] / fx, 2)
                h["price_src"] = f"computed:value/qty/FX({fx:,.2f})"
            else:
                h["price"] = round(h["value"] / h["qty"], 2)
                h["price_src"] = "computed:value/qty"

    # T3 — 계좌간 동일종목: 수량이 있는 화면의 주가로, 수량이 없는 화면의 수량을 역산.
    known = {}          # symbol → (단가, 원화기준 단가)
    for h in rows:
        if h.get("symbol") and h.get("qty") and h.get("value") and not _is_cash(h):
            known.setdefault(h["symbol"], h["value"] / h["qty"])   # 화면 통화 기준 단가
    for h in rows:
        if h.get("qty") or not h.get("value") or _is_cash(h):
            continue
        unit = known.get(h.get("symbol"))
        if not unit:
            continue
        rawq = h["value"] / unit
        q = round(rawq); resid = round(abs(rawq - q), 3)
        margin = round(resid + q * DELTA["KRW"], 3)
        if q > 0 and margin < GATE:      # 정수 검증은 게이트 — 안 떨어지면 채택하지 않는다
            h["qty"] = q
            h["qty_src"] = f"derived:cross-account({unit:,.0f})"
            h["confidence"] = "high"
            h["qty_resid"] = resid; h["qty_margin"] = margin
            if h.get("price") is None:
                h["price"] = round(unit, 2); h["price_src"] = "cross-account"

    # T4 — 캡처일 종가로 역산(외부 시세). T3가 실패한 것만.
    for h in rows:
        if h.get("qty") or not h.get("value") or not h.get("symbol") or _is_cash(h):
            continue
        usd = h.get("_native_usd")
        close, cday = close_of(h, h.get("currency"))
        fx = get_fx()
        denom = (close * fx if (usd and fx) else (None if usd else close)) if close else None
        if denom:
            rawq = h["value"] / denom
            q = round(rawq); resid = round(abs(rawq - q), 3)
            margin = round(resid + q * (DELTA["USD"] if usd else DELTA["KRW"]), 3)
            if q > 0 and margin < GATE:
                h["qty"] = q
                h["qty_src"] = f"derived:capture-close({cday})"
                h["confidence"] = "estimated-low" if usd else "estimated"
                h["qty_resid"] = resid; h["qty_margin"] = margin
                if h.get("price") is None:
                    h["price"] = round(close, 2); h["price_src"] = f"capture-close:{cday}"
            else:
                h["confidence"] = "unreproducible"
                h["qty_note"] = f"수량 추정 신뢰 부족(잔차 {resid}, 여유 {margin}≥{GATE}) — 재평가 불가"
        else:
            h["confidence"] = "unreproducible"
            h["qty_note"] = "캡처일 종가 미취득 — 재평가 불가"

    # 통화 표현 통일 — USD 자산의 금액 필드는 **네이티브(달러)**로 내보낸다.
    # 한국 앱 화면은 해외주식도 '원화 평가금액'으로 보여주지만, 앱(프론트)은 USD 행을 fx로 환산한다
    # (krw = qty×price×fx, costKrw = cost×fx). 원화 금액을 그대로 넘기면 환율이 두 번 곱해진다.
    fx = get_fx()
    for h in rows:
        if h.get("_native_usd") and h.get("_value_krw") and fx:
            for k in ("value", "cost"):
                if h.get(k) is not None:
                    h[k] = round(h[k] / fx, 2)
            # price도 같은 불변식 — 앱은 value보다 qty×price를 우선하므로 price가 원화면 표가 깨진다.
            # 이미 달러인 출처(capture-close·computed:…/FX)가 섞여 있어, 어느 단위인지는 출처 문자열이
            # 아니라 측정으로 판정: 환산 후 단가(value/qty)에 더 가까워지는 가정을 택한다.
            if h.get("price") and h.get("qty") and h.get("value"):
                per = h["value"] / h["qty"]                     # 달러 단가(방금 환산됨)
                if abs(h["price"] / fx - per) < abs(h["price"] - per):
                    h["price"] = round(h["price"] / fx, 2)
            h["fx_applied"] = fx
        elif (_is_cash(h) and h.get("currency") == "USD" and fx and h.get("qty")
              and h.get("value") and abs(h["value"] / h["qty"] - fx) / fx < 0.02):
            # 외화 현금도 같은 규칙: 평가금액을 달러로(원화 금액을 USD로 표시하면 자기모순이고,
            # 앱이 fx를 다시 곱하면 값이 튄다). 화면의 원화 금액 = 달러잔액 × 환율임을 확인한 뒤 환산.
            h["value"] = round(h["value"] / fx, 2)
            h["fx_applied"] = fx

    # 표시 항등식 게이트 — 앱은 qty×price를 value보다 우선한다(재평가 경로가 price를 갱신하는 구조라).
    # 화면 현재가를 오독하면(자릿수 유실 등) 추출 value가 정확해도 표가 조용히 오염된다.
    # 평가금액이 진실(value_src=screen)이므로, qty×price가 value와 2% 넘게 어긋나면 price를 버리고
    # value/qty로 되돌린다. 2%는 앱의 checkFail 기준과 같은 값(계약 단일화).
    for h in rows:
        if _is_cash(h) or not (h.get("qty") and h.get("price") and h.get("value")):
            continue
        if abs(h["qty"] * h["price"] - h["value"]) / h["value"] > 0.02:
            h["price_note"] = (f"화면 현재가 {h['price']:,} 기각 — qty×price가 평가금액과 "
                               f"불일치(오독 의심)")
            h["price"] = round(h["value"] / h["qty"], 2)
            h["price_src"] = "computed:value/qty"

    for h in rows:
        if not h.get("qty") and not h.get("confidence"):
            h["confidence"] = "unreproducible"
        if _is_cash(h) and h.get("price") is None and h.get("qty"):
            h["price_src"] = "cash"
        for k in ("_native_usd", "_value_krw"):
            h.pop(k, None)
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


def reprice(holdings, capture_dt):
    """앱의 보유자산 → 현재가로 재평가. 심볼 해석·T4(수량 복원)·현재가 합성.
    반환: {fx, asOf, captureDateTime, holdings:[... value=수량×현재가×(환율 if USD)]}."""
    rows = enrich(holdings, capture_dt)        # symbol + qty(T4) + confidence
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
    return {"fx": fx, "asOf": pdata.get("asOf"),
            "captureDateTime": capture_dt.isoformat(), "holdings": rows}


def extract(b64, capture_dt):
    body = json.dumps({"model": MODEL, "prompt": PROMPT, "images": [resample_half_b64(b64)],
                       "stream": False, "keep_alive": -1, "options": {"temperature": 0, "num_ctx": 8192}}).encode()
    t0 = time.time()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        out = json.loads(r.read())
    raw = out.get("response", "")
    dt = exif_capture_dt(b64)                  # 이미지에 EXIF 캡처시각 있으면 저장·사용
    if dt:
        store_capture(dt); capture_dt = dt
    rows = parse_json(raw) or []
    warnings = []
    if not rows:
        warnings.append("JSON 파싱 실패 — 원문 확인 필요")
    rows = enrich(rows, capture_dt)
    update_watchlist(rows)
    return {"holdings": rows, "seconds": round(time.time() - t0, 1), "warnings": warnings,
            "model": MODEL, "raw": raw, "captureDateTime": capture_dt.isoformat()}


def _vision(b64):
    """이미지 1장 → 비전 원문 텍스트. 배치·단건 공용."""
    body = json.dumps({"model": MODEL, "prompt": PROMPT, "images": [resample_half_b64(b64)],
                       "stream": False, "keep_alive": -1, "options": {"temperature": 0, "num_ctx": 8192}}).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.loads(r.read()).get("response", "")


def extract_batch(images, capture_dt):
    """여러 화면을 한 번에: 화면별 비전추출 → finalize(계좌합계 대조 게이트 + broker 정규화)
    → 결정적 enrich(심볼·수량·현재가). 앱이 스크린샷 여러 장을 종합해 정확한 결과를 얻는 경로.

    비전 호출은 NP개 동시 발사 — ollama 슬롯(OLLAMA_NUM_PARALLEL)이 디코드 스텝을 배칭해
    가중치 스트리밍을 공유하므로 총 처리량이 슬롯 수에 가깝게 늘어난다. 요청은 여전히 화면당
    1개라 행→화면 귀속은 구조적으로 보존된다(NP는 ollama 슬롯 수와 일치시킬 것)."""
    t0 = time.time()
    for b64 in images:                            # EXIF는 병렬 전에 순차로(빠름·상태 갱신 결정적)
        dt = exif_capture_dt(b64)
        if dt:
            store_capture(dt); capture_dt = dt
    if NP > 1 and len(images) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=NP) as ex:
            raws = list(ex.map(_vision, images))  # map은 입력 순서로 반환 → 귀속 불변
    else:
        raws = [_vision(b64) for b64 in images]
    screens = [{"file": f"img{i + 1}", "raw": raw} for i, raw in enumerate(raws)]
    fin = finalize_mod.finalize(screens)          # holdings(정규화) + gate(대조 리포트)
    rows = enrich(fin["holdings"], capture_dt)    # 심볼 해석 + 수량 사다리 + 가격(_file은 화면단위 게이트에 필요)
    for h in rows:
        h.pop("_file", None)
    update_watchlist(rows)
    return {"holdings": rows, "gate": fin["gate"], "screens": fin["screens"],
            "seconds": round(time.time() - t0, 1), "model": MODEL,
            "captureDateTime": capture_dt.isoformat()}


def _batch_run(jid, images, capture_dt):
    try:
        res = extract_batch(images, capture_dt)
        with _JOBS_LOCK:
            _JOBS[jid] = {"status": "done", "result": res, "ts": time.time()}
    except Exception as e:
        with _JOBS_LOCK:
            _JOBS[jid] = {"status": "error", "error": str(e), "ts": time.time()}


def submit_batch(body):
    images = body.get("images") or []
    capture_dt = parse_capture(body)
    jid = os.urandom(8).hex()
    with _JOBS_LOCK:
        _JOBS[jid] = {"status": "pending", "ts": time.time()}
    threading.Thread(target=_batch_run, args=(jid, images, capture_dt), daemon=True).start()
    _job_gc()
    return {"id": jid}


def batch_result(jid):
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
    if not j:
        return {"status": "unknown"}
    if j["status"] == "done":
        return {"status": "done", **j["result"]}
    if j["status"] == "error":
        return {"status": "error", "error": j.get("error", "오류")}
    return {"status": "pending"}

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
        elif self.path.split("?")[0] in ("/complete/result", "/extract/batch/result"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            jid = (qs.get("id") or [""])[0]
            res = (batch_result if self.path.split("?")[0] == "/extract/batch/result"
                   else job_result)(jid)
            b = json.dumps(res, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8"); self._cors()
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        elif self.path.split("?")[0] == "/prices":
            # 서버가 저장한 시세 파일을 그대로 서빙(정적). 에이전트(LLM) 무관.
            if os.path.exists(PRICES_PATH):
                b = open(PRICES_PATH, "rb").read(); code = 200
            else:
                b = b'{"error":"prices.json not ready"}'; code = 503
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8"); self._cors()
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        elif self.path.split("?")[0] == "/capture":
            # 마지막 추출 스크린샷의 EXIF 캡처시각(사이드카가 기준시각 프리필·표시용)
            try:
                b = open(LAST_CAPTURE_PATH, "rb").read()
            except Exception:
                b = b'{"datetime":null}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8"); self._cors()
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        else:
            self.send_response(404); self._cors(); self.end_headers()
    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/extract", "/reprice", "/complete",
                        "/complete/submit", "/extract/batch/submit"):
            self.send_response(404); self._cors(); self.end_headers(); return
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n))
            if path == "/extract/batch/submit":     # 비동기: 여러 장 종합 추출 잡
                result = submit_batch(data)
            elif path == "/complete/submit":        # 비동기: 즉시 잡 id 반환(터널 친화)
                result = submit_complete(data)
            elif path == "/complete":               # 동기(하위호환): 앱의 Anthropic 호출 대체
                result = complete(data)
            elif path == "/reprice":
                result = reprice(data.get("holdings", []), parse_capture(data))
            else:
                result = extract(data["image"], parse_capture(data))
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
