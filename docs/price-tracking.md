# 시세 트래킹 — 조사 결과 및 설계 결정

> 조사·검증일: 2026-07-09. 가격·한도는 벤더가 바꾸므로 인용된 수치는 재확인 대상.

## 문제

보유 종목에 **국내 주식 + 국내 ETF + 미국 주식/ETF가 혼재**한다. 이들의 현재가를 주기적으로
받아 목표 배분 대비 드리프트를 계산하고, 밴드 이탈 시 알린다.

## 결정 요약

1. **MCP는 수집층에 쓰지 않는다.** MCP는 LLM에 도구를 노출하는 프로토콜이다. "가격을 받아
   기록한다"에는 LLM이 필요 없다 — cron + HTTP 요청이 더 싸고 결정론적이다. 에이전트는
   *판단·알림층*에만 둔다.
2. **실시간 시세는 요구사항이 아니다.** 리밸런싱은 드리프트 밴드가 주 단위로 벌어질 때 작동한다.
   장중 시세는 비용을 10~100배로 올리면서, 밴드를 스쳤다 돌아오는 **가짜 이탈**을 만든다.
   종가 기준 1일 1~2회가 충분하고 더 정확하다. 이 전제가 아래 모든 선택을 결정한다.
3. **소스는 Yahoo chart 엔드포인트 하나로 시작한다.** 단, `prices.json` 스키마를 계약으로
   고정해 소스를 교체 가능하게 둔다.

## 검증된 사실 — Yahoo chart 엔드포인트

```
GET https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1d
```

인증 없음. `User-Agent` 헤더만 필요. 2026-07-09 실제 호출 결과:

| 심볼 | 이름 | instrumentType | currency | price |
|---|---|---|---|---|
| `KRW=X` | USD/KRW | CURRENCY | KRW | 1510.98 |
| `069500.KS` | KODEX 200 | ETF | KRW | 117,700 |
| `360750.KS` | TIGER 미국S&P500 | ETF | KRW | 28,075 |
| `005930.KS` | 삼성전자 | EQUITY | KRW | 278,000 |
| `VOO` | Vanguard S&P 500 ETF | ETF | USD | 685.26 |

응답 경로: `chart.result[0].meta` → `regularMarketPrice`, `currency`, `instrumentType`,
`longName`, `regularMarketTime`.

핵심 함의:

- **단일 네임스페이스**로 KRX(`.KS`) / KOSDAQ(`.KQ`) / 미국(접미사 없음) / 환율(`KRW=X`)이 모두 해석된다.
- **`currency`가 응답에 포함**된다. 종목→통화 매핑을 직접 들고 있으면 안 된다.
  `360750.KS`(미국 S&P500 추종이지만 KRW 거래)처럼 손으로 매핑하면 틀리는 케이스가 실재한다.
- 환율이 같은 소스로 덮이므로 현재 `.dc.html`의 `open.er-api.com/v6/latest/USD` 의존성을 흡수할 수 있다.

### 리스크

비공식 엔드포인트다. Yahoo는 웹 페이지 쪽(`finance.yahoo.com/quote/...`)에 봇 차단을 걸어 두었고
조사 중 실제로 503을 반환했다. JSON 엔드포인트는 열려 있었다. 차단·rate limit 사례는
대부분 공격적 폴링에서 나온다 — 하루 1~2회 수십 심볼 조회는 해당 없음. 그래도 **언젠가 깨진다**고
가정하고 설계할 것.

## 소스 비교

| 소스 | 국내+미국+ETF | 비용 | 판정 |
|---|---|---|---|
| **Yahoo chart 엔드포인트** | 전부 (검증함) | 무료, 키 불필요 | **채택** |
| [EODHD](https://eodhd.com/pricing) | 전부 (`005930.KO` / `VOO.US`) | $19.99/mo EOD 전세계, $29.99 15분지연 | **유료 이전 경로** |
| [Twelve Data](https://twelvedata.com/exchanges/xkrx) | KRX 지원하나 **EOD만** | KRX는 Pro **$99/mo** | 함정 — 자사 페이지에 "Delay: EOD, Pro+ 필요" 명시 |
| [KIS Open API](https://github.com/koreainvestment/open-trading-api) | 전부, 실시간 웹소켓 | 무료(계좌 필요) | 과잉. OAuth 토큰 + 국내/해외 엔드포인트 분리 |
| [Alpha Vantage](https://www.alphavantage.co/premium/) | 미국 중심 | 무료 **25 req/day** | 탈락. 폴링 불가 (10종목 × 시간당 = 240/day) |

Alpha Vantage는 유일한 벤더 공식 MCP 서버(`mcp.alphavantage.co`)를 갖고 있으나, 무료 한도와
미국 편중 때문에 이 프로젝트에는 맞지 않는다. "공식 MCP가 있다"는 사실이 선택 근거가 되지 못한 사례.

## 권장 아키텍처

현재 앱은 **정적 `index.html` + localStorage**이고 GitHub Pages에 배포된다. 순진하게 접근하면
두 벽에 부딪힌다: (1) 브라우저에서 직접 부를 수 있는 시세 API가 드물다(CORS), (2) API 키를
브라우저에 두면 노출된다.

**하루 1~2회 EOD 수집으로 두 벽이 동시에 사라진다:**

```
GitHub Actions cron ──(KRX 마감 15:30 KST / NYSE 마감 16:00 ET 직후)
  └─ Yahoo chart 엔드포인트로 심볼 훑기
     └─ prices.json 생성 → Pages 브랜치에 커밋
                                  │
정적 앱 ──(same-origin fetch ./prices.json)──┘
```

- 프록시 불필요, CORS 없음, 브라우저에 키 없음.
- **`prices.json` 스키마를 계약으로 고정한다.** Yahoo가 막히면 페처의 소스만 EODHD로 교체하고
  앱은 건드리지 않는다. 교체 가능성을 지금 확보하는 비용은 ~0, 나중은 크다.
- 스키마에는 최소한 `symbol`, `price`, `currency`, `asOf`를 담는다. `currency`를 앱이 아니라
  **소스가 말하게** 두는 것이 이 설계의 불변식이다.

## 에이전트(MCP)의 자리

수집은 위 cron이 한다. 에이전트는 `/schedule`로 하루 1~2회 깨어나 `prices.json`과 목표 배분을
읽어 드리프트를 계산하고, **밴드를 넘었을 때만** `notify-phone`으로 알린다.
가격을 읽으려고 LLM을 돌리지 않고, 판단이 필요한 순간에만 돌린다.
이 구성에서는 별도의 주식 데이터 MCP 서버를 붙일 이유가 없다.

관련 기존 코드: `design-source/Portfolio Rebalancer.dc.html`의 `driftBand`, `comfortReading` props.

## 구현 현황 (2026-07-10)

실제 보유 31종목으로 실측 후 구현. 코드는 `agent/`.

**아키텍처 전환 — Actions 대신 서버 서빙.** 문서의 Actions안은 "서버 없는 정적 앱" 전제였다.
이제 추출용 서버(에이전트 박스, 향후 Orin)가 있고, **레포가 PUBLIC이라 `prices.json`을 커밋하면
보유 종목 구성이 공개 누출**된다. 그래서 **서버가 시세를 받아 저장·서빙**한다:
`fetch_prices`(결정론적, LLM 무관)를 스케줄 실행 → `GET /prices`로 서빙. watchlist·prices는 서버 전용(레포 밖).

- [x] **커버리지 전수 확인 — 26/26.** 신형 영숫자 KRX 코드(0053L0 등)·미국 티커 모두 Yahoo 커버. 구멍 없음.
- [x] **이름→심볼 해석(`resolve.py`).** 국내=Naver autocomplete(코드+시장→.KS/.KQ), 미국=괄호 티커. Yahoo search는 한글에 불가라 폐기. 하드코딩 없음.
- [x] **스키마 확정 + 페처(`fetch_prices.py`).** `{symbol,price,currency,marketTime,stale}` + `fx.USDKRW`. 소스 교체 가능.
- [x] **서버 스케줄 페치 + `/prices`(`server.py`).** 마감 후 UTC 06:45/21:30 갱신, 원자적 쓰기.
- [x] **staleness.** `marketTime` 기준 `stale` 플래그(STALE_DAYS=4).
- [x] **T4 수량 역산 엔리치.** 화면에 수량 없는 종목은 **캡처 시점(EXIF) 종가로 역산**(수량=평가금액/(종가·환율)).
  게이트는 **노이즈 전파식**: `잔차 + 주식수×δ < 0.33`(δ KRW≈0/USD≈0.015) — 주식수 적은 종목은 δ 커도 안전, 많으면 위험.
  실측: KRW 전량+USD 소수주식(GOOGL·VOO·META·ARKF·PLTR) 복원, **오채택 0**. 이미지 EXIF로 캡처시각 자동, 시장 마감 전/후로 기준 세션 선택(KRW 당일·US 직전).
- [x] **앱 연동.** 사이드카가 Anthropic 호출을 `/complete`로 인터셉트(키 불필요, 다중사진), `/reprice`로 재평가(네이티브 통화, 앱이 ×fx), 수량/평가 두 기준시각 표시. **배포됨**.

**남은 것:**
- [ ] **보안** — 터널 엔드포인트에 토큰/인증(현재 URL만 알면 접근).
- [ ] **Orin 이관** — 동일 스택 + named tunnel(고정 도메인) → 사이드카 URL 재입력 불필요.
- [ ] `open.er-api.com` 환율 완전 대체: 앱 자체 환율 fetch를 `/reprice`의 fx로 이미 덮지만, 앱 초기 로드시 er-api 호출은 남아 있음(선택).
