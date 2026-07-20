#!/usr/bin/env python3
"""index.html의 임베드 앱 문서(193번째 줄 문자열)를 안전하게 추출·교체하는 단일 통제점.

index.html은 dc-runtime(gzip 블롭) + 앱 문서(.dc.html을 JSON 문자열로 임베드)로 패키징돼 있다.
이 레포엔 재빌드(dc 패키저)가 없고 design-source/*.dc.html은 배포본과 다른 구버전이라,
편집 대상은 임베드 문자열 그 자체다. 손편집은 위험하므로 여기서만 decode/encode한다.

  extract  index.html → app.html            (가독 문서로 추출)
  embed    app.html   → index.html          (편집한 문서를 다시 심음)
  check    두 쪽이 일치하는지 검사           (불일치 시 exit 1)

인코딩은 원본과 바이트동일이 아니라 'JSON 디코드 등가 + HTML 안전(</… 이스케이프)'을 보장한다.
런타임은 이 문자열을 JSON.parse 하므로 디코드 등가면 충분하다.
"""
import json, sys

IDX = "index.html"
SRC = "design-source/app.current.html"
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


def check(src_path=SRC, idx_path=IDX):
    """index.html의 임베드 문서 == src_path 인지 검사. 불변식을 지키는 게이트.

    둘의 동기화는 'embed를 잊지 않는 규율'로만 유지된다 — 규율은 깨지므로 여기서 강제한다.
    반환: 일치하면 None, 어긋나면 사람이 읽을 진단 문자열.
    """
    doc = extract(idx_path)
    src = open(src_path, encoding="utf-8").read()
    if doc == src:
        return None
    # 어디서부터 갈렸는지 짚어줘야 '뭘 해야 하나'가 바로 나온다.
    # 안내는 정식 경로(SRC/IDX)로 적는다 — 훅이 넘기는 임시경로를 찍으면 따라할 수 없는 명령이 된다.
    a, b = src.split("\n"), doc.split("\n")
    at = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))
    return (f"{SRC} 와 {IDX}:{DOC_LINE + 1} 의 앱 문서가 다릅니다.\n"
            f"  원본 {len(a)}줄 / 임베드 {len(b)}줄 · 첫 차이 {at + 1}행\n"
            f"  원본  : {a[at][:90] if at < len(a) else '(없음)'}\n"
            f"  임베드: {b[at][:90] if at < len(b) else '(없음)'}\n"
            f"  → 원본이 최신이면: python3 agent/repack_app.py embed {SRC}\n"
            f"  → index.html을 직접 고쳤다면 그 수정은 버려집니다(원본에 다시 반영할 것).")


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
    elif cmd == "check":
        # 훅은 스테이징된 blob을 임시파일로 떠서 검사하므로 두 경로 다 받는다.
        src = sys.argv[2] if len(sys.argv) > 2 else SRC
        idx = sys.argv[3] if len(sys.argv) > 3 else IDX
        bad = check(src, idx)
        if bad:
            print("✗ " + bad, file=sys.stderr)
            sys.exit(1)
        print(f"✓ {IDX}:{DOC_LINE + 1} == {SRC} (동기화됨)")
    else:
        print("usage: repack_app.py extract [app.html] | embed [app.html] | check [src] [index.html]")
