#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# ANDROID(Termux) 포트폴리오 추출 에이전트 — 원샷 셋업. 재실행 안전(idempotent).
#
# setup-orin.sh와의 결정적 차이: **모델을 반입하지 않는다.**
#   Orin 경로 : ollama + 비전 LLM 4.6GB  → 폰에선 메모리·속도·배포 전부 불가
#   여기      : OCR 12.9MB + 결정론 코드 → 신경망 LLM 0개
# 근거는 docs/edge-ai-capability.md (Play 4GB/200MB 한도, Adreno 740 미검증,
# Termux ollama CPU 전용, prefill 10~30분/장).
#
# Termux는 **F-Droid/GitHub 배포판**이어야 한다(Play 스토어판은 구식이라 pkg가 깨진다).
# 실행:  bash agent/setup-termux.sh   (레포를 클론한 뒤 그 안에서)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${DATA_DIR:-$HOME/portf-agent/data}"

log(){ printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
have(){ command -v "$1" >/dev/null 2>&1; }

# 0) 환경 점검 --------------------------------------------------------------
log "환경 점검"
if [ -n "${TERMUX_VERSION:-}" ]; then
  echo "· Termux $TERMUX_VERSION"
else
  echo "⚠ Termux가 아닌 것 같다 — 이 스크립트는 Android/Termux 전용"
fi
echo "· arch $(uname -m)"

# 1) 패키지 -----------------------------------------------------------------
# Pillow는 **필수**다. 없으면 EXIF 캡처시각을 못 읽는다(수량 사다리 T4의 기준시각).
log "패키지 설치"
pkg update -y >/dev/null 2>&1 || true
pkg install -y python python-pillow git libjpeg-turbo libpng

# 2) OCR 엔진 ---------------------------------------------------------------
# 검증(Orin)은 RapidOCR(PP-OCRv5 korean, onnxruntime)로 파리티를 통과했다.
# Termux는 bionic libc라 manylinux 휠이 안 맞을 수 있어 tesseract를 폴백 rung으로 둔다.
# 어느 쪽이든 agent/ocr.py의 계약([{text,x,y,w,h,conf}])만 만족하면 bind.py 이하는 동일하다.
log "OCR 엔진"
if python3 -c "import rapidocr" 2>/dev/null; then
  echo "· rapidocr 이미 있음"
elif pip install rapidocr onnxruntime 2>/dev/null; then
  echo "· rapidocr 설치됨 (권장 — 파리티 검증된 엔진)"
  export OCR_ENGINE=rapidocr
else
  echo "· rapidocr 불가(bionic 휠 부재로 추정) → tesseract 폴백"
  pkg install -y tesseract
  # 한국어 traineddata는 아키텍처 무관 — 없으면 받아서 tessdata에 둔다.
  TESSDIR="$(dirname "$(command -v tesseract)")/../share/tessdata"
  mkdir -p "$TESSDIR"
  [ -f "$TESSDIR/kor.traineddata" ] || \
    curl -fsSL -o "$TESSDIR/kor.traineddata" \
      https://github.com/tesseract-ocr/tessdata/raw/main/kor.traineddata
  echo "· tesseract + kor 준비됨"
  export OCR_ENGINE=tesseract
fi

# 3) 데이터 디렉터리(시세·캐시 — 레포 밖) ------------------------------------
mkdir -p "$DATA_DIR"; echo "· DATA_DIR=$DATA_DIR"

# 4) 배터리 최적화 --------------------------------------------------------
# Android doze가 장시간 추출을 죽인다. wake-lock은 기동 스크립트가 잡지만 여기서도 안내.
have termux-wake-lock && echo "· termux-wake-lock 사용 가능" \
  || echo "⚠ termux-api 미설치 — 'pkg install termux-api' 권장(doze 방어)"

log "셋업 완료 ✅"
cat <<EOF
실행:
  TUNNEL=0 EXTRACT=ocr OCR_ENGINE=${OCR_ENGINE:-rapidocr} bash $REPO_DIR/agent/run-agent.sh

  → 폰 브라우저에서  http://localhost:8899/  를 연다.
    앱과 API가 **같은 오리진**이라 '에이전트 연결' URL을 입력할 필요가 없다(자동 채택).
    같은 Wi-Fi의 다른 기기에서 보려면 http://<폰 IP>:8899/

반입한 모델: 없음. OCR 12.9MB만 내려받는다(최초 1회, 자동).
EOF
