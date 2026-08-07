#!/usr/bin/env python3
"""APK 진입점 — Kotlin이 부르는 **유일한** 파이썬 함수.

앱 안에서 `server.py`를 그대로 띄우고 WebView가 127.0.0.1로 붙는다. 즉 Termux(A2)와
APK(A3)가 **같은 서버 코드·같은 API·같은 index.html**을 쓴다. 새 API 표면을 만들지 않는 게
요점이다 — 게이트·수량 사다리가 한 벌만 존재해야 한다.

server.py는 환경변수를 **임포트 시점에** 읽는다. 그래서 여기서 먼저 세팅하고 나중에 임포트한다.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def start(index_path, data_dir, port=8899, granularity="element"):
    """Kotlin에서 호출. 블로킹이다 — 반드시 백그라운드 스레드에서 부를 것."""
    os.environ["INDEX_PATH"] = str(index_path)
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["PORT"] = str(port)
    # **127.0.0.1 고정.** 앱이 스크린샷 API를 같은 Wi-Fi에 열어두면 안 된다.
    os.environ["BIND"] = "127.0.0.1"
    # 기기에는 비전 LLM이 없다 — OCR+기하만. ollama를 부르는 경로로 절대 가지 않는다.
    os.environ["EXTRACT"] = "ocr"
    os.environ["OCR_ENGINE"] = "mlkit"
    # 기본은 **element** — ML Kit의 Line 그룹핑에 걸지 않고 `ocr.merge_lines`로 우리가 합친다.
    # bind가 견디는 봉투는 R∈[0.6,1.8]이고 우리 임계값 1.0은 그 한가운데다(docs §4.8).
    # 'line'을 쓰면 봉투 안인지가 ML Kit 손에 넘어간다.
    os.environ["OCR_MLKIT_GRANULARITY"] = str(granularity)
    # 기기에는 검증할 LLM이 없다. 그런데 `finalize(use_llm=True)`가 기본이라 증권사 브랜드
    # **웹검색은 나가고** LLM이 없어 실패해 None이 된다 — 값도 못 얻고 요청만 나가는 낭비다.
    # 시세·심볼만 남긴다(`outbound.py` 정책). 증권사는 **화면 표기·계좌 상속으로만** 푼다 —
    # 캐시는 없다(규칙). 브랜드만 찍히는 앱에서는 `증권사 미상` 경고가 남는다. 알려진 구멍이다.
    os.environ.setdefault("PF_OUTBOUND", "prices")

    os.makedirs(data_dir, exist_ok=True)
    if HERE not in sys.path:
        sys.path.insert(0, HERE)

    import server
    server.serve()
