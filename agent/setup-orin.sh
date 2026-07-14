#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# ORIN(Jetson) 포트폴리오 추출 에이전트 — 원샷 셋업.
# 한 번 실행하면: ollama(GPU/CUDA) + 비전모델 + cloudflared(arm64) + Pillow +
#                데이터 디렉터리 + 레포를 준비한다. 재실행 안전(idempotent).
#
# 대상: Jetson Orin (AGX/NX/Nano), JetPack 6 (L4T r36.x), aarch64.
# 실행:  bash agent/setup-orin.sh
# 조정:  MODEL, REPO_DIR, DATA_DIR 환경변수로 덮어쓰기 가능.
#
# 근거(2026-07): ollama 공식 install.sh는 Jetson을 감지해 CUDA로 설치(GPU 가속).
#               cloudflared는 pkg.cloudflare.com에 arm64가 없어 GitHub 릴리스
#               바이너리(cloudflared-linux-arm64)를 직접 설치한다.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

MODEL="${MODEL:-qwen2.5vl:7b}"
REPO_URL="${REPO_URL:-https://github.com/jsilver-p/portf-rebalancing.git}"
REPO_DIR="${REPO_DIR:-$HOME/portf-rebalancing}"
DATA_DIR="${DATA_DIR:-$HOME/portf-agent/data}"
CF_BIN=/usr/local/bin/cloudflared

log(){ printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
have(){ command -v "$1" >/dev/null 2>&1; }
ollama_up(){ curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; }

start_ollama(){
  ollama_up && return 0
  sudo systemctl enable --now ollama 2>/dev/null || nohup ollama serve >/tmp/ollama.log 2>&1 &
  for _ in $(seq 1 20); do ollama_up && return 0; sleep 1; done
  echo "⚠ ollama 기동 확인 실패 — /tmp/ollama.log 확인"; return 1
}

# 0) 환경 점검 ---------------------------------------------------------------
log "환경 점검"
[ "$(uname -m)" = aarch64 ] || echo "⚠ aarch64가 아님($(uname -m)) — Jetson Orin이 맞는지 확인"
if have nvidia-smi || [ -e /usr/bin/tegrastats ]; then
  echo "· Jetson/GPU 감지됨"
else
  echo "⚠ GPU 미감지(tegrastats/nvidia-smi 없음) — CPU로 돌면 이미지당 수 분 걸림"
fi

# 1) 기본 도구 --------------------------------------------------------------
have git && have curl || { log "git/curl 설치"; sudo apt-get update -qq && sudo apt-get install -y git curl; }

# 2) ollama (Jetson CUDA) ---------------------------------------------------
if have ollama; then
  echo "· ollama 이미 설치됨: $(ollama --version 2>/dev/null | head -1)"
else
  log "ollama 설치 (Jetson CUDA)"
  curl -fsSL https://ollama.com/install.sh | sh
fi
log "ollama 기동"; start_ollama

# 3) 비전모델 pull ----------------------------------------------------------
log "모델 pull: $MODEL  (7B ≈ 6GB, 최초 1회 다운로드)"
ollama pull "$MODEL"

# 4) cloudflared (arm64 바이너리) -------------------------------------------
if [ -x "$CF_BIN" ]; then
  echo "· cloudflared 이미 설치됨: $($CF_BIN --version 2>/dev/null | head -1)"
else
  log "cloudflared 설치 (arm64 릴리스 바이너리)"
  tmp=$(mktemp)
  curl -fsSL -o "$tmp" \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64
  sudo install -m0755 "$tmp" "$CF_BIN"; rm -f "$tmp"
  echo "· 설치됨: $($CF_BIN --version 2>/dev/null | head -1)"
fi

# 5) Python 의존성(Pillow — EXIF 캡처시각용, 나머지는 표준 라이브러리) ------
log "Python 의존성(Pillow)"
if python3 -c "import PIL" 2>/dev/null; then
  echo "· Pillow 이미 있음"
else
  sudo apt-get update -qq && sudo apt-get install -y python3-pil \
    || pip3 install --user pillow
fi

# 6) 데이터 디렉터리(시세·캐시 — 레포 밖) -----------------------------------
mkdir -p "$DATA_DIR"; echo "· DATA_DIR=$DATA_DIR"

# 7) 레포 ------------------------------------------------------------------
log "레포 준비: $REPO_DIR"
if [ -d "$REPO_DIR/.git" ]; then
  git -C "$REPO_DIR" pull --ff-only || echo "⚠ git pull 실패(로컬 변경?) — 수동 확인"
else
  git clone "$REPO_URL" "$REPO_DIR"
fi

# 8) GPU 자기점검(선택, 비치명적) -------------------------------------------
log "GPU 자기점검"
ollama run "$MODEL" "OK" >/dev/null 2>&1 || true
ollama ps 2>/dev/null || true
echo "  ↑ PROCESSOR 열이 'GPU'면 가속 정상(초 단위). 'CPU'면 JetPack6/CUDA 확인 필요."

# 완료 ---------------------------------------------------------------------
log "셋업 완료 ✅"
cat <<EOF
실행:
  MODEL=$MODEL DATA_DIR=$DATA_DIR bash $REPO_DIR/agent/run-agent.sh

  → 로컬 서버 http://0.0.0.0:8899
  → run-agent.sh가 공개 https 터널 URL을 출력하니, 그 값을 앱의
    '🔗 에이전트 연결' 입력칸에 붙여넣으면 폰에서 추출/재평가 가능.
EOF
