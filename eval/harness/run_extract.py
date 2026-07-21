#!/usr/bin/env python3
"""Gate 1 — 로컬 비전 모델 추출 실행기.
각 스크린샷 + 앱의 실제 프로덕션 프롬프트를 Ollama 모델에 보내고 원출력·지연을 저장한다.
스크린샷에 맞춘 하드코딩 없음: 모든 이미지에 동일한 일반 프롬프트만 사용."""
import base64, json, sys, time, os, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHOTS = os.environ.get("SHOTS", os.path.join(ROOT, "test-fixtures", "screenshots"))
PROMPT_FILE = os.environ.get("PROMPT_FILE", os.path.join(ROOT, "eval/harness/prompt.txt"))
PROMPT = open(PROMPT_FILE).read().strip()
TAG = os.environ.get("OUT_TAG", "")  # 결과 디렉토리 접미사 (프롬프트 변형 구분)
NUM_CTX = int(os.environ.get("NUM_CTX", "8192"))   # 컨텍스트 — 조밀·긴 화면은 이미지 토큰이 크다
OLLAMA = os.environ.get("OLLAMA", "http://127.0.0.1:11434") + "/api/generate"
NP = int(os.environ.get("NP", "1"))                # 동시 요청 수 — ollama의 OLLAMA_NUM_PARALLEL과 일치시킬 것
# SYSTEM_MODE=1: 지시문을 system으로 보내 템플릿상 이미지보다 앞에 배치 → 슬롯 KV prefix 재사용(E2)
SYSTEM_MODE = os.environ.get("SYSTEM_MODE", "") == "1"

def call(model, img_path):
    b64 = base64.b64encode(open(img_path, "rb").read()).decode()
    payload = {
        "model": model, "prompt": PROMPT, "images": [b64],
        "stream": False, "keep_alive": -1, "options": {"temperature": 0, "num_ctx": NUM_CTX},
    }
    if SYSTEM_MODE:
        # 빈 prompt는 ollama의 '모델 로드 핑' 특수 케이스라 즉시 빈 응답 — 최소 지시문 필수.
        # 지시문 본문은 system으로 → 템플릿상 이미지보다 앞 = 요청 간 KV prefix 재사용.
        payload["system"], payload["prompt"] = PROMPT, "위 규칙대로 이 화면에서 추출하라."
    body = json.dumps(payload).encode()
    t0 = time.time()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        out = json.loads(r.read())
    # 병목 분해용 계측 — prefill(prompt_eval)/decode(eval) 토큰 수·시간(ns)
    metrics = {k: out.get(k) for k in
               ("prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration")}
    return out.get("response", ""), time.time() - t0, metrics

def main():
    model = sys.argv[1]
    only = sys.argv[2:] if len(sys.argv) > 2 else None
    outdir = os.path.join(ROOT, "eval/results", model.replace(":", "_").replace("/", "_") + TAG)
    os.makedirs(outdir, exist_ok=True)
    imgs = sorted(f for f in os.listdir(SHOTS) if f.lower().endswith((".jpg", ".png")))
    if only:
        imgs = [f for f in imgs if any(o in f for o in only)]
    t0 = time.time()

    def one(f):
        print(f"[{model}] {f} ...", flush=True)
        try:
            resp, dt, metrics = call(model, os.path.join(SHOTS, f))
        except Exception as e:
            resp, dt, metrics = f"__ERROR__ {e}", -1, {}
        json.dump({"image": f, "seconds": round(dt, 1), "raw": resp,
                   "num_ctx": NUM_CTX, "prompt_file": os.path.basename(PROMPT_FILE),
                   "metrics": metrics},
                  open(os.path.join(outdir, f + ".json"), "w"), ensure_ascii=False, indent=2)
        print(f"   {f}  {dt:.1f}s  chars={len(resp)}", flush=True)

    if NP > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=NP) as ex:
            list(ex.map(one, imgs))
    else:
        for f in imgs:
            one(f)
    print(f"DONE -> {outdir}  (wall {time.time() - t0:.0f}s, NP={NP})")

if __name__ == "__main__":
    main()
