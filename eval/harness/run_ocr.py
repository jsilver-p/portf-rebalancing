#!/usr/bin/env python3
"""OCR+기하 추출 실행기 — `run_extract.py`(비전 LLM)의 **산출 규약을 그대로** 따른다.

각 스크린샷 → `agent/ocr.py`(박스) → `agent/bind.py`(11칸 배열) → `{"image","raw"}` JSON.
`raw`가 비전 원문이 있던 자리를 차지하므로 **`parity.py`를 고치지 않고** 같은 채점기로 잰다.
바꾼 것은 `raw`를 만드는 방법 하나뿐 — 그래서 두 경로의 점수가 같은 기준으로 비교된다.

사용:  python3 eval/harness/run_ocr.py [이미지명 필터...]
환경:  SHOTS(기본 test-fixtures/screenshots) · OUT_TAG · OCR_ENGINE(rapidocr|tesseract)
"""
import json, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "agent"))
import ocr                                              # noqa: E402
import bind                                             # noqa: E402

SHOTS = os.environ.get("SHOTS", os.path.join(ROOT, "test-fixtures", "screenshots"))
TAG = os.environ.get("OUT_TAG", "")


def main():
    only = sys.argv[1:] or None
    engine = os.environ.get("OCR_ENGINE", "rapidocr")
    outdir = os.path.join(ROOT, "eval/results", f"ocr_{engine}{TAG}")
    os.makedirs(outdir, exist_ok=True)
    imgs = sorted(f for f in os.listdir(SHOTS) if f.lower().endswith((".jpg", ".png")))
    if only:
        imgs = [f for f in imgs if any(o in f for o in only)]
    t_all = time.time()
    for f in imgs:
        t0 = time.time()
        try:
            boxes = ocr.recognize(os.path.join(SHOTS, f))
            rows = bind.bind(boxes)
            raw = json.dumps(rows, ensure_ascii=False)
            err = None
        except Exception as e:
            boxes, rows, raw, err = [], [], "[]", f"{type(e).__name__}: {e}"
        dt = time.time() - t0
        json.dump({"image": f, "seconds": round(dt, 2), "raw": raw,
                   "engine": engine, "n_boxes": len(boxes), "n_rows": len(rows),
                   "error": err},
                  open(os.path.join(outdir, f + ".json"), "w"), ensure_ascii=False, indent=2)
        print(f"  {dt:5.2f}s  {len(boxes):3d} boxes → {len(rows):2d} rows  {f}"
              + (f"  ERROR {err}" if err else ""), flush=True)
    print(f"DONE -> {outdir}  (wall {time.time() - t_all:.1f}s, engine={engine})")


if __name__ == "__main__":
    main()
