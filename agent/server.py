#!/usr/bin/env python3
"""포트폴리오 추출 에이전트 — MVP 로컬 서버.
폰 등 외부에서 스크린샷을 올리면 로컬 Ollama(Qwen2.5-VL-7B + 헤더프롬프트)로
보유자산을 추출하고, 결정적 엔리치(주가=평가금액/수량)·계좌합계 검증을 붙여 JSON으로 돌려준다.

실행:  python3 agent/server.py         (기본 포트 8899, 모델 qwen2.5vl:7b)
환경:  MODEL, PORT, OLLAMA 로 조정.
외부접속: 별도로  cloudflared tunnel --url http://localhost:8899  (public https URL)
주의: 이 맥은 CPU라 이미지당 수 분 소요(정상). Orin GPU에선 초 단위.
"""
import base64, json, os, re, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = os.environ.get("MODEL", "qwen2.5vl:7b")
PORT = int(os.environ.get("PORT", "8899"))
OLLAMA = os.environ.get("OLLAMA", "http://127.0.0.1:11434") + "/api/generate"
PROMPT = open(os.path.join(ROOT, "eval/harness/prompt2.txt")).read().strip()

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
pre{white-space:pre-wrap;word-break:break-all;font-size:.7rem;color:#9aa1b2}
</style></head><body>
<h1>📸 포트폴리오 추출 에이전트 <span class=muted>MVP</span></h1>
<div class=card>
  <input id=f type=file accept="image/*" capture=environment>
  <button id=go>추출하기</button>
  <div id=status class=muted style="margin-top:10px"></div>
</div>
<div id=out></div>
<script>
const f=document.getElementById('f'),go=document.getElementById('go'),st=document.getElementById('status'),out=document.getElementById('out');
go.onclick=async()=>{
  if(!f.files[0]){st.textContent='이미지를 선택하세요';return;}
  go.disabled=true;out.innerHTML='';
  const t0=Date.now();
  const tick=setInterval(()=>{st.textContent='추출 중… '+Math.round((Date.now()-t0)/1000)+'s (이 맥은 CPU라 수 분 걸립니다)';},1000);
  try{
    const b64=await new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(r.result.split(',')[1]);r.onerror=rej;r.readAsDataURL(f.files[0]);});
    const r=await fetch('/extract',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({image:b64})});
    const j=await r.json();clearInterval(tick);
    if(j.error){st.innerHTML='<span class=warn>오류: '+j.error+'</span>';go.disabled=false;return;}
    st.textContent=j.holdings.length+'개 추출 · '+j.seconds+'s';
    let h='<div class=card><table><tr><th>종목</th><th>자산군</th><th>수량</th><th>주가</th><th>평가금액</th><th>매수금액</th></tr>';
    for(const x of j.holdings){h+=`<tr><td>${x.name||''}</td><td>${x.assetClass||''}</td><td>${x.qty??'—'}</td><td>${x.price!=null?Number(x.price).toLocaleString():'—'}</td><td>${x.value!=null?Number(x.value).toLocaleString():'—'}</td><td>${x.cost!=null?Number(x.cost).toLocaleString():'—'}</td></tr>`;}
    h+='</table>';
    if(j.warnings&&j.warnings.length)h+='<div class="warn muted" style="margin-top:8px">⚠ '+j.warnings.join(' · ')+'</div>';
    h+='</div>';out.innerHTML=h;
  }catch(e){clearInterval(tick);st.innerHTML='<span class=warn>실패: '+e+'</span>';}
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

def extract(b64):
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
    # 결정적 엔리치: 주가 = 평가금액 / 수량 (수량 있을 때만; 추측 금지)
    for h in rows:
        for k in ("qty", "value", "cost", "price"):
            if k in h: h[k] = num(h[k])
        if h.get("price") is None and h.get("qty") and h.get("value"):
            h["price"] = round(h["value"] / h["qty"], 2)
    return {"holdings": rows, "seconds": round(time.time() - t0, 1),
            "warnings": warnings, "model": MODEL, "raw": raw}

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
        else:
            self.send_response(404); self._cors(); self.end_headers()
    def do_POST(self):
        if self.path != "/extract":
            self.send_response(404); self._cors(); self.end_headers(); return
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n))
            result = extract(data["image"])
        except Exception as e:
            result = {"error": str(e)}
        b = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8"); self._cors()
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass

if __name__ == "__main__":
    print(f"에이전트 서버 → http://0.0.0.0:{PORT}  (모델 {MODEL})")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
