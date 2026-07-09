# 포트폴리오 리밸런서 — Cloud Design MCP 경로 (브랜치 B)

이 브랜치는 **claude.ai Design 프로젝트를 `claude_design` MCP(`DesignSync`)로 가져와 구현한** 결과입니다.
같은 디자인을 손으로 언번들해 재작성한 결과는 브랜치 [`impl/manual-unbundle`](../../tree/impl/manual-unbundle)에 있습니다.

## 무엇을 배포하나

- **`index.html`** — 디자인 툴이 내보낸 **self-unpacking 번들**이자 이 경로의 배포 산출물입니다.
  브라우저에서 열면 자체적으로 React + dc-runtime + Instagram 디자인 시스템 + 템플릿 + 폰트를
  blob URL로 복원해 앱을 렌더합니다. GitHub Pages 등 정적 호스팅에 그대로 올리면 동작합니다.

## `design-source/` — MCP가 노출하는 권위 있는 소스

디자인 프로젝트 `주식 포트폴리오 관리 웹앱`
(`6b5fb037-a48d-42de-bdbc-528156d16c38`)에서 `DesignSync.get_file`로 받은 원본입니다.

- **`Portfolio Rebalancer.dc.html`** (58KB) — design-comp 원본. `<x-dc>` 템플릿 +
  `<script data-dc-script>`(상태·리밸런싱 수학·환율 fetch·Claude 비전 추출·localStorage) +
  `data-props`(driftBand, comfortReading). 번들과 달리 **읽고 편집 가능한 소스**입니다.
- **`support.js`** — dc-runtime (React 위에서 `{{ }}`/`sc-if`/`sc-for` 템플릿을 컴파일).

> 이 소스만으로는 단독 실행되지 않습니다. `.dc.html`은 `<link>`로 Instagram 디자인 시스템
> 토큰 CSS(`_ds/instagram-design-system-a82dd2…/`)와 `_ds_bundle.js`를, `<script>`로
> `support.js`를 참조하며, **React/ReactDOM은 디자인 툴 런타임이 주입**합니다.
> 위 `index.html` 번들은 이 모든 의존성을 인라인해 배포 가능하게 만든 것입니다.
> 전체 디자인 시스템 원본(토큰 CSS, 컴포넌트 JSX, `_ds_bundle.js`)은 MCP 프로젝트에 있으며
> `DesignSync`로 재동기화할 수 있습니다.

## 앱 개요

증권사 앱 스크린샷 업로드 → Claude 비전으로 보유자산 추출(스냅샷 병합: 갱신·추가·제거) →
통합 보유 현황 → 자산배분 분석(현재 vs 목표 도넛·드리프트) → 목표 포트폴리오(프리셋+수동) →
세금 인지 리밸런싱 액션. 상태는 브라우저 localStorage에 저장됩니다.
AI 추출에는 사용자의 Anthropic API 키가 필요하며 키는 브라우저(localStorage)에만 저장됩니다.

## 두 경로 비교

| | 브랜치 A `impl/manual-unbundle` | 브랜치 B `impl/design-mcp` (이 브랜치) |
|---|---|---|
| 소스 출처 | 레포의 번들을 디코딩해 **역추출** | MCP로 **원본 `.dc.html` 직접 수령** |
| 배포 산출물 | 단일 `index.html` (vanilla, ~330KB, 폰트 인라인) | 디자인 툴 번들 `index.html` (~486KB) |
| 렌더러 | 직접 작성한 ~150줄 미니 런타임 (프레임워크 無) | React + dc-runtime (번들에 인라인) |
| 편집성 | 로직·마크업이 평문으로 노출, 바로 수정 | 배포본은 불투명 blob / 편집은 `design-source` 또는 MCP |
| 실행 환경 | file:// 및 http 모두 | http 권장 (번들 blob 복원) |
| 디자인 충실도 | 레퍼런스와 픽셀 동일(검증) | 원본 그대로(동일 렌더러) |
