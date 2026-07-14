# 핸드오프 — 포트폴리오 리밸런서 로컬 에이전트 (2026-07)

증권사 스크린샷 → 로컬 비전 LLM 추출 → 계좌합계 게이트·broker 정규화 → 리밸런싱. 무료·프라이빗.
서버는 **Jetson AGX Orin 32GB**(GPU/CUDA)에서 구동. 코드 브랜치 `eval/local-agent`(푸시됨).

## 지금 할 일 — 전자 트랙: 추출 파리티 (ORIN에서)

목표: 정답표와 동일 정보(31종목·정규 broker·계좌합계·출처)를 배치 추출이 안정 산출.
`/extract/batch`를 8장 실추출했을 때 드러난 **회귀 2건**을 잡는다.

1. **해외주식 상세(10종목·128M) 통째 누락.** 증상: 그 화면이 `screens`에 `detail`인데 holdings 0.
   원인 추정: 라이브 7B가 그 화면(USD 10행, 최밀) JSON 파싱 실패 → `finalize.parse_rows`가 `[]` →
   `classify([])`가 기본 `detail` 반환(빈 상세). **진단엔 per-screen raw 필요.**
   → **먼저** `server.extract_batch`가 결과에 `screens[i].raw`(또는 디버그 플래그)를 실어 반환하게 하고,
   ORIN에서 8장 1회 재추출해 img3(해외주식) raw를 직접 본다. 파싱 문제면 `parse_rows` 견고화 or 프롬프트,
   빈 결과면 `classify`가 빈 배열을 detail로 삼지 않게(빈 화면=경고) 수정.
2. **삼성 ISA broker → 한국투자증권 오정규화.** IRP는 삼성증권으로 맞음. 원인 후보:
   ① `resolve_broker`/검색이 틀린 값 반환 ② `canonical_in(screen_text)`가 펀드 법정명 "…증권투자신탁"의
   `증권`을 broker로 오매치 ③ `summary_broker` 상속 오류. img6(계좌요약)·img7(ISA상세) raw로 판별 후 수정.

파리티 확인되면(31종목·정규 broker):
3. 배치 import 경로가 `qty_src`/`price_src`를 `pf_rebalancer_v1`에 저장하게 → 앱 출처칩 자동 점등(후자 완료).
4. **배포**: `eval/local-agent` → `main` 머지 + GitHub Pages(후자 UI까지 함께 공개). 배포는 승인 게이트.

검증은 앱 밖에서: `test-fixtures/screenshots`(8장) + `ground-truth.json`(31) 대조. `python3 agent/finalize.py`.

## ORIN 서버 기동 / 재기동
```bash
cd <repo> && git checkout eval/local-agent && git pull
bash agent/setup-orin.sh          # 최초 1회(ollama CUDA·모델·cloudflared arm64·Pillow). 재실행 안전.
bash agent/run-agent.sh           # ollama+서버(:8899)+터널 기동, 공개 URL 출력. Ctrl-C 정리.
# 재기동: Ctrl-C 후 run-agent.sh 다시. 퀵터널이라 URL이 매번 새로 발급됨.
```
새 URL은 로컬 `agent/env`(gitignore)에 두거나 사용자에게 전달 → 앱 좌하단 '에이전트 연결'에 입력.
데이터(시세·watchlist·broker 캐시)는 `~/portf-agent/data`(레포 밖).

## 서버 엔드포인트 (`agent/server.py` :8899)
- `GET /health` · `GET /capture`
- `POST /complete/submit`+`/complete/result` — 앱 Anthropic 호출 대체(비동기, 키 불필요).
- `POST /extract` — 단건 추출+엔리치.
- `POST /extract/batch/submit`+`/extract/batch/result` — **다중화면 종합**: 비전추출→`finalize`(게이트+정규화)→엔리치.
- `POST /reprice` · `GET /prices` — 결정론적 시세 재평가(LLM 무관, 초 단위).
- MODEL=`qwen2.5vl:7b`, 프롬프트 `eval/harness/prompt2.txt`. ORIN GPU ~38초/장(측정).

## 파이프라인 핵심 (완료·유지)
- **게이트**(`finalize.py`): 화면 유형 분류(product_summary/account_summary/detail) → 상세홀딩합 ↔ 요약총액
  매칭으로 스코프·재현율·환각 점검. 요약행은 홀딩서 제외. 하드드롭 아님(경고). 8장 스코프 5/5 매칭 확인.
- **broker 정규화**(`resolve_broker.py`): 하드코딩 없음. 정규명 직채택 / 브랜드(Super365)→네이버검색+LLM-RAG /
  계좌번호·별칭→요약화면 정규명 상속. 캐시 `~/portf-agent/data/broker_cache.json`.
- **엔리치**(`server.enrich`): 주가=평가금액/수량, 계좌간 동일종목 수량 역산, **T4 캡처시점(EXIF) 종가 역산**
  (노이즈 전파 게이트, 오채택 0). 이름→심볼 `agent/resolve.py`(네이버 autocomplete/티커).

## 프론트 (후자 트랙 — 완료, 미배포)
- `index.html`은 **편집 가능**(불투명 아님): 193번 줄 임베드 문서를 `agent/repack_app.py`로 extract/embed.
  진짜 현행 소스=`design-source/app.current.html`(구 .dc.html은 stale). 상세 → 메모리 `app-edit-workflow`.
- STEP2 테이블을 **계좌 그룹**(증권사+유형뱃지+소계) + 출처칩으로 개편, 헤드리스 검증 통과. **main 미배포**(전자와 묶음).

## 주의 / 보안
- 레포 **공개**. 민감정보(스크린샷·ground-truth·prices·watchlist·심볼캐시·`agent/env`·`~/portf-agent/data`·
  실계좌번호·홀더명)는 전부 gitignore. **커밋 금지.**
- 미해결: 루트 `env`가 git 추적 중(공개, stale 죽은 URL). `git rm --cached env && rm env` 필요.
- 퀵터널 URL 매 기동 변경(향후 named tunnel로 고정 승격). 터널 인증 없음(URL만 알면 접근) — 추후 토큰.
