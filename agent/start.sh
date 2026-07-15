#!/usr/bin/env bash
# 로컬 추출 에이전트 MVP 기동: Ollama + 서버(:8899) + Cloudflare 퀵터널(공개 https)
# 바이너리는 ~/portf-agent/bin (세션 무관 영속). 로그는 /tmp/pf-*.log
set -e
BIN="$HOME/portf-agent/bin"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
export OLLAMA_HOST=127.0.0.1:11434

# 1) Ollama
if ! curl -s http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
  echo "· Ollama 기동…"; ( cd "$BIN/ollama-bin" && nohup ./ollama serve >/tmp/pf-ollama.log 2>&1 & )
  until curl -s http://127.0.0.1:11434/api/version >/dev/null 2>&1; do sleep 1; done
fi

# 2) 에이전트 서버
if ! pgrep -f "agent/server.py" >/dev/null; then
  echo "· 서버 기동 :8899…"; nohup python3 "$REPO/agent/server.py" >/tmp/pf-agent.log 2>&1 &
  sleep 2
fi

# 3) 퀵터널 (공개 https URL)
echo "· 퀵터널 기동…"; nohup "$BIN/cloudflared" tunnel --url http://localhost:8899 >/tmp/pf-cf.log 2>&1 &
until grep -q trycloudflare.com /tmp/pf-cf.log 2>/dev/null; do sleep 2; done
echo
echo "  폰에서 열 URL →  $(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /tmp/pf-cf.log | head -1)"
echo "  (URL은 기동할 때마다 바뀜. 종료: pkill -f 'ollama serve'; pkill -f agent/server.py; pkill cloudflared)"
