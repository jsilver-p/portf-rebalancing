#!/usr/bin/env python3
"""Gate 1 — 로컬 비전 모델 추출 실행기.
각 스크린샷 + 앱의 실제 프로덕션 프롬프트를 Ollama 모델에 보내고 원출력·지연을 저장한다.
스크린샷에 맞춘 하드코딩 없음: 모든 이미지에 동일한 일반 프롬프트만 사용."""
import base64, json, sys, time, os, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHOTS = os.path.join(ROOT, "test-fixtures", "screenshots")
PROMPT_FILE = os.environ.get("PROMPT_FILE", os.path.join(ROOT, "eval/harness/prompt.txt"))
PROMPT = open(PROMPT_FILE).read().strip()
TAG = os.environ.get("OUT_TAG", "")  # 결과 디렉토리 접미사 (프롬프트 변형 구분)
OLLAMA = "http://127.0.0.1:11434/api/generate"

def call(model, img_path):
    b64 = base64.b64encode(open(img_path, "rb").read()).decode()
    body = json.dumps({
        "model": model, "prompt": PROMPT, "images": [b64],
        "stream": False, "options": {"temperature": 0, "num_ctx": 8192},
    }).encode()
    t0 = time.time()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        out = json.loads(r.read())
    return out.get("response", ""), time.time() - t0

def main():
    model = sys.argv[1]
    only = sys.argv[2:] if len(sys.argv) > 2 else None
    outdir = os.path.join(ROOT, "eval/results", model.replace(":", "_").replace("/", "_") + TAG)
    os.makedirs(outdir, exist_ok=True)
    imgs = sorted(f for f in os.listdir(SHOTS) if f.lower().endswith((".jpg", ".png")))
    if only:
        imgs = [f for f in imgs if any(o in f for o in only)]
    for f in imgs:
        print(f"[{model}] {f} ...", flush=True)
        try:
            resp, dt = call(model, os.path.join(SHOTS, f))
        except Exception as e:
            resp, dt = f"__ERROR__ {e}", -1
        json.dump({"image": f, "seconds": round(dt, 1), "raw": resp},
                  open(os.path.join(outdir, f + ".json"), "w"), ensure_ascii=False, indent=2)
        print(f"   {dt:.1f}s  chars={len(resp)}", flush=True)
    print(f"DONE -> {outdir}")

if __name__ == "__main__":
    main()
