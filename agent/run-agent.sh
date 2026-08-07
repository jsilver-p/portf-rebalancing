#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# 에이전트 서버 + cloudflared 터널을 켜고 공개 URL을 출력한다. Ctrl-C로 정리.
# 셋업은 setup-orin.sh가 선행. 조정: MODEL, DATA_DIR, PORT, REPO_DIR.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # 이 스크립트가 든 레포
export MODEL="${MODEL:-qwen2.5vl:3b-ft3-q8}"
export DATA_DIR="${DATA_DIR:-$HOME/portf-agent/data}"
export PORT="${PORT:-8899}"
export NP="${NP:-2}"                     # 동시 비전 요청 수 = OLLAMA_NUM_PARALLEL
export EXTRACT="${EXTRACT:-vlm}"         # vlm(기본·라이브) | ocr(엣지 — 모델·ollama 불필요)
# 업로드 원본 백업 — **기동 경로가 소유한다.** 예전엔 이 스크립트가 넘기지 않아서 외부에서
# 안 주면 조용히 꺼졌다(실측: 스파이크 서버를 직접 띄웠더니 하루치 업로드가 백업 없이 날아감).
# 관측 도구는 기본이 켬이고 끄는 쪽이 명시적이어야 한다.
# CAPTURES_DIR은 기본값(DATA_DIR/../captures)이 어느 DATA_DIR에서도 ~/portf-agent/captures로
# 수렴한다 — 라이브·스파이크 업로드가 한 폴더에 모이는 건 **의도된 동작**이다(진단용 수집).
export SAVE_CAPTURES="${SAVE_CAPTURES:-1}"
TUNNEL="${TUNNEL:-1}"                    # 0이면 공개 터널을 열지 않는다(엣지: 폰 안에서 자체완결)
CF_BIN=/usr/local/bin/cloudflared
ollama_up(){ curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; }

# EXTRACT=ocr 이면 ollama·모델이 통째로 필요 없다 — 엣지 경로엔 신경망 LLM이 0개다.
if [ "$EXTRACT" != "ocr" ]; then

# 0) ollama 살아있는지 -------------------------------------------------------
# 디코드 배칭(NUM_PARALLEL)·상시 로드(KEEP_ALIVE)는 서버 env라 systemd 유닛엔 drop-in 필요:
#   sudo systemctl edit ollama  →  [Service] Environment="OLLAMA_NUM_PARALLEL=2" "OLLAMA_KEEP_ALIVE=-1"
# systemd 없이 직접 띄우는 폴백 경로엔 여기서 주입한다.
if ! ollama_up; then
  echo "· ollama 기동…"
  sudo -n systemctl start ollama 2>/dev/null || \
    OLLAMA_NUM_PARALLEL="$NP" OLLAMA_KEEP_ALIVE=-1 nohup ollama serve >/tmp/ollama.log 2>&1 &
  for _ in $(seq 1 20); do ollama_up && break; sleep 1; done
fi
ollama_up || { echo "❌ ollama 미기동 — /tmp/ollama.log 확인"; exit 1; }

# 0.5) 모델 상시 로드(warmup) — 첫 요청의 콜드로딩(~90s)을 기동 시점으로 옮긴다 ----
echo "· 모델 워밍업($MODEL)…"
curl -s http://127.0.0.1:11434/api/generate \
  -d "{\"model\":\"$MODEL\",\"prompt\":\"1\",\"stream\":false,\"keep_alive\":-1,\"options\":{\"num_ctx\":8192}}" \
  >/dev/null && echo "· 모델 적재 완료(상시 유지)" || echo "⚠ 워밍업 실패 — 첫 추출이 느릴 수 있음"

else
  echo "· EXTRACT=ocr — ollama/모델 건너뜀(OCR 12.9MB만 사용)"
fi

# 0.7) 포트 선점 정리 — 이전 실행 잔여 프로세스가 $PORT를 물고 있으면 죽인다 ------
# (Ctrl-C 못 받고 죽거나 터널만 살아 서버 좀비가 남은 경우 "address already in use" 방지)
if fuser "$PORT/tcp" >/dev/null 2>&1; then
  echo "· 포트 $PORT 선점 프로세스 정리…"
  fuser -k "$PORT/tcp" 2>/dev/null || true
  for _ in $(seq 1 10); do fuser "$PORT/tcp" >/dev/null 2>&1 || break; sleep 0.5; done
  fuser "$PORT/tcp" >/dev/null 2>&1 && { echo "❌ 포트 $PORT 여전히 점유 중 — 수동 확인 필요"; exit 1; }
fi

# 1) 에이전트 서버 -----------------------------------------------------------
# 인터프리터는 **OCR이 되는 것**을 고른다. 한 서버가 요청마다 engine=edge|orin 을 고르므로
# (docs §4.15), 시스템 python3로 띄우면 엣지 경로가 통째로 죽고 셀렉터가 반쪽이 된다.
# 부수효과: VLM 경로의 broker 심판(evidence OCR)도 그동안 라이브에서 조용히 꺼져 있었다.
PY="${PY:-python3}"
for c in "$HOME/workspaces/edge-ocr-venv/bin/python" "$REPO_DIR/../edge-ocr-venv/bin/python"; do
  [ -x "$c" ] && "$c" -c "import rapidocr" >/dev/null 2>&1 && { PY="$c"; break; }
done
echo "· 인터프리터 $PY $("$PY" -c "import rapidocr" 2>/dev/null && echo '(엣지 경로 사용 가능)' || echo '(OCR 없음 — engine=edge 비활성)')"
"$PY" "$REPO_DIR/agent/server.py" >/tmp/agent-server.log 2>&1 &
SRV=$!
sleep 2
if ! kill -0 "$SRV" 2>/dev/null; then
  echo "❌ 서버 기동 실패:"; tail -n 20 /tmp/agent-server.log; exit 1
fi
echo "· 서버 pid $SRV  →  http://0.0.0.0:$PORT   (log: /tmp/agent-server.log)"

# 2) 터널 --------------------------------------------------------------------
if [ "$TUNNEL" = "0" ]; then
  # 엣지: 앱과 API가 같은 기기·같은 오리진이라 공개 터널이 필요 없다(= 노출면 0).
  echo
  echo "🔒 터널 없음(TUNNEL=0). 이 기기에서 바로 열어라:"
  echo "   http://localhost:$PORT/"
  echo "   (같은 Wi-Fi의 다른 기기: http://$(hostname -i 2>/dev/null | awk '{print $1}'):$PORT/)"
  echo
  cleanup(){ echo; echo "정리 중…"; kill "$SRV" 2>/dev/null || true; }
  trap cleanup EXIT INT TERM
  echo "실행 중. 종료하려면 Ctrl-C."
  wait "$SRV"
  exit 0
fi
echo "· cloudflared 터널 여는 중…"
"$CF_BIN" tunnel --url "http://localhost:$PORT" >/tmp/cf.log 2>&1 &
CF=$!

cleanup(){ echo; echo "정리 중…"; kill "$SRV" "$CF" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# 3) 공개 URL 추출 -----------------------------------------------------------
URL=""
for _ in $(seq 1 30); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/cf.log | head -1 || true)
  [ -n "$URL" ] && break
  kill -0 "$CF" 2>/dev/null || { echo "❌ cloudflared 종료됨:"; tail -n 20 /tmp/cf.log; exit 1; }
  sleep 1
done

echo
if [ -n "$URL" ]; then
  echo "🌐 공개 URL:  $URL"
  echo "   앱의 '🔗 에이전트 연결' 입력칸에 붙여넣기."
else
  echo "⚠ URL 추출 실패 — /tmp/cf.log 확인:"; tail -n 20 /tmp/cf.log
fi
echo
echo "실행 중. 종료하려면 Ctrl-C."
wait "$SRV"
