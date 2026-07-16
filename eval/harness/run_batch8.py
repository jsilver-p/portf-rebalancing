#!/usr/bin/env python3
"""8장 스크린샷 전부를 7B+prompt2로 비전추출해 원본 JSON을 저장(gitignored).
finalize(게이트·정규화) 검증용 입력. CPU라 느림 — 백그라운드로 순차 실행."""
import base64, glob, json, os, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
MODEL = os.environ.get("MODEL", "qwen2.5vl:7b")
OLLAMA = os.environ.get("OLLAMA", "http://127.0.0.1:11434") + "/api/generate"
PROMPT = open(os.path.join(ROOT, "eval/harness/prompt2.txt")).read().strip()
SHOTS = os.path.join(ROOT, "test-fixtures/screenshots")
OUT = os.path.join(ROOT, "eval/results/batch8")
os.makedirs(OUT, exist_ok=True)

imgs = sorted(glob.glob(os.path.join(SHOTS, "*.jpg")))
print(f"{len(imgs)}장 추출 시작 (model={MODEL})", flush=True)
for p in imgs:
    name = os.path.basename(p)
    outp = os.path.join(OUT, name + ".json")
    if os.path.exists(outp):
        print(f"· skip(cached) {name}", flush=True)
        continue
    b64 = base64.b64encode(open(p, "rb").read()).decode()
    req = json.dumps({"model": MODEL, "prompt": PROMPT, "images": [b64],
                      "stream": False, "options": {"temperature": 0, "num_ctx": 8192}}).encode()
    t0 = time.time()
    r = urllib.request.Request(OLLAMA, data=req, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=3600) as resp:
        out = json.loads(resp.read())
    secs = round(time.time() - t0, 1)
    raw = out.get("response", "")
    json.dump({"image": name, "seconds": secs, "raw": raw},
              open(outp, "w"), ensure_ascii=False, indent=2)
    print(f"· done {name}  {secs}s  ({len(raw)} chars)", flush=True)
print("전체 완료", flush=True)
