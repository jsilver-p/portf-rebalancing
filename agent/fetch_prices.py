#!/usr/bin/env python3
"""시세 페처 — 심볼 목록 → prices.json (스키마 = 계약).

범용 함수: 심볼→가격만 한다. 어떤 종목을 볼지(watchlist)는 데이터 입력으로 분리한다.
소스는 Yahoo chart 하나로 시작하되, 출력 스키마를 고정해 소스를 교체 가능하게 둔다.
통화는 소스가 말한다(앱이 매핑하지 않는다). 환율(USD/KRW)도 같은 소스로 덮는다.

사용:
  python3 agent/fetch_prices.py watchlist.json prices.json
  watchlist.json = ["000660.KS","VOO", ...]  또는  [{"symbol":"...","name":"..."}]

스키마(prices.json):
  {
    "asOf": "<UTC ISO8601, 페치 시각>",
    "source": "yahoo-chart",
    "fx": { "USDKRW": <float|null> },
    "prices": {
      "<symbol>": {
        "price": <float>, "currency": "<KRW|USD|...>",
        "name": "<longName|null>", "marketTime": "<UTC ISO8601|null>",
        "stale": <bool>            # marketTime이 STALE_DAYS보다 오래됨
      }, ...
    },
    "errors": { "<symbol>": "<reason>" }   # 실패 심볼(있을 때만)
  }
"""
import json, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
STALE_DAYS = 4          # 연휴를 넘겨 이만큼 오래된 종가는 stale로 표시
FX_SYMBOL = "KRW=X"     # USD/KRW


def _iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_one(symbol, retries=2):
    """심볼 → meta dict 또는 예외."""
    url = CHART + urllib.parse.quote(symbol) + "?range=1d&interval=1d"
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read())
            res = d.get("chart", {}).get("result")
            if not res:
                err = d.get("chart", {}).get("error")
                raise ValueError(str(err) or "no result")
            return res[0]["meta"]
        except Exception as e:
            last = e
            time.sleep(0.5 * (i + 1))
    raise last


def chart(symbol, rng="1d", retries=2):
    """심볼 → chart result(meta+indicators) 또는 예외."""
    url = CHART + urllib.parse.quote(symbol) + f"?range={rng}&interval=1d"
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read())
            res = d.get("chart", {}).get("result")
            if not res:
                raise ValueError(str(d.get("chart", {}).get("error") or "no result"))
            return res[0]
        except Exception as e:
            last = e
            time.sleep(0.5 * (i + 1))
    raise last


def history_close(symbol, date, rng="3mo"):
    """symbol의 date(YYYY-MM-DD) 종가. 정확 일치 없으면 date 이하 최신. → (close, actual_date)."""
    res = chart(symbol, rng)
    ts = res.get("timestamp", []) or []
    cl = res.get("indicators", {}).get("quote", [{}])[0].get("close", []) or []
    days = {}
    for t, c in zip(ts, cl):
        if c is None:
            continue
        days[time.strftime("%Y-%m-%d", time.gmtime(t))] = c
    if date in days:
        return days[date], date
    prev = [d for d in sorted(days) if d <= date]
    if prev:
        return days[prev[-1]], prev[-1]
    return None, None


# 시장 마감의 UTC 근사 시각: KRX 15:30 KST=06:30 UTC, US 16:00 ET≈20:00 UTC(EDT).
# (zoneinfo 없는 3.8 호환. DST로 겨울 US 마감은 21:00 UTC지만, 한국 낮 캡처는 마감과 멀어 무영향.)
_CLOSE_UTC_H = {"USD": 20.0, "KRW": 6.5}


def price_asof(symbol, capture_dt, currency, rng="3mo"):
    """캡처 시각(capture_dt: tz-aware datetime) 당시 이미 확정돼 있던 마지막 종가.
    시장 마감(UTC 근사)과 비교해 세션 날짜를 정한다: KRW는 15:30 KST 이후면 당일 종가,
    US는 20:00 UTC 이후라야 당일 — 한국 낮에 찍으면 미국장 마감 전이라 직전 미국 세션 종가.
    → (close, close_date). 실패 시 history_close(date) 폴백."""
    try:
        cu = capture_dt.astimezone(timezone.utc)
        h = _CLOSE_UTC_H.get(currency, _CLOSE_UTC_H["KRW"])
        day = cu.date()
        close_today = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(hours=h)
        eff = day if cu >= close_today else day - timedelta(days=1)
        return history_close(symbol, eff.isoformat(), rng)
    except Exception:
        d = capture_dt.strftime("%Y-%m-%d") if hasattr(capture_dt, "strftime") else str(capture_dt)
        return history_close(symbol, d, rng)


def build(symbols, now=None):
    now = now or datetime.now(timezone.utc)
    prices, errors, fx = {}, {}, {"USDKRW": None}
    # 중복 제거 + 환율 심볼 보장
    syms = list(dict.fromkeys(list(symbols) + [FX_SYMBOL]))
    for sym in syms:
        try:
            m = fetch_one(sym)
            price = m.get("regularMarketPrice")
            mtime = m.get("regularMarketTime")
            stale = bool(mtime) and (now.timestamp() - mtime) > STALE_DAYS * 86400
            rec = {"price": price, "currency": m.get("currency"),
                   "name": m.get("longName") or m.get("shortName"),
                   "marketTime": _iso(mtime), "stale": stale}
            if sym == FX_SYMBOL:
                fx["USDKRW"] = price
            else:
                prices[sym] = rec
        except Exception as e:
            errors[sym] = str(e)[:200]
        time.sleep(0.2)
    out = {"asOf": now.isoformat().replace("+00:00", "Z"), "source": "yahoo-chart",
           "fx": fx, "prices": prices}
    if errors:
        out["errors"] = errors
    return out


def load_watchlist(path):
    data = json.load(open(path))
    out = []
    for x in data:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict) and x.get("symbol"):
            out.append(x["symbol"])
    return out


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: fetch_prices.py <watchlist.json> <prices.json>", file=sys.stderr)
        sys.exit(2)
    syms = load_watchlist(sys.argv[1])
    result = build(syms)
    json.dump(result, open(sys.argv[2], "w"), ensure_ascii=False, indent=2)
    n_ok, n_err = len(result["prices"]), len(result.get("errors", {}))
    print(f"prices.json 작성: {n_ok} OK, {n_err} 실패, "
          f"USDKRW={result['fx']['USDKRW']}, asOf={result['asOf']}")
    if n_err:
        print("실패:", json.dumps(result["errors"], ensure_ascii=False))
