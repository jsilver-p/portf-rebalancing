#!/bin/sh
# 클론마다 1회. .git/config 는 커밋되지 않으므로 훅·머지드라이버는 여기서 등록한다.
set -e
cd "$(dirname "$0")/.."
git config core.hooksPath .githooks
git config merge.pf-app.name "index.html 임베드 앱 문서 3-way 머지"
git config merge.pf-app.driver ".githooks/merge-app-html %O %A %B %L"
echo "✓ pre-commit 게이트 + index.html 머지 드라이버 등록됨"
