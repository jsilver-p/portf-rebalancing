#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# 에이전트 서버 + cloudflared 터널을 켜고 공개 URL을 출력한다. Ctrl-C로 정리.
# 셋업은 setup-orin.sh가 선행. 조정: MODEL, DATA_DIR, PORT, REPO_DIR.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # 이 스크립트가 든 레포
export MODEL="${MODEL:-qwen2.5vl:7b}"
export DATA_DIR="${DATA_DIR:-$HOME/portf-agent/data}"
export PORT="${PORT:-8899}"
CF_BIN=/usr/local/bin/cloudflared
ollama_up(){ curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; }

# 0) ollama 살아있는지 -------------------------------------------------------
if ! ollama_up; then
  echo "· ollama 기동…"
  sudo systemctl start ollama 2>/dev/null || nohup ollama serve >/tmp/ollama.log 2>&1 &
  for _ in $(seq 1 20); do ollama_up && break; sleep 1; done
fi
ollama_up || { echo "❌ ollama 미기동 — /tmp/ollama.log 확인"; exit 1; }

# 1) 에이전트 서버 -----------------------------------------------------------
python3 "$REPO_DIR/agent/server.py" >/tmp/agent-server.log 2>&1 &
SRV=$!
sleep 2
if ! kill -0 "$SRV" 2>/dev/null; then
  echo "❌ 서버 기동 실패:"; tail -n 20 /tmp/agent-server.log; exit 1
fi
echo "· 서버 pid $SRV  →  http://0.0.0.0:$PORT   (log: /tmp/agent-server.log)"

# 2) 터널 --------------------------------------------------------------------
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
