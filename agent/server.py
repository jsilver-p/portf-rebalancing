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
MODEL = os.environ.get("MODEL", "qwen2.5vl:3b-ft3-q8")
PORT = int(os.environ.get("PORT", "8899"))
# 바인드 주소. 기본은 LAN 노출(폰 브라우저에서 Orin에 붙는 기존 사용법 유지).
# **APK는 127.0.0.1로 고정한다** — 앱이 스크린샷 API를 같은 Wi-Fi에 열어두면 안 된다.
BIND = os.environ.get("BIND", "0.0.0.0")
OLLAMA = os.environ.get("OLLAMA", "http://127.0.0.1:11434") + "/api/generate"
NP = int(os.environ.get("NP", "2"))            # 동시 비전 요청 수 — ollama의 OLLAMA_NUM_PARALLEL과 일치시킬 것
# 추출 백엔드: vlm(기본, 라이브 유지) | ocr(엣지 — OCR+기하, 신경망 LLM 0개).
# 기본을 바꾸지 않는 이유: 이 파일은 라이브 에이전트와 공유된다. 엣지는 기동 스크립트에서 켠다.
EXTRACT = os.environ.get("EXTRACT", "vlm")

# ── 경로(engine)는 **요청마다 고를 수 있다** ────────────────────────────────
# 폰에서 한 URL로 두 경로를 나란히 비교하려면 프로세스를 두 개 띄우는 게 아니라 요청이 골라야
# 한다(데이터·holdings가 갈리면 비교가 아니라 다른 앱 두 개가 된다).
#
# **경로는 추출기만이 아니다.** 폰에는 검색 결과를 읽을 LLM이 없으므로 `edge`는 증권사
# 검색까지 함께 꺼야 진짜 폰과 같다(그래야 브랜드 폴백이 실제로 밟힌다, docs §4.14).
#   engine → (추출기, finalize의 use_llm)
ENGINES = {"edge": ("ocr", False),      # ML Kit/RapidOCR + 기하. 신경망 LLM 0개 = APK와 같은 경로
           "orin": ("vlm", True)}       # 커스텀 FT 비전모델 + 증권사 검색·판독
DEFAULT_ENGINE = "edge" if EXTRACT == "ocr" else "orin"


def engine_of(name):
    """요청이 준 engine 이름 → (추출기, use_llm). 모르는 값이면 서버 기본값으로."""
    return ENGINES.get(str(name or "").strip().lower(), ENGINES[DEFAULT_ENGINE])


def engine_status():
    """각 경로가 이 프로세스에서 실제로 도는가 — 프런트가 못 고르게 막을 근거."""
    out = {}
    try:
        sys.path.insert(0, HERE)
        import ocr as _ocr                      # noqa: F401  rapidocr/mlkit가 있는 인터프리터인가
        out["edge"] = {"ok": True}
    except Exception as e:
        out["edge"] = {"ok": False, "why": f"OCR 엔진 없음({type(e).__name__})"}
    try:
        with urllib.request.urlopen(OLLAMA.replace("/api/generate", "/api/tags"), timeout=3) as r:
            names = [m.get("name") for m in json.loads(r.read()).get("models", [])]
        out["orin"] = ({"ok": True} if MODEL in names else
                       {"ok": False, "why": f"모델 미적재({MODEL})"})
    except Exception as e:
        out["orin"] = {"ok": False, "why": f"ollama 없음({type(e).__name__})"}
    return out
PROMPT_FILE = os.environ.get("PROMPT_FILE", os.path.join(ROOT, "eval/harness/prompt4f.txt"))
# prompt4f = prompt4e + broker 정의 단일화(계좌라벨 제거)·시장지수 제외·자릿수 — Phase2 3b-ft3
# OCR 경로는 프롬프트를 쓰지 않는다. APK에는 eval/ 트리가 없으므로 읽지도 않는다
# (임포트 시점에 죽지 않게. VLM 경로에서는 없으면 여전히 즉시 실패한다).
# 프롬프트는 **없으면 빈 문자열**이다. 예전엔 EXTRACT=ocr일 때만 건너뛰었는데, 이제 한
# 프로세스가 두 경로를 다 태우므로 엣지 기본으로 떠 있어도 orin 요청이 올 수 있다.
# APK에는 eval/ 트리가 없다 → 여기서 죽으면 임포트가 통째로 죽는다. VLM 요청 시점에 검사한다.
try:
    PROMPT = open(PROMPT_FILE).read().strip()
except Exception:
    PROMPT = ""
# 앱(index.html)을 에이전트가 직접 서빙 → 단일 오리진. 터널·CORS·혼합 컨텐츠가 사라진다.
INDEX_PATH = os.environ.get("INDEX_PATH", os.path.join(ROOT, "index.html"))

# 시세: 서버 전용 데이터(레포 밖). 결정론적 페치 — LLM 무관.
DATA_DIR = os.environ.get("DATA_DIR", os.path.expanduser("~/portf-agent/data"))
# 개발 기간 캡처 저장(off-by-default) — 실화면으로 모델 오류를 사후 분석하기 위한 스냅샷.
# 민감정보(실계좌 스크린샷)라 레포 밖(DATA_DIR 옆)에만 둔다. 개발 종료 시 수동 삭제.
SAVE_CAPTURES = os.environ.get("SAVE_CAPTURES", "") == "1"
CAPTURES_DIR = os.environ.get("CAPTURES_DIR",
                              os.path.normpath(os.path.join(DATA_DIR, "..", "captures")))
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
  if(x.confidence==='estimated-low') return '<span class=est>≈'+x.qty+'</span><span class="badge low" title="'+esc(x.qty_src||'')+'">추정(정확도 낮음)</span>';
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
    if(nEst)h+='<div class="muted" style="margin-top:8px">≈ 표시는 캡처일('+esc(j.captureDate)+') 시세로 <b>역산한 추정 수량</b>입니다(화면에 수량이 없어). <span class=est>추정</span>은 정수에 잘 맞은 값, <span class="badge low">추정(정확도 낮음)</span>은 시세 노이즈로 정수 확정이 약한 값 — 수량이 보이는 상세화면을 함께 올리면 정확해집니다.</div>';
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


def save_capture_batch(images, result):
    """SAVE_CAPTURES=1일 때만: 원본 업로드 이미지(리샘플 전)와 프로덕션 결과를 스냅샷으로 저장.
    ~/portf-agent/captures/<UTCts>_<rand>/ 아래 img{N}.png + result.json. 개발 기간 오류 분석용.
    저장 실패가 추출을 막지 않도록 전부 삼킨다(관측 도구일 뿐)."""
    if not SAVE_CAPTURES:
        return
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        d = os.path.join(CAPTURES_DIR, f"{ts}_{os.urandom(3).hex()}")
        os.makedirs(d, exist_ok=True)
        for i, b64 in enumerate(images):
            try:
                with open(os.path.join(d, f"img{i + 1}.png"), "wb") as f:
                    f.write(base64.b64decode(b64))
            except Exception:
                pass
        json.dump(result, open(os.path.join(d, "result.json"), "w"),
                  ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"· 캡처 저장 실패: {e}")


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


def capture_source(data):
    """이미지 없는 경로(reprice)에서 캡처 시각의 출처를 판정 → UI 표기용.
    'manual'(사용자가 직접 지정) / 'exif'(직전 추출의 저장된 EXIF) / 'fallback'(기준일·now)."""
    if data.get("captureDateTime"):
        return "manual"
    try:
        json.load(open(LAST_CAPTURE_PATH))   # 직전 추출이 남긴 EXIF 시각
        return "exif"
    except Exception:
        return "fallback"


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


# 유도 수량(T3 교차계좌·T4 캡처시점가) 채택 게이트: q = round(value/참가격)의 **소수부**만 본다.
# 소수부가 크다 = rawq가 두 정수 사이에 걸쳐 어느 쪽인지 분별 불가(resid≥1/3이면 이웃 정수가 2배
# 이내로 붙음). resid≥1/3 → 빈칸, resid<0.10 → 확신, 그 사이 → '정확도 낮음'. (옛 q×δ 항은
# 주식수에 비례해 다수주 정답을 오기각 → 제거. 참가격 노이즈는 이미 소수부에 반영된다.)
BLANK_MARGIN = 1 / 3   # resid ≥ 1/3 → 빈칸(정수 분별 불가)
CONF_MARGIN = 0.10     # resid < 0.10 → 확신 티어, 그 사이 → 정확도 낮음 티어
SYMBOL_TOL = 0.10   # 심볼 검증: 화면 단가 vs 캡처시점 참가격 허용 괴리(시간외가 여유)
# 화면 수량 게이트: 명시된 정수 수량은 신뢰하고, 참가격 대비 이만큼(배수/자릿수급) 어긋날 때만 기각
# (열-오매핑·이름 속 숫자 혼입만 컷). 정상 장중가 드리프트(수 %)는 통과.
QTY_GROSS_TOL = 0.35


def _round_qty(value, denom):
    """value/denom → (정수, resid, tier). tier: 'conf'|'low'|None(빈칸). denom=참가격 분모(화면 통화 기준).
    유도 수량의 단일 채택 판정(T3·T4 공용) — 소수부 마진만 본다."""
    if not denom or value is None:
        return None, None, None
    rawq = value / denom
    q = round(rawq)
    if q <= 0:
        return None, None, None
    resid = round(abs(rawq - q), 3)
    if resid >= BLANK_MARGIN:
        return None, resid, None
    return q, resid, ("conf" if resid < CONF_MARGIN else "low")


def _qty_from_name(h):
    """화면 수량이 **종목명 속 숫자**와 같은가 — 열 오매핑의 대표 실패 모드다
    ('TIGER 미국나스닥100' → qty 100, 'KODEX 미국S&P500' → qty 500).

    이 판정은 **심볼·시세를 전혀 쓰지 않는다.** 그래서 심볼 게이트와 수량 게이트의 순환
    의존(심볼 검증엔 수량이, 수량 검증엔 심볼이 필요하다)을 끊는 지점이 된다.
    검증: 픽스처 31행 중 이름에 숫자가 있는 4행 모두 정답 수량이 그 숫자와 다르다
    (KODEX S&P500→115·312, TIGER 나스닥100→30, TIME 나스닥100…50→223) — 오탐 0."""
    q = h.get("qty")
    if q is None or float(q) != int(q):
        return False
    return int(q) in {int(x) for x in re.findall(r"\d+", str(h.get("name") or ""))}


def _is_cash(h):
    n = str(h.get("name") or "")
    return h.get("assetClass") == "현금" or any(k in n for k in ("예수금", "현금", "달러", "CMA"))


def _explains(per, close, fx):
    """캡처시점 참가격이 화면 단가를 설명하는가 — 네이티브/원화표기 두 가정 중 가까운 쪽."""
    if not close:
        return 9.0
    e = abs(per - close) / close
    return min(e, abs(per - close * fx) / (close * fx)) if fx else e


def _arbitrate_symbol(h, per, fx, close_of, cache):
    """심볼 게이트가 기각한 행을 **근거로** 고칠 수 있는지 본다 — 글리프 혼동 후보 중
    캡처시점 시세가 화면 단가를 설명하는 것이 **정확히 하나**면 그것으로 교정한다.

    왜 여기인가: 오독은 OCR 층에서 못 고친다(측정 2026-08-06 — 화면의 `IVV`가 커닝 때문에
    `IWV`와 같은 잉크이고, rec 모델 4종·8배 확대까지 전부 같은 답을 낸다). 그리고 검색으로도
    못 가른다(`IVV`·`IWV` 둘 다 실재 ETF). 가르는 근거는 **시세뿐**이다:
    화면 단가 751.18 / IVV 749.06(−0.28%) / IWV 424.96(+76.8%).

    0개면 교정하지 않고 2개 이상이면 **분별력이 없는 것**이라 역시 교정하지 않는다.
    두 경우 다 호출부가 기존대로 심볼을 비우고 경고한다 — **지어내지 않는다.**
    판정 허용치는 심볼 게이트와 **같은 `SYMBOL_TOL`**이다(새 상수를 만들지 않는다)."""
    hits = []
    for cand in resolve.confusable_variants(h.get("symbol")):
        try:
            rec = resolve.resolve(cand, h.get("currency"), cache)
        except Exception:
            continue
        if not rec or rec["symbol"] != cand:
            continue                      # 자동완성이 다른 종목을 준 것 = 실재 티커가 아니다
        close, _ = close_of(rec, h.get("currency"))
        if _explains(per, close, fx) <= SYMBOL_TOL:
            hits.append((cand, rec, close))
    return hits[0] if len(hits) == 1 else None


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


def enrich(rows, capture_dt, mode="extract"):
    """엔리치 사다리 — **추측한 값은 반드시 출처(qty_src·confidence)로 표시**한다(사용자 오해 방지).
    mode="extract"(기본): STEP1 추출 — 화면수량 검증 게이트 포함(품질검증·확인필요 마커 신호원).
    mode="reprice": STEP2 재평가 — 이미 검토·확정된 수량을 신뢰한다(화면수량 기각 게이트 미실행).
      T1 화면 수량                      → screen / exact
      T2 수량·평가금액 → 주가            → computed:value/qty (USD 자산인데 화면값이 원화면 ÷FX)
      T3 계좌간 동일종목 주가로 수량 역산   → derived:cross-account / high
      T4 캡처일(EXIF) 종가로 수량 역산    → capture-close / estimated (노이즈 게이트)
      실패                             → qty=null + unreproducible (지어내지 않는다)
    T3가 T4보다 위인 이유: 같은 캡처 시점·같은 종목의 주가라 시장 타이밍·환율 오차가 끼지 않는다.
    capture_dt: tz-aware datetime (스크린샷 캡처 시각)."""
    cache = {}          # 이름→심볼 메모: 이 요청 안에서만 산다(디스크 캐시 없음, `resolve` docstring)
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
                    fx = fetch_prices.price_at("KRW=X", capture_dt, "KRW", cache=pxcache)[0]
                except Exception:
                    fx = None
            fx_cap[0] = fx
        return fx_cap[0]

    pxcache = {}   # (symbol,range,interval) → 분봉 시리즈: enrich 1회 실행 중 재요청 방지
    def close_of(rec, cur):
        # 캡처 '시점'의 참가격(최근 캡처면 장중 분봉, 오래되면 일봉 폴백) — 단일 기준가.
        try:
            return fetch_prices.price_at(rec["symbol"], capture_dt, cur, cache=pxcache)
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

    # 심볼 검증 게이트 — 이름 검색은 엉뚱한 종목을 집을 수 있다('메타 플랫폼스'→국내 메타랩스).
    # 화면 단가(평가금액/수량)가 그 심볼의 캡처시점 참가격으로 설명되지 않으면 **채택하지 않는다**.
    # 조용한 오매칭이 잘못된 수량·주가로 번지는 것을 막는다(틀린 값보다 빈칸이 낫다).
    #
    # ── 순환 의존을 끊는다 ──────────────────────────────────────────────────
    # 이 게이트는 `value/qty`를 쓰므로 **수량이 맞아야** 하고, 아래 수량 게이트는 심볼의
    # 참가격을 쓰므로 **심볼이 맞아야** 한다. 순서만 바꾸면 어느 쪽이 깨질지가 바뀔 뿐이다:
    #   심볼 먼저 → 'TIGER 미국나스닥100'의 이름숫자 혼입(qty=100)이 심볼을 떨구고,
    #               수량 게이트가 symbol 없는 행을 건너뛰어 오독 수량이 살아남는다(실측 회귀).
    #   수량 먼저 → 오독 티커('IVV'→'IWV')의 엉뚱한 시세로 멀쩡한 화면 수량이 기각된다.
    # 그래서 이 게이트는 **수량이 신뢰할 수 없을 때 판정을 포기한다** — 근거가 오염된 상태로
    # 심볼을 기각하느니 아래 수량 게이트에 넘긴다. 오염 여부는 `_qty_from_name`이 **심볼과
    # 무관하게** 판정하므로 순환이 끊기고 순서가 무의미해진다.
    #
    # 참고 — 폐기한 대안: '`value/참종가`가 정수면 심볼이 맞다'는 2차 증거를 넣어봤는데
    # **분별력이 없었다**. 실측: 오독 티커 `IWV`가 58.02주(잔차 0.02)를 내서 정답 `IVV`의
    # 33.06주(잔차 0.06)보다 오히려 더 정수에 가까웠다. 수량이 크면 시세가 0.4%만 움직여도
    # 소수부가 한 바퀴 돌기 때문에 애초에 심볼 판별에 쓸 수 있는 신호가 아니다.
    for h in rows:
        if not h.get("symbol") or not h.get("qty") or not h.get("value") or _is_cash(h):
            continue
        if _qty_from_name(h):
            continue          # 이 수량으로는 심볼을 판정하지 않는다(아래 수량 게이트가 잡는다)
        close, _ = close_of(h, h.get("currency"))
        fx = get_fx()
        if not close:
            continue
        per = h["value"] / h["qty"]
        e_native = abs(per - close) / close                       # 화면값이 네이티브 통화
        e_krw = abs(per - close * fx) / (close * fx) if fx else 9  # 화면값이 원화(해외주식 원화표기)
        if min(e_native, e_krw) > SYMBOL_TOL:
            bad = h["symbol"]
            fix = _arbitrate_symbol(h, per, fx, close_of, cache)   # 버리기 전에 근거로 고쳐본다
            if fix:
                cand, rec, cclose = fix
                if str(h.get("name") or "").strip() == bad:        # 이름 자체가 티커였으면 같이
                    h["name"] = cand
                h["symbol"], h["market"] = rec["symbol"], rec["market"]
                h["symbol_src"] = "corrected:price-arbitration"
                # 화면 평가금액이 원화 표기면 단가도 원화다 → 근거를 **같은 단위로** 적는다
                h["_value_krw"] = (abs(per - cclose * fx) / (cclose * fx) if fx else 9) < \
                                  abs(per - cclose) / cclose
                shown = per / fx if (h["_value_krw"] and fx) else per
                h["symbol_note"] = (f"식별자 교정 {bad}→{cand} — 화면 단가({shown:,.2f})를 캡처일 "
                                    f"종가({cclose:,.2f})가 설명(글리프 혼동 후보 중 유일)")
                continue
            h["symbol_note"] = (f"심볼 불일치 — {h['symbol']} 캡처일 종가로 화면 단가({per:,.0f})가 "
                                f"설명되지 않음(오해석 의심)")
            h.pop("symbol", None); h.pop("market", None)
            h["_native_usd"] = False
            continue
        h["_value_krw"] = e_krw < e_native   # 화면 평가금액의 통화를 측정으로 판정(추측 아님)

    # 화면 수량 게이트 — **화면에 명시된 정수 수량은 신뢰한다.** 다만 모델이 수량 없는 화면에서
    # 다른 열(평가손익)이나 이름 속 숫자('나스닥100'→qty 100)를 수량 칸에 넣는 열-오매핑만 걸러낸다.
    # 캡처시점 참가격(price_at)으로 계산한 기대 수량과 **배수/자릿수급(>QTY_GROSS_TOL) 어긋날 때만** 기각 —
    # 정상 장중가 드리프트(수 %)는 통과한다(폭락일 정상 수량 오기각 방지가 이 완화의 핵심).
    # (재평가 mode=reprice는 STEP1에서 확정된 수량을 신뢰 — 이 게이트를 돌리지 않는다.)
    for h in rows:
        if mode == "reprice" or h.get("qty") is None or not h.get("value") or not h.get("symbol") or _is_cash(h):
            continue
        close, _ = close_of(h, h.get("currency"))
        fx = get_fx()
        if not close:
            continue
        for denom in ((close * fx) if fx else None, close):     # 원화표기 / 네이티브 두 가정
            if denom and abs(h["value"] / denom - h["qty"]) / max(h["value"] / denom, 1) <= QTY_GROSS_TOL:
                break
        else:
            h["qty_note"] = (f"화면 수량 {h['qty']:,} 기각 — 참가격으로 설명 안 됨"
                             f"(열 오매핑·이름숫자 혼입 의심)")
            h["qty"] = None
            h.pop("qty_src", None); h.pop("confidence", None)
            h["price"] = None; h.pop("price_src", None)         # 같은 행의 주가도 신뢰 불가

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
        q, resid, tier = _round_qty(h["value"], unit)
        if tier:      # 소수부 분별되면 채택 — 확신이면 high, 낮으면 estimated-low
            h["qty"] = q
            h["qty_src"] = f"derived:cross-account({unit:,.0f})"
            h["confidence"] = "high" if tier == "conf" else "estimated-low"
            h["qty_resid"] = resid
            if h.get("price") is None:
                h["price"] = round(unit, 2); h["price_src"] = "cross-account"

    # T4 — 캡처시점 참가격으로 역산(외부 시세, 장중 분봉/일봉 폴백). T3가 실패한 것만.
    for h in rows:
        if h.get("qty") or not h.get("value") or not h.get("symbol") or _is_cash(h):
            continue
        usd = h.get("_native_usd")
        close, cday = close_of(h, h.get("currency"))
        fx = get_fx()
        denom = (close * fx if (usd and fx) else (None if usd else close)) if close else None
        if not denom:
            h["confidence"] = "unreproducible"
            h["qty_note"] = "캡처시점 참가격 미취득 — 재평가 불가"
            continue
        q, resid, tier = _round_qty(h["value"], denom)
        if tier:
            h["qty"] = q
            h["qty_src"] = f"derived:capture-close({cday})"
            # USD는 FX 노이즈가 더해져 확신 티어로 올리지 않는다.
            h["confidence"] = "estimated" if (tier == "conf" and not usd) else "estimated-low"
            h["qty_resid"] = resid
            if h.get("price") is None:
                h["price"] = round(close, 2); h["price_src"] = f"capture-close:{cday}"
        else:
            h["confidence"] = "unreproducible"
            h["qty_note"] = (f"정수 추정 불가(소수부 {resid} — 이웃 정수와 분별 안 됨)"
                             if resid is not None else "수량 추정 불가")

    # 통화 표현 통일 — USD 자산의 금액 필드는 **네이티브(달러)**로 내보낸다.
    # 한국 앱 화면은 해외주식도 '원화 평가금액'으로 보여주지만, 앱(프론트)은 USD 행을 fx로 환산한다
    # (krw = qty×price×fx, costKrw = cost×fx). 원화 금액을 그대로 넘기면 환율이 두 번 곱해진다.
    fx = get_fx()
    for h in rows:
        if h.get("_native_usd") and h.get("_value_krw") and fx:
            for k in ("value", "cost", "pnl"):      # pnl도 함께 — 화면 평가손익은 원화라 value·cost와 같은 단위여야
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
        elif not h.get("price_src"):
            # **출처 없는 숫자를 그대로 두지 않는다.** 추출기가 낸 price가 항등식은 통과했지만
            # 어디서 왔는지 우리가 댈 근거가 없다(이 화면들엔 단가 열이 아예 없다 — 모델이 계산했거나
            # 지어낸 것이다). 우리가 확인한 두 값(value·qty)으로 다시 계산해 **근거를 붙인다.**
            # 항등식을 이미 통과했으므로 값은 2% 안에서 같다 — 바뀌는 것은 출처의 유무뿐이다.
            h["price"] = round(h["value"] / h["qty"], 2)
            h["price_src"] = "computed:value/qty"

    for h in rows:
        if not h.get("qty") and not h.get("confidence"):
            h["confidence"] = "unreproducible"
        if _is_cash(h) and h.get("price") is None and h.get("qty"):
            h["price_src"] = "cash"
        for k in ("_native_usd", "_value_krw"):
            h.pop(k, None)
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


def reprice(holdings, capture_dt, capt_source="exif"):
    """앱의 보유자산 → 현재가로 재평가. 목적: 오늘 주가로 평가금액 최신화.
    STEP2는 검토·확정된 수량을 신뢰한다(mode=reprice → 화면수량 기각 게이트 미실행).
    현재가를 못 붙이는 행(심볼 미해석·현재가 미취득·수량 없음)만 kept + reprice_note로 명시.
    반환: {fx, asOf, captureDateTime, captureSource, holdings:[... value=수량×현재가]}."""
    rows = enrich(holdings, capture_dt, mode="reprice")   # symbol + (수량 없을 때만)T4 + confidence
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
            h.pop("reprice_note", None)
        else:
            h["value_src"] = "kept"  # 재평가 불가(기존 값 유지)
            if not _is_cash(h):      # 현금은 재평가 대상이 아님(정상) — note 없음
                if not s:
                    h["reprice_note"] = "심볼 미해석 — 재평가 불가, 스크린샷 값 유지"
                elif not h.get("qty"):
                    h["reprice_note"] = "수량 없음 — 재평가 불가, 스크린샷 값 유지"
                else:
                    h["reprice_note"] = "현재가 미취득 — 재평가 불가, 스크린샷 값 유지"
    return {"fx": fx, "asOf": pdata.get("asOf"),
            "captureDateTime": capture_dt.isoformat(), "captureSource": capt_source,
            "holdings": rows}


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


def _ocr_boxes(b64):
    import base64 as _b64
    sys.path.insert(0, HERE)
    import ocr
    return ocr.recognize(_b64.b64decode(b64))


def _evidence_text(boxes):
    """화면 원문 텍스트 — finalize의 broker 심판용(읽기순 = y, x)."""
    return " ".join(str(b["text"]) for b in sorted(boxes, key=lambda b: (b["y"], b["x"])))


def _vision(b64, mode=None):
    """이미지 1장 → (추출 원문 텍스트, 화면 OCR 텍스트, 기하 바인딩 행 JSON|None). 배치·단건 공용.

    두 번째 값(evidence)은 **비전 모델과 독립인 심판**이다. 값에는 총액 대조가 있지만
    라벨에는 심판이 없어 모델이 증권사명을 지어내면 게이트가 침묵했다 —
    OCR 텍스트가 그 자리를 메운다(finalize._label_supported).

    세 번째 값(geom)은 VLM 모드에서만 실린다 — 같은 OCR 박스를 기하 바인딩(bind)에 태운
    결정적 판독. VLM이 비운 수량 칸을 finalize가 값-일치 조인으로 되메꾼다(실측 08-10:
    IRP·ISA 폼에서 VLM이 화면에 실재하는 수량을 전 행 null로 냈다 → 추정으로 오표시).

    mode: 'ocr' | 'vlm'. 없으면 프로세스 기본(EXTRACT).
    """
    if (mode or EXTRACT) == "ocr":
        sys.path.insert(0, HERE)
        import bind
        boxes = _ocr_boxes(b64)                     # OCR은 한 번만 — 추출과 심판이 같은 박스에서 나온다
        return json.dumps(bind.bind(boxes), ensure_ascii=False), _evidence_text(boxes), None
    if not PROMPT:                                  # 프롬프트 없이 비전 호출 = 조용히 쓰레기를 낸다
        raise RuntimeError(f"VLM 경로인데 프롬프트가 없다: {PROMPT_FILE}")
    body = json.dumps({"model": MODEL, "prompt": PROMPT, "images": [resample_half_b64(b64)],
                       "stream": False, "keep_alive": -1, "options": {"temperature": 0, "num_ctx": 8192}}).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        raw = json.loads(r.read()).get("response", "")
    try:                                            # 심판은 있으면 좋은 것 — 실패해도 추출을 막지 않는다
        boxes = _ocr_boxes(b64)
        evidence = _evidence_text(boxes)
        sys.path.insert(0, HERE)
        import bind
        geom = json.dumps(bind.bind(boxes), ensure_ascii=False)
    except Exception as e:
        # 이 경로는 **실제로 밟힌다**: VLM 서버는 OCR 없이도 도는 게 정상이라 인터프리터에
        # rapidocr가 없을 수 있다(실측: 라이브의 시스템 python3). 그때 심판은 무판정으로
        # 물러나고 추출은 계속돼야 한다 — 여기서 죽으면 추출 전체가 죽는다(실측 사고).
        print(f"· evidence OCR 없음 — broker 심판·수량 되메꿈 생략({type(e).__name__}: {e})")
        evidence, geom = None, None
    return raw, evidence, geom


def extract_batch(images, capture_dt, on_screen=None, on_stage=None, engine=None, names=None):
    """여러 화면을 한 번에: 화면별 비전추출 → finalize(계좌합계 대조 게이트 + broker 정규화)
    → 결정적 enrich(심볼·수량·현재가). 앱이 스크린샷 여러 장을 종합해 정확한 결과를 얻는 경로.

    비전 호출은 NP개 동시 발사 — ollama 슬롯(OLLAMA_NUM_PARALLEL)이 디코드 스텝을 배칭해
    가중치 스트리밍을 공유하므로 총 처리량이 슬롯 수에 가깝게 늘어난다. 요청은 여전히 화면당
    1개라 행→화면 귀속은 구조적으로 보존된다(NP는 ollama 슬롯 수와 일치시킬 것)."""
    t0 = time.time()
    eng = str(engine or DEFAULT_ENGINE).strip().lower()
    if eng not in ENGINES:
        eng = DEFAULT_ENGINE
    mode, use_llm = ENGINES[eng]
    exif_found = False
    for b64 in images:                            # EXIF는 병렬 전에 순차로(빠름·상태 갱신 결정적)
        dt = exif_capture_dt(b64)
        if dt:
            store_capture(dt); capture_dt = dt; exif_found = True
    capture_src = "exif" if exif_found else "fallback"
    if not exif_found:                            # EXIF가 벗겨진 업로드 → 파일명의 캡처시각이 폴백
        for nm in (names or []):                  # ('Screenshot_20260709_160155_….jpg')
            mt = re.search(r"(20\d{2})(\d{2})(\d{2})[_\-]?(\d{2})(\d{2})(\d{2})", str(nm or ""))
            if mt:
                try:
                    dt = datetime(*(int(g) for g in mt.groups()), tzinfo=KST)
                    store_capture(dt); capture_dt = dt; capture_src = "filename"
                    break
                except ValueError:
                    continue
    n = len(images)
    raws = [None] * n                             # 입력 순서 보존(행→화면 귀속 불변) — as_completed여도 자리에 채움
    evid = [None] * n                             # 화면별 OCR 원문 — broker 라벨의 독립 심판
    geoms = [None] * n                            # 화면별 기하 바인딩 행(VLM 모드) — 수량 되메꿈 근거
    def _done(i, res):
        raw, raws[i], evid[i], geoms[i] = res[0], res[0], res[1], res[2]
        if on_screen:                             # 화면 하나 끝날 때마다 그 화면의 원시 행을 흘려보낸다(라이브)
            try:
                rows = finalize_mod.parse_rows(raw) or []   # 단일 파서 재사용 — 게이트 전 원시값
            except Exception:
                rows = []
            on_screen(i, rows)
    if NP > 1 and n > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=NP) as ex:
            futs = {ex.submit(_vision, b64, mode): i for i, b64 in enumerate(images)}
            for fut in as_completed(futs):        # 완료되는 대로 방출 — 최종 조립은 raws[i]로 순서 보존
                _done(futs[fut], fut.result())
    else:
        for i, b64 in enumerate(images):
            _done(i, _vision(b64, mode))
    if on_stage:                                  # 비전추출 끝 → 종합·검증 단계로(라이브 상태 전환)
        on_stage("finalizing")
    screens = [{"file": f"img{i + 1}", "raw": raw, "evidence": evid[i], "geom": geoms[i],
                "fname": (names[i] if names and i < len(names) else None)}
               for i, raw in enumerate(raws)]
    # 증권사는 **요청마다 검색으로 다시 확정한다**(디스크 캐시 없음, `resolve_broker` docstring).
    # 예전엔 검색 순위가 호출마다 달라 같은 스크린샷이 다른 증권사를 냈고 캐시가 그걸 고정했다.
    # 그 흔들림의 원인은 최빈-토큰 폴백이었고 이미 제거됐다 — 지금은 LLM이 검색 텍스트를 읽고
    # 확인 불가면 UNKNOWN이라 **캐시 없이도 결정적이다**(실측 2026-08-07: 브랜드 4종 각 6/6).
    fin = finalize_mod.finalize(screens, use_llm=use_llm)   # holdings(정규화) + gate(대조 리포트)
    rows = enrich(fin["holdings"], capture_dt)    # 심볼 해석 + 수량 사다리 + 가격(_file은 화면단위 게이트에 필요)
    for h in rows:
        h.pop("_file", None)
    update_watchlist(rows)
    result = {"holdings": rows, "gate": fin["gate"], "screens": fin["screens"],
              "seconds": round(time.time() - t0, 1), "engine": eng,
              "model": MODEL if mode == "vlm" else f"OCR+기하({os.environ.get('OCR_ENGINE', 'rapidocr')})",
              "captureDateTime": capture_dt.isoformat(),
              "captureSource": capture_src}
    # 개발 캡처 저장 — 화면별 raw(모델 원문)와 **업로드 파일명**을 함께 남겨 오류를 사후 분석한다.
    # (names 저장이 없어서 08-10 폰 케이스에서 '파일명이 안 왔는지'를 사후 판별 못 했다.)
    save_capture_batch(images, {**result, "raws": raws, "names": list(names or [])})
    return result


def _batch_run(jid, images, capture_dt, engine=None, names=None):
    seed = lambda: {"done": 0, "total": len(images), "rows": [], "stage": "extracting"}
    def on_screen(idx, rows):                     # 화면 완료 시 진행 상황에 원시 행 누적(폴링이 읽어감)
        with _JOBS_LOCK:
            j = _JOBS.get(jid)
            if not j or j.get("status") != "pending":
                return
            p = j.setdefault("progress", seed())
            for r in rows:                        # 행→소스 이미지 귀속(라이브 UI가 "이미지 N"으로 표시)
                r["_img"] = idx + 1
            p["rows"].extend(rows)
            p["done"] += 1
    def on_stage(stage):                          # 단계 전환(extracting → finalizing) 라이브 반영
        with _JOBS_LOCK:
            j = _JOBS.get(jid)
            if j and j.get("status") == "pending":
                j.setdefault("progress", seed())["stage"] = stage
    try:
        res = extract_batch(images, capture_dt, on_screen=on_screen, on_stage=on_stage,
                            engine=engine, names=names)
        with _JOBS_LOCK:
            _JOBS[jid] = {"status": "done", "result": res, "ts": time.time()}
    except Exception as e:
        with _JOBS_LOCK:
            _JOBS[jid] = {"status": "error", "error": str(e), "ts": time.time()}


def submit_batch(body):
    images = body.get("images") or []
    names = body.get("names") or []          # 업로드 원본 파일명(선택) — 증권사 앱·캡처시각 근거
    capture_dt = parse_capture(body)
    jid = os.urandom(8).hex()
    with _JOBS_LOCK:
        _JOBS[jid] = {"status": "pending", "ts": time.time(),
                      "progress": {"done": 0, "total": len(images), "rows": [], "stage": "extracting"}}
    threading.Thread(target=_batch_run,
                     args=(jid, images, capture_dt, body.get("engine"), names), daemon=True).start()
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
    # 진행 중: 화면별로 도착한 원시 행을 함께 준다 → 앱이 라이브로 그린다(하위호환: 필드 추가만)
    return {"status": "pending", "progress": j.get("progress", {"done": 0, "total": 0, "rows": [], "stage": "extracting"})}

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
            # 앱 본체. 같은 오리진에서 API도 나가므로 앱은 pf_agent_url 없이도 붙는다.
            try:
                b = open(INDEX_PATH, "rb").read()
            except Exception:
                b = PAGE.encode()          # index.html이 없으면 기존 MVP 페이지로 폴백
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8"); self._cors()
            # 앱은 자주 고쳐 배포한다 — 캐시된 구버전이 새 계약(names 동봉 등)을 빠뜨리면
            # 서버 쪽 근거 축이 조용히 죽는다(실측 08-10 의심 사례) → 매 방문 재검증 강제.
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        elif self.path.split("?")[0] == "/mvp":
            b = PAGE.encode(); self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8"); self._cors()
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        elif self.path.split("?")[0] == "/manifest.webmanifest":
            # 홈화면 설치(PWA) — 아이콘 탭으로 앱이 열린다. 서비스워커는 두지 않는다:
            # 앱을 서빙하는 주체가 이 에이전트라 서버가 꺼지면 어차피 못 쓴다(오프라인 캐시가
            # 주는 이득이 없고, 낡은 번들을 물고 있을 위험만 생긴다).
            b = json.dumps({
                "name": "포트폴리오 리밸런서", "short_name": "리밸런서",
                "start_url": "/", "scope": "/", "display": "standalone",
                "background_color": "#ffffff", "theme_color": "#4f5bd5",
                "icons": [{"src": "data:image/svg+xml,"
                           "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
                           "%3Crect width='64' height='64' rx='12' fill='%234f5bd5'/%3E"
                           "%3Ctext x='32' y='44' font-size='36' text-anchor='middle'%3E%F0%9F%93%8A"
                           "%3C/text%3E%3C/svg%3E",
                           "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
            }, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8"); self._cors()
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        elif self.path.split("?")[0] == "/health":
            # 프런트의 경로 셀렉터가 읽는다 — **고를 수 없는 경로를 고르게 두지 않는다.**
            # (라이브는 시스템 python3라 rapidocr가 없을 수 있고, 엣지 기동엔 ollama가 없다)
            b = json.dumps({"ok": True, "default": DEFAULT_ENGINE,
                            "engines": engine_status()}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8"); self._cors()
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
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
                result = reprice(data.get("holdings", []), parse_capture(data), capture_source(data))
            else:
                result = extract(data["image"], parse_capture(data))
        except Exception as e:
            result = {"error": str(e)}
        b = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8"); self._cors()
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass

def serve():
    """기동 진입점. `__main__`과 APK(android_main)가 **같은 경로**를 쓴다."""
    print(f"에이전트 서버 → http://{BIND}:{PORT}  (모델 {MODEL})")
    print(f"· 시세 데이터 {DATA_DIR}  갱신시각(UTC) {FETCH_TIMES_UTC}")
    threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()


if __name__ == "__main__":
    serve()
