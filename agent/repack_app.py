#!/usr/bin/env python3
"""index.html의 임베드 앱 문서(193번째 줄 문자열)를 안전하게 추출·교체하는 단일 통제점.

index.html은 dc-runtime(gzip 블롭) + 앱 문서(.dc.html을 JSON 문자열로 임베드)로 패키징돼 있다.
이 레포엔 재빌드(dc 패키저)가 없고 design-source/*.dc.html은 배포본과 다른 구버전이라,
편집 대상은 임베드 문자열 그 자체다. 손편집은 위험하므로 여기서만 decode/encode한다.

  extract  index.html → app.html            (가독 문서로 추출)
  embed    app.html   → index.html          (편집한 문서를 다시 심음)

인코딩은 원본과 바이트동일이 아니라 'JSON 디코드 등가 + HTML 안전(</… 이스케이프)'을 보장한다.
런타임은 이 문자열을 JSON.parse 하므로 디코드 등가면 충분하다.
"""
import json, sys

IDX = "index.html"
DOC_LINE = 192  # 0-indexed (193번째 줄)


def _encode(doc: str) -> str:
    # </script>·</style> 등이 임베드 <script> 컨텍스트를 깨지 않도록 '</' 를 이스케이프.
    # / 는 '/' 로 디코드되므로 값은 동일하고 HTML 파싱만 안전해진다.
    return json.dumps(doc, ensure_ascii=False).replace("</", "<\\u002F")


def extract(idx_path=IDX):
    lines = open(idx_path, encoding="utf-8").read().split("\n")
    return json.loads(lines[DOC_LINE])


def embed(doc: str, idx_path=IDX):
    lines = open(idx_path, encoding="utf-8").read().split("\n")
    lines[DOC_LINE] = _encode(doc)
    open(idx_path, "w", encoding="utf-8").write("\n".join(lines))
    # 왕복 검증: 방금 쓴 줄을 다시 디코드해 입력과 동일한지
    assert extract(idx_path) == doc, "round-trip mismatch"


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "extract":
        out = sys.argv[2] if len(sys.argv) > 2 else "app.html"
        open(out, "w", encoding="utf-8").write(extract())
        print(f"extracted → {out} ({len(extract())} chars)")
    elif cmd == "embed":
        src = sys.argv[2] if len(sys.argv) > 2 else "app.html"
        embed(open(src, encoding="utf-8").read())
        print(f"embedded {src} → {IDX} (round-trip OK)")
    else:
        print("usage: repack_app.py extract [app.html] | embed [app.html]")
