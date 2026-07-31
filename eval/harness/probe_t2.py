#!/usr/bin/env python3
"""T2 역량 탐침 — A1에서 실제로 실패한 5개 케이스를 텍스트 LLM에 그대로 묻는다.

T1(OCR+기하)이 못 메운 칸이 '식별자 교정·표기 정규화'다. 그 칸을 소형 텍스트 LLM이
메울 수 있는지, 메울 수 있다면 **몇 B부터**인지가 사다리의 T2 rung이다.

격리: :11435 (전용 모델 스토어). 라이브 :11434 무접촉.
"""
import json, sys, time, urllib.request

HOST = "http://127.0.0.1:11435"

# 프롬프트는 파이프라인이 실제로 시킬 법한 형태로 쓴다 — '근거 없으면 모른다'를 명시적으로 허용한다.
# (레포의 게이트 철학. 모른다고 답하면 그건 실패가 아니라 정상이다.)
CASES = [
    dict(
        id="assetClass-ACE",
        prompt="한국 상장 ETF의 자산군을 분류한다. 보기: 성장주, 배당주, 채권, 원자재, 현금, 혼합.\n"
               "상품명: ACE 엔비디아밸류체인액티브\n"
               "보기 중 하나만 답하라. 다른 말 금지.",
        answer=["성장주"],
    ),
    dict(
        id="assetClass-TIGER",
        prompt="한국 상장 ETF의 자산군을 분류한다. 보기: 성장주, 배당주, 채권, 원자재, 현금, 혼합.\n"
               "상품명: TIGER 차이나휴머노이드로봇\n"
               "보기 중 하나만 답하라. 다른 말 금지.",
        answer=["성장주"],
    ),
    dict(
        id="broker-Super365",
        prompt="증권 앱 화면에서 읽은 브랜드명이다. 이 브랜드를 운영하는 증권사의 정식 회사명을 답하라.\n"
               "브랜드명: Super365\n"
               "회사명만 답하라. 모르면 '모름'이라고 답하라.",
        answer=["메리츠"],
    ),
    dict(
        id="ticker-IVV-IWV",
        prompt="OCR이 미국 ETF 티커를 'IWV'로 읽었으나 글꼴상 'VV'와 'W'는 혼동될 수 있다.\n"
               "화면에 표시된 이 종목의 1주 단가는 $751 이다.\n"
               "참고 시세: IVV = $751, IWV = $390.\n"
               "실제 티커는 무엇인가? 티커만 답하라.",
        answer=["IVV"],
    ),
    dict(
        id="ticker-alphabet",
        prompt="한국 증권 앱이 표기한 미국 주식 종목명을 미국 거래소 티커로 변환하라.\n"
               "종목명: 알파벳A\n"
               "티커만 답하라. 모르면 '모름'이라고 답하라.",
        answer=["GOOGL", "GOOG"],
    ),
]


def ask(model, prompt):
    body = json.dumps({
        "model": model, "prompt": prompt, "stream": False,
        # DECISION.md:247 — 이 인스턴스는 num_ctx를 매 요청 명시한다.
        "options": {"num_ctx": 8192, "temperature": 0},
    }).encode()
    req = urllib.request.Request(HOST + "/api/generate", body,
                                 {"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read())
    return d.get("response", "").strip(), time.time() - t


def main():
    models = sys.argv[1:] or ["gemma3:1b", "gemma3:4b"]
    for m in models:
        print(f"\n===== {m} =====")
        ok = 0
        for c in CASES:
            try:
                out, dt = ask(m, c["prompt"])
            except Exception as e:
                print(f"  {c['id']:22s} ERROR {e}")
                continue
            flat = " ".join(out.split())[:90]
            hit = any(a.lower() in out.lower() for a in c["answer"])
            ok += hit
            print(f"  {c['id']:22s} {'PASS' if hit else 'FAIL'}  {dt:5.1f}s  {flat!r}")
        print(f"  → {ok}/{len(CASES)}")


if __name__ == "__main__":
    main()
