#!/usr/bin/env python3
"""OCR 어댑터 — 스크린샷 → 텍스트 박스. **엔진 교체 지점**.

계약(이것만이 아래 단계와의 접점이다):
    recognize(img) -> [{"text": str, "x": int, "y": int, "w": int, "h": int, "conf": float}]
  · x,y = 박스 좌상단, 원본 이미지 픽셀 좌표. w,h = 축정렬 사각형 크기.
  · 정렬 순서 보장 없음 — 줄 그룹핑은 bind.py의 일이다.

엔진은 `OCR_ENGINE`으로 고른다:
  rapidocr (기본) — PP-OCRv5 korean rec(12.9MB) + PP-OCRv6 det. 검증·데스크톱·Termux(glibc).
  tesseract       — `tesseract -l kor+eng --psm 6 TSV`. Termux 배포 rung(pkg 하나로 끝).
  APK(ML Kit)     — Kotlin 쪽이 **이 계약의 JSON을 그대로** 넘긴다. 파이썬 엔진 불필요.

왜 어댑터인가: 엔진은 플랫폼마다 다르지만(검증=rapidocr, 폰=ML Kit) **bind.py 이하는 하나여야
한다.** 엔진을 갈아도 기하 바인더·게이트가 재검증 대상이 되지 않도록 계약을 여기서 끊는다.
"""
import json, os, subprocess, tempfile

ENGINE = os.environ.get("OCR_ENGINE", "rapidocr")

_rapid = None


def _rapidocr_engine():
    """RapidOCR 싱글턴 — 모델 로드가 비싸다(요청마다 만들면 안 된다)."""
    global _rapid
    if _rapid is None:
        from rapidocr import RapidOCR, EngineType, LangRec, ModelType, OCRVersion
        _rapid = RapidOCR(params={
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Rec.lang_type": LangRec.KOREAN,     # 한글 종목명은 열린 어휘 — 한국어 rec 필수
            "Rec.model_type": ModelType.MOBILE,  # server 미지원(모바일 rung과 같은 모델을 검증한다)
            "Rec.ocr_version": OCRVersion.PPOCRV5,
        })
    return _rapid


def _box_xywh(box):
    """4점 폴리곤 → 축정렬 (x, y, w, h). 스크린샷은 회전이 없어 손실이 없다."""
    xs = [float(p[0]) for p in box]
    ys = [float(p[1]) for p in box]
    x0, y0 = min(xs), min(ys)
    return int(round(x0)), int(round(y0)), int(round(max(xs) - x0)), int(round(max(ys) - y0))


def _recognize_rapidocr(img):
    # use_cls=False 는 성능 튜닝이 아니라 **오류 원인 제거**다. 방향 분류기(cls)는 단어마다
    # 180° 회전 여부를 독립적으로 판정하는데, 스크린샷에서 오판이 난다(측정: '엔비디아'가
    # cls ON에서 '{이그A' c=0.41, cls OFF에서 '엔비디아' c=1.00 — 그리고 같은 크롭을 손으로
    # 180° 돌리면 '{이그A' c=0.41이 그대로 재현된다). 스크린샷은 회전이 없다는 불변식이
    # 절대적이므로 이 단계 자체를 끈다(임계값으로 덮지 않는다).
    res = _rapidocr_engine()(img, use_cls=False)
    if not res or not res.txts:
        return []
    out = []
    for box, txt, score in zip(res.boxes, res.txts, res.scores):
        x, y, w, h = _box_xywh(box)
        out.append({"text": txt, "x": x, "y": y, "w": w, "h": h, "conf": float(score)})
    return out


def _recognize_tesseract(img):
    """TSV 출력을 파싱. `-l kor+eng`: 한글 종목명 + 라틴 숫자를 한 패스로.
    --psm 6(균일 블록)은 표 화면에서 줄 구조를 가장 안정적으로 낸다."""
    path, tmp = img, None
    if not isinstance(img, str):
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.write(img if isinstance(img, bytes) else img.tobytes())
        tmp.close()
        path = tmp.name
    try:
        p = subprocess.run(
            ["tesseract", path, "stdout", "-l", os.environ.get("TESS_LANG", "kor+eng"),
             "--psm", os.environ.get("TESS_PSM", "6"), "tsv"],
            capture_output=True, text=True, timeout=180)
        out = []
        for line in p.stdout.splitlines()[1:]:
            f = line.split("\t")
            if len(f) < 12 or f[11].strip() == "":
                continue
            try:
                conf = float(f[10])
            except ValueError:
                continue
            if conf < 0:                     # -1 = 텍스트 없는 레이아웃 행
                continue
            out.append({"text": f[11], "x": int(f[6]), "y": int(f[7]),
                        "w": int(f[8]), "h": int(f[9]), "conf": conf / 100.0})
        return out
    finally:
        if tmp:
            os.unlink(tmp.name)


def _recognize_mlkit(img):
    """APK 경로 — Chaquopy로 Kotlin의 MlKitOcr를 부른다(계약 JSON을 그대로 받는다).

    ML Kit은 Block > Line > Element 계층이고 RapidOCR의 구절 박스에 가장 가까운 건 Line이다.
    이 **박스 단위 차이가 엔진 스왑의 유일한 실질 위험**이라 노브로 둔다(기기에서 재서 고정).
    """
    from java import jarray, jbyte, jclass          # Chaquopy 런타임에만 존재
    data = img if isinstance(img, bytes) else open(img, "rb").read()
    gran = os.environ.get("OCR_MLKIT_GRANULARITY", "line")
    raw = jclass("com.portfrebalance.edge.MlKitOcr").recognize(jarray(jbyte)(data), gran)
    return json.loads(str(raw))


def recognize(img, engine=None):
    """img: 파일 경로(str) · 이미지 바이트 · numpy 배열. → 계약 리스트."""
    eng = engine or ENGINE
    if eng == "rapidocr":
        return _recognize_rapidocr(img)
    if eng == "tesseract":
        return _recognize_tesseract(img)
    if eng == "mlkit":
        return _recognize_mlkit(img)
    raise ValueError(f"알 수 없는 OCR_ENGINE: {eng}")


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        boxes = recognize(p)
        print(f"== {os.path.basename(p)}  ({len(boxes)} boxes, engine={ENGINE})")
        for b in sorted(boxes, key=lambda b: (b["y"], b["x"])):
            print(f"  x={b['x']:5d}+{b['w']:<4d} y={b['y']:5d}+{b['h']:<3d} "
                  f"c={b['conf']:.2f}  {b['text']!r}")
