# 핸드오프 — 포트폴리오 리밸런서 + 로컬 에이전트 + 시세 추적 (2026-07)

다른 에이전트가 이어받기 위한 현황. 상세: 모델 결정 [`eval/DECISION.md`], 시세 설계 [`docs/price-tracking.md`].

## 목표
증권사 앱 스크린샷 → AI로 보유자산 추출 → 현재가로 재평가 → 리밸런싱. **무료·프라이빗**을 위해
클라우드 API 대신 **로컬 비전 모델 + 로컬 서버**. 최종 서버는 **Jetson AGX Orin 32GB**(나중). 지금은 이 맥에서 MVP.

## 지금 동작하는 것
- **프론트(배포 앱)**: `main` → GitHub Pages `https://jsilver-p.github.io/portf-rebalancing/`. 불투명 번들이라
  **사이드카**(`index.html` 말미 `<script>`, 번들 독립·자가치유)로 확장. localStorage 계약 `pf_rebalancer_v1`만 사용.
- **로컬 서버**(`agent/server.py` :8899) — 두 역할:
  - **추출 에이전트**(LLM): `POST /extract`(스크린샷→보유자산, qwen2.5vl:7b+`prompt2.txt`). 엔리치 사다리 T1~T4 적용.
  - **시세**(결정론적, LLM 무관): `GET /prices`(스케줄 갱신본 서빙), `POST /reprice`(앱 보유자산→현재가 재평가).
- **시세 페처**(`agent/fetch_prices.py`): 심볼→Yahoo chart→`prices.json`. **이름→심볼**은 `agent/resolve.py`
  (국내=Naver autocomplete, 미국=티커. 하드코딩 없음). 마감 후 UTC 06:45/21:30 자동 갱신.
- **외부 접속**: `cloudflared` 퀵터널 → 공개 https URL(폰).

## 기동 / 종료
```bash
bash agent/start.sh    # Ollama+서버+터널 기동 후 공개 URL 출력 (URL은 기동마다 바뀜)
# 종료: pkill -f 'ollama serve'; pkill -f agent/server.py; pkill cloudflared
```
바이너리는 `~/portf-agent/bin`, 시세·watchlist·심볼캐시는 `~/portf-agent/data`(서버 전용, 레포 밖).

## 재평가 사용법 (사용자)
1. `bash agent/start.sh` → 출력된 터널 URL 복사.
2. 배포 앱 좌하단 **📈 현재가 재평가** 패널에 URL·캡처일 입력 → **재평가**.
3. 화면에 수량이 없던 종목은 캡처일 종가로 **역산한 추정 수량(≈ 배지)**으로 표시.

## 완료됨
- 모델 선정 실측 → 7B+헤더프롬프트(정확도 100%).
- **시세 추적**: 실보유 26/26 커버리지 확인, 서버 페치·서빙(공개 누출 없음), `prices.json` 계약 스키마.
- **T4 수량 역산 엔리치**: 캡처일 종가로 역산+정수 잔차 게이트. 실측 KRW 14/14 정확, unobtainable 7종 복원, USD는 보수적 거부.
- **앱 연동**: 사이드카 `POST /reprice`로 수량×현재가 재평가(네이티브 통화, 앱이 ×fx). 헤드리스 종단 검증 통과. **배포 완료**.

## 다음 (우선순위)
1. **Orin 이관** — 동일 스택(Ollama ARM64+CUDA, Python 서버). cloudflared는 **named tunnel**(고정 도메인)로 승격 → 사이드카 URL 재입력 불필요.
2. **보안** — `/extract`·`/reprice`·`/prices` 터널에 토큰/인증(현재 URL만 알면 접근).
3. **추출→앱 자동 반영** — 현재 사이드카는 재평가만. 스크린샷 추출 결과를 앱에 병합하는 경로(현재는 앱 자체 Anthropic 경로 또는 서버 `/extract` 수동).

## 주의
- 퀵터널 URL은 **기동마다 바뀜**(Orin named tunnel 전까지 매번 재입력).
- 이 맥은 **CPU라 추출 이미지당 수 분**(정상). 재평가(시세)는 초 단위. Orin GPU에선 추출도 초 단위.
- 브랜치 `eval/local-agent`(agent 코드·docs·eval, **미푸시**). `main`엔 index.html만 배포. 민감정보(스크린샷·정답표·prices·watchlist·심볼캐시)는 gitignore.
