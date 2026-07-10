# 핸드오프 — 포트폴리오 리밸런서 + 로컬 추출 에이전트 (2026-07)

다른 에이전트가 이어받기 위한 현황. 상세 결정은 [`eval/DECISION.md`](eval/DECISION.md), 실측 리포트는 [아티팩트](https://claude.ai/code/artifact/4bfc070b-52fa-4522-b15d-41cdaeefcee3).

## 목표
증권사 앱 스크린샷 → AI로 보유자산 추출 → 리밸런싱. **무료·프라이빗**을 위해 클라우드 API 대신 **로컬 비전 모델**을 쓰기로 함. 최종 서버는 **Jetson AGX Orin 32GB**(나중 세팅). 지금은 **이 맥에서 MVP**를 돌려 외부 폰 접속까지 확인.

## 지금 동작하는 것 (이 맥, MVP)
- **프론트(리밸런서 앱)**: `main` 브랜치 → GitHub Pages `https://jsilver-p.github.io/portf-rebalancing/` (self-unpacking 번들). AI 호출부는 `callClaude`(design-source 참조).
- **로컬 추출 에이전트**: `agent/server.py` (:8899) — 업로드 UI + `POST /extract`(base64 이미지 → Ollama 추출 → 주가 역산·경고). CORS 개방.
- **모델**: `qwen2.5vl:7b` + 헤더프롬프트(`eval/harness/prompt2.txt`). Ollama(`~/portf-agent/bin/ollama-bin/`), 모델은 `~/.ollama`.
- **외부 접속**: `cloudflared` 퀵터널 → 공개 https URL. 폰에서 그 URL 열면 업로드·추출 동작.

## 기동 / 종료
```bash
bash agent/start.sh        # Ollama+서버+터널 기동 후 공개 URL 출력
# 종료: pkill -f 'ollama serve'; pkill -f agent/server.py; pkill cloudflared
```
바이너리(ollama·cloudflared)는 `~/portf-agent/bin`에 영속. 퀵터널 URL은 **기동할 때마다 바뀜**.

## 완료됨
- 모델 선정 실측 → 7B+헤더프롬프트 결정(정확도 100%). `eval/` 하네스·결정문서.
- 정답표(`test-fixtures/ground-truth.json`, gitignore) — 실계좌 4개 통합·검증.
- MVP 서버 + 퀵터널로 폰 접속 경로 확인.

## 다음 (우선순위)
1. **엔리치 강화** — 현재 서버는 추출 + 주가 역산만. 미구현: 증권사·티커 웹검색 식별, 계좌 간 수량 역산, 계좌합계 대조 게이트(정답표 검증 로직 참고).
2. **프론트 연동** — 배포 앱 `callClaude`를 이 에이전트 URL로 향하게(현재는 Anthropic 직접호출). 번들이 불투명하니 design-source 수정 후 재빌드 or 별도 프론트.
3. **Orin 이관** — 동일 스택(Ollama ARM64+CUDA)을 Orin에 올리고 속도·재현성 검증. Cloudflared는 **named tunnel**(고정 도메인)로 승격.
4. **보안** — 현재 퀵터널은 URL만 알면 누구나 접근. 토큰/인증 추가.

## 주의
- 이 맥은 **CPU라 이미지당 수 분**(정상). Orin GPU에선 초 단위 — 정확도는 동일 GGUF라 그대로 이전.
- 브랜치 `eval/local-agent`(미푸시). 민감정보(스크린샷·정답표·채점키·결과)는 gitignore.
