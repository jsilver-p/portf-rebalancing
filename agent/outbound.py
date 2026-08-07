#!/usr/bin/env python3
"""외부 인터넷으로 나가는 **유일한 통로**. 정책 한 곳, 목록 한 곳.

프라이버시 주장("스크린샷과 금액은 나가지 않는다")은 나가는 것을 **열거할 수 있을 때만**
검증 가능하다. 그래서 아웃바운드를 이 함수 하나로 모으고, 호스트마다 무엇이 나가는지 적는다.
여기를 지나지 않는 외부 요청은 없다 — 새 호출을 추가하려면 이 표에 먼저 등록해야 한다.

`127.0.0.1`(ollama·WebView)은 여기 대상이 아니다. 기기를 벗어나지 않는다.

정책 스위치 `PF_OUTBOUND`:
  all    (기본) 전부 허용
  prices 시세·환율·심볼만. **증권사 브랜드 검색을 끈다**
  none   전부 차단 — 추출은 그대로 되고 시세·심볼만 비는지 확인하는 프라이버시 게이트용

CLI:  python3 agent/outbound.py        # 나가는 것 표를 그대로 출력
"""
import os
import urllib.error
import urllib.request

# **브라우저 흉내를 내지 않는다.** 버전을 박은 UA는 썩는다 — 두 번 실측했다(2026-08-07):
#   · Yahoo:  Chrome/120 UA + Accept-Language → **HTTP 429** (같은 시각 아래 헤더는 정상)
#   · Naver:  Chrome/120 UA → **HTTP 403 · 0/10** (같은 시각 아래 헤더는 **10/10**, 중앙 0.63s)
# 낡은 Chrome 버전이 차단 대상이 됐고, 아침에 되던 채널이 오후에 죽었다. 버전 없는 UA 하나로
# 통일한다 — 흉내를 정교하게 할수록 상대의 봇 규칙에 더 잘 걸린다.
_UA_PLAIN = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# 목적(purpose) → (호스트, 헤더, 나가는 내용, 안 나가는 것, 없으면 어떻게 되나)
ROUTES = {
    "symbol": ("ac.stock.naver.com", _UA_PLAIN,
               "종목명 한 개 (예: '엔비디아', 'TIGER 차이나휴머노이드로봇')",
               "수량·금액·계좌·증권사·스크린샷",
               "symbol=None → 시세·수량 유도 불가, 화면값만 남는다"),
    "quote":  ("query1.finance.yahoo.com", _UA_PLAIN,
               "심볼 한 개 (예: 'NVDA', '005930.KS', 환율은 'KRW=X')",
               "수량·금액·계좌·증권사·스크린샷",
               "price/FX 없음 → T3·T4 수량 유도와 재평가가 꺼진다"),
    "broker": ("search.naver.com", _UA_PLAIN,
               "증권사 브랜드 토큰 한 개 (예: 'Super365')",
               "계좌번호·계좌별칭·종목명·금액·스크린샷 (brand_token이 미리 거른다)",
               "broker=None + '증권사 미상' 경고 — 지어내지 않는다"),
}
LEVELS = {"all": set(ROUTES), "prices": {"symbol", "quote"}, "none": set()}


class Blocked(RuntimeError):
    """정책이 막았다 — 네트워크 오류가 아니다. 호출부는 '근거 없음'으로 처리해야 한다."""


def level():
    return os.environ.get("PF_OUTBOUND", "all").strip().lower()


def enabled(purpose):
    return purpose in LEVELS.get(level(), LEVELS["all"])


def get(purpose, url, timeout=15):
    """등록된 목적으로만 나간다. 반환은 raw bytes — 파싱은 호출부 몫."""
    host, headers, *_ = ROUTES[purpose]              # 미등록 목적이면 여기서 KeyError
    if not enabled(purpose):
        raise Blocked(f"{purpose}({host}) 차단됨 — PF_OUTBOUND={level()}")
    if not url.startswith(f"https://{host}/"):       # 목적과 호스트가 어긋나면 나가지 않는다
        raise Blocked(f"{purpose}의 호스트가 아니다: {url[:60]}")
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


if __name__ == "__main__":
    print(f"PF_OUTBOUND={level()}\n")
    for p, (host, _h, sends, never, without) in ROUTES.items():
        print(f"[{p}] {host}   {'허용' if enabled(p) else '차단'}")
        print(f"   나가는 것 : {sends}")
        print(f"   안 나가는 것: {never}")
        print(f"   없으면    : {without}\n")
