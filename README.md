# 포트폴리오 리밸런서

증권사 앱 스크린샷을 올리면 보유자산을 추출해 **통합 현황 → 자산배분 분석 → 목표 대비 리밸런싱 액션**까지 내는 단일 페이지 웹앱. 상태는 브라우저 `localStorage`에만 저장한다(백엔드 없음).

- **배포 산출물은 `index.html` 하나.** 정적 호스팅에 그대로 올라간다. 빌드·의존성 없음.
- **추출 경로 두 가지** — 로컬 에이전트(무료·프라이빗, 권장) 또는 Anthropic API 직접 호출(사용자 키 필요).

## 흐름

```
스크린샷 8장 ──▶ 추출 ──▶ 보유자산 JSON ──▶ STEP2 표(출처칩) ──▶ 도넛·드리프트 ──▶ 리밸런싱 액션
                  │
                  ├─ 로컬 에이전트 (앱 좌하단 '에이전트 연결'에 URL이 있으면)
                  └─ Anthropic 직접 호출 (없으면 폴백)
```

앱은 `extractFiles()`에서 분기한다. 에이전트 경로는 8장을 **한 번에** `/extract/batch`로 보낸다 — 계좌합계 대조·계좌간 수량 역산 같은 **화면 간 교차검증**이 필요해서, 장당 호출로는 근거가 모이지 않는다. 서버가 게이트·정규화·수량 사다리까지 끝낸 결과를 출처(`qty_src`/`price_src`/`value_src`)째로 받아 표에 칩으로 표시한다.

## 레포 구조

| 경로 | 역할 |
|---|---|
| `index.html` | **배포 산출물**. dc-runtime + 앱 문서를 인라인한 self-unpacking 번들 |
| `design-source/app.current.html` | **앱을 고치는 유일한 편집 원본** — 아래 필독 |
| `design-source/support.js` | dc-runtime 소스. 배포 블롭 `679ba045`와 바이트 동일 — 템플릿 문법을 읽을 때 본다 |
| `agent/repack_app.py` | 원본 ↔ `index.html` 사이의 **단일 통제점** (`extract`/`embed`/`check`) |
| `agent/server.py` | 로컬 추출 에이전트(`:8899`). 비전추출·게이트·엔리치 |
| `agent/finalize.py` | 여러 화면 종합 → 정규화 + 계좌합계 대조 게이트. 파서 단일 출처 |
| `agent/resolve.py` · `resolve_broker.py` | 이름→시세심볼, 증권사 라벨→정규명. **하드코딩 매핑 없음**(동적 조회) |
| `agent/fetch_prices.py` | 시세 페처 → `prices.json` |
| `eval/harness/` | 추출 파리티 채점기(`parity.py`) · 실연결 E2E(`e2e.py`) |
| `eval/DECISION.md` | 추출 모델 선정 근거(`qwen2.5vl:32b`) |
| `docs/price-tracking.md` | 시세 트래킹 설계 결정 |
| `HANDOFF.md` | 에이전트 서버 구동·검증 절차, 현재 상태 |

## 프론트엔드 편집 — 반드시 읽을 것

`index.html`은 354줄이지만 실질은 **두 줄**이다.

| 줄 | 크기 | 정체 |
|---|---|---|
| 185 | 369KB | dc-runtime을 gzip+base64로 넣은 블롭. 손댈 수 없다 |
| 193 | 117KB | **앱 문서(HTML 전체)를 JSON 문자열 하나로** 인라인한 것 |

**193행이 진짜 소스다.** 117KB짜리 한 줄이고 따옴표·개행이 전부 이스케이프돼 있어 손편집은 불가능하다(깨지면 앱이 통째로 안 뜬다). 반드시 `repack_app.py`만 거친다:

```bash
python3 agent/repack_app.py extract design-source/app.current.html   # 193행 → 읽을 수 있는 문서
#   design-source/app.current.html 을 편집
python3 agent/repack_app.py embed design-source/app.current.html     # 다시 심음(왕복 검증 포함)
python3 agent/repack_app.py check                                    # 두 쪽 일치 확인
```

`index.html`을 직접 고치면 다음 `embed`에 **조용히 사라진다.** 커밋엔 항상 두 파일이 같이 오른다 — 원본 diff는 읽히고 `index.html` diff는 `193행 1줄 변경`으로만 보이는 게 정상이다.

`check`는 pre-commit 훅에도 걸려 있다. 한 번 켜두면 된다:

```bash
git config core.hooksPath .githooks
```

훅은 작업트리가 아니라 **스테이징된 내용**을 본다. 원본만 `add`하고 `index.html`을 빠뜨리면 작업트리는 멀쩡한데 커밋만 어긋나는데, 그건 커밋될 blob을 봐야만 잡힌다.

### 왜 이런 구조인가 — 디자인 툴 경로는 닫혀 있다

앱은 원래 claude.ai/design 프로젝트(`6b5fb037-a48d-42de-bdbc-528156d16c38`)에서 만들어졌고 `index.html`은 그 툴의 번들 export다. 그런데 **번들 export는 웹 UI에서만 되는 동작**이다. `DesignSync` MCP에는 파일 읽기/쓰기(`get_file`/`write_files`)만 있고 번들을 만드는 메서드가 없다. 즉 디자인 프로젝트 쪽 소스를 고쳐도 CLI에서 `index.html`을 재생성할 길이 없다. 그래서 편집 대상이 임베드 문자열 그 자체이고, `repack_app.py`가 그걸 안전하게 넣고 빼는 유일한 통제점이다.

2026-07-08에 디자인 프로젝트에서 받아둔 `Portfolio Rebalancer.dc.html`이 있었으나 **업로드된 번들과 처음부터 갈라져 있었다** — 배포본에 없는 기능(`reclassify`·`suggestion` 등)이 들어있었다. 권위 있는 소스처럼 보이지만 따라가면 이후의 앱 변경이 전부 날아가고, 애초에 그걸로 번들을 다시 만들 수도 없으므로 **삭제했다**(이력에 `e6a3163`으로 남아있다).

같이 받은 `support.js`는 남겼다. 이건 갈라진 사본이 아니라 **배포 런타임 그 자체**다 — 185행 매니페스트의 `679ba045` 블롭과 바이트 동일하고, 앱이 `app.current.html:5`에서 `<script src="679ba045-…">`로 로드하는 게 바로 이 코드다. 템플릿 문법(`sc-if`/`sc-for`/`x-import`/`{{ }}`)이 실제로 어떻게 컴파일되는지 읽을 유일한 창이다.

> 185행 매니페스트엔 블롭이 25개 있다 — JS 4개(dc-runtime `679ba045` 61KB, React·디자인시스템 `fdd09da4` 133KB, 그 외 2개) + 폰트 21개. **비교할 땐 블롭을 특정할 것** — 엉뚱한 걸 집으면 "다르다 → 구버전"이라는 틀린 결론이 나온다.

## 검증

```bash
python3 agent/repack_app.py check     # index.html ↔ 편집 원본 동기화 (훅이 자동 실행)
```

추출 파이프라인 파리티·실연결 E2E는 에이전트 서버가 필요하다 — `HANDOFF.md` 참조.

## 보안

레포는 **공개**다. 민감정보는 전부 gitignore이며 **커밋 금지**:
`test-fixtures/`(실계좌 스크린샷·정답표) · `prices.json` · `watchlist.json` · 심볼 캐시 · `agent/env` · `/env` · `data/`.

Anthropic API 키는 브라우저 `localStorage`에만 저장되고 레포·서버에 들어가지 않는다.
