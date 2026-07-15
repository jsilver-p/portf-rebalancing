# 핸드오프 — 포트폴리오 리밸런서 로컬 에이전트 (2026-07-15)

증권사 스크린샷 → 로컬 비전 LLM 추출 → 게이트·정규화·수량 사다리 → 리밸런싱. 무료·프라이빗.
서버는 **Jetson AGX Orin 64GB**(GPU/CUDA)에서 구동. 브랜치 `eval/local-agent`.

## 현재 상태 — 추출 파리티 달성 ✅

`eval/harness/parity.py`(엔드투엔드 채점기)로 **31/31 종목 · 환각 0 · 전 필드 일치 · 게이트 침묵**.
모델은 **`qwen2.5vl:32b`**(선정 근거·측정치·탈락 모델은 `eval/DECISION.md` v2).

- 게이트 대조군: 화면 누락·빈 화면·환각행 주입 → **3종 모두 경고** / 정상 입력 → **침묵** / 순서 셔플 → 동일 결과.
- 앱은 이제 `/extract/batch`를 탄다(전엔 한 장씩 Anthropic 호출 → 게이트·정규화·출처가 앱에 도달하지 못했다).

## 남은 일

1. **배포 게이트(승인 필요)**: `eval/local-agent` → `main` 머지 + GitHub Pages 공개.
2. 퀵터널 URL이 매 기동 바뀜 → named tunnel로 고정 승격(선택). 터널 인증 없음(URL만 알면 접근) — 추후 토큰.

## 실행

```bash
bash agent/setup-orin.sh            # 최초 1회(ollama CUDA·모델·cloudflared arm64·Pillow). 재실행 안전.
MODEL=qwen2.5vl:32b bash agent/run-agent.sh   # ollama+서버(:8899)+터널 기동, 공개 URL 출력
```
새 URL은 앱 좌하단 '에이전트 연결'에 입력(= `localStorage.pf_agent_url`).
데이터(시세·watchlist·broker 캐시)는 `~/portf-agent/data`(레포 밖).
**8장 추출 ≈ 13분**(32B, 92s/장). 빠른 대안은 `MODEL=qwen2.5vl:7b`(33s/장) — 단 값 1건이 틀리고 게이트가 무력화된다.

## 검증 (앱 밖 → 앱까지)

```bash
PROMPT_FILE=eval/harness/prompt4.txt python3 eval/harness/run_extract.py qwen2.5vl:32b   # 8장 추출
python3 eval/harness/parity.py eval/results/qwen2.5vl_32b_p4 --no-llm --controls          # 채점 + 게이트 대조군
FIREFOX_BIN=/snap/firefox/current/usr/lib/firefox/firefox python3 eval/harness/e2e.py     # 앱→서버 실연결
```
E2E는 정적서버(`python3 -m http.server 8000`)와 에이전트 서버가 떠 있어야 한다.

## 파이프라인 (핵심 설계)

- **파서**(`finalize.parse_rows`) — 단일 출처. 모델이 화면 표기를 그대로 옮겨 JSON을 깨뜨린다
  (`"cost": +263504` ← 선행 `+`는 문법 위반). 부호·쉼표 교정 + **행 단위 salvage**(한 행이 깨져도 나머지는 살린다).
- **게이트**(`finalize.py`) — 화면 유형 분류 → 계좌·상품 총액 대조. 전부 **경고 + abstain**(하드드롭 아님).
  불변식: 회계 항등식(값=원가+손익) / 화면 수량 검증(캡처일 종가로 설명 안 되면 기각) / 합계행은 산술로 판정 /
  한 자산 한 행 / 잔고 음수 불가 / 계좌 잔고 행엔 수량 없음 / 자릿수 오독 보정(계좌합계 대조).
- **broker 정규화**(`resolve_broker.py`) — 하드코딩 없음. 정규명 직채택 / 브랜드→검색+LLM-RAG /
  계좌번호·별칭 → **같은 계좌**의 요약행에서 상속(전역 상속 금지 — 증권사 3곳부터 오염).
  라벨 없는 화면은 '같은 요약화면에 대조되는 화면'에서 소속 추론. **근거 없으면 답하지 않는다.**
- **수량 사다리**(`server.enrich`) — T1 화면수량 → T2 주가(=값/수량, USD면 ÷FX) → T3 **계좌간 동일종목 역산**
  → T4 **캡처일(EXIF) 종가 역산**(노이즈 게이트). 실패 시 null. 추정은 `qty_src`·`confidence`로 반드시 표시.
- **통화** — USD 자산의 금액 필드는 **네이티브(달러)**로 낸다. 앱이 fx로 환산하므로 원화로 주면 환율이 두 번 곱해진다.

## 프론트 (`index.html`)

- 편집 경로: `python3 agent/repack_app.py extract app.html` → 수정 → `embed app.html`.
  진짜 소스는 임베드 문자열이고 `design-source/app.current.html`은 그 사본(동기화 유지).
- 에이전트 URL이 있으면 **8장을 한 번에** `/extract/batch`로 보내고, 서버가 종합한 holdings를
  출처(`qty_src`/`price_src`/`value_src`)까지 그대로 받아 표에 칩으로 표시한다.

## 주의 / 보안

- 레포 **공개**. 민감정보(스크린샷·ground-truth·prices·watchlist·심볼캐시·`agent/env`·`~/portf-agent/data`·
  실계좌번호·홀더명)는 전부 gitignore. **커밋 금지.** (`agent/env`는 추적 해제 완료.)
