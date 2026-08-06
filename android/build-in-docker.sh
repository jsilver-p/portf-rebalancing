#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# APK 빌드 — **amd64 컨테이너 안에서**. 재실행 안전(idempotent).
#
# 왜 컨테이너인가: 이 호스트(Jetson Orin)는 aarch64인데 Android build-tools의
# aapt2·zipalign은 x86-64 ELF다. 실측:
#     $ file build-tools/34.0.0/aapt2
#       ELF 64-bit LSB pie executable, x86-64 ...
#     $ ./aapt2 version   →  cannot execute binary file: Exec format error
# Google은 Linux/aarch64용 build-tools를 배포하지 않는다. 그래서 qemu-x86_64
# binfmt를 등록하고(아래 전제) amd64 이미지 안에서 빌드한다.
# d8·apksigner는 순수 Java라 이 제약과 무관하다.
#
# 전제(한 번만):
#   docker run --privileged --rm --network host tonistiigi/binfmt --install amd64
#   → /proc/sys/fs/binfmt_misc/qemu-x86_64 가 생긴다. 확인: docker run --rm \
#       --network host --platform linux/amd64 alpine uname -m   → x86_64
#
# --network host를 쓰는 이유: 이 호스트의 커널에 iptables raw 테이블이 없어
# docker 기본 bridge 네트워킹이 실패한다(Tegra 커널). host면 정상.
#
# 서명(선택): PF_KEYSTORE / PF_KEYSTORE_PASSWORD / PF_KEY_ALIAS / PF_KEY_PASSWORD
#   keystore는 **레포 밖**에 두고 커밋하지 않는다. 없으면 debug APK만 나온다.
#
# 실행:  bash android/build-in-docker.sh [assembleDebug|assembleRelease]
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

TASK="${1:-assembleDebug}"
ANDROID_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$ANDROID_DIR")"

# 호스트에 남겨 재사용하는 캐시(컨테이너는 매번 버린다) — 레포 밖.
SDK_DIR="${PF_ANDROID_SDK:-$HOME/android-sdk-x86}"
GRADLE_DIR="${PF_GRADLE_HOME:-$HOME/.gradle-x86}"
IMAGE="portf-android-build:1"

COMPILE_SDK=35
BUILD_TOOLS=35.0.0

mkdir -p "$SDK_DIR" "$GRADLE_DIR"

# binfmt 전제 확인 — 없으면 빌드가 알 수 없는 곳에서 죽는다. 여기서 먼저 잡는다.
if [ ! -e /proc/sys/fs/binfmt_misc/qemu-x86_64 ]; then
  echo "❌ amd64 에뮬레이션이 등록돼 있지 않다. 먼저:"
  echo "   docker run --privileged --rm --network host tonistiigi/binfmt --install amd64"
  exit 1
fi

# 빌드 이미지 — 없을 때만 만든다(에뮬레이션에서 apt는 비싸므로 한 번만).
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "== 빌드 이미지 생성 $IMAGE (최초 1회, 수 분 소요)"
  docker build --network host --platform linux/amd64 -t "$IMAGE" "$ANDROID_DIR"
fi

# keystore는 **컨테이너 안으로 마운트**해야 한다. 호스트 경로를 그대로 넘기면
# :app:validateSigningRelease 가 "Keystore file ... not found"로 죽는다(실측).
# 읽기 전용으로 붙이고 컨테이너 경로로 바꿔서 넘긴다 — 레포에는 들어가지 않는다.
KS_MOUNT=()
KS_IN_CONTAINER=""
if [ -n "${PF_KEYSTORE:-}" ]; then
  [ -f "$PF_KEYSTORE" ] || { echo "❌ keystore 없음: $PF_KEYSTORE"; exit 1; }
  KS_MOUNT=(-v "$(readlink -f "$PF_KEYSTORE"):/keystore.jks:ro")
  KS_IN_CONTAINER=/keystore.jks
fi

echo "== amd64 컨테이너에서 빌드  (task=$TASK)"
echo "·  SDK    $SDK_DIR"
echo "·  gradle $GRADLE_DIR"
[ -n "$KS_IN_CONTAINER" ] && echo "·  서명   $PF_KEYSTORE → $KS_IN_CONTAINER"

exec docker run --rm --network host --platform linux/amd64 \
  -v "$REPO_DIR:/repo" \
  -v "$SDK_DIR:/sdk" \
  -v "$GRADLE_DIR:/gradle" \
  "${KS_MOUNT[@]}" \
  -e ANDROID_HOME=/sdk -e ANDROID_SDK_ROOT=/sdk -e GRADLE_USER_HOME=/gradle \
  -e PF_KEYSTORE="$KS_IN_CONTAINER" \
  -e PF_KEYSTORE_PASSWORD="${PF_KEYSTORE_PASSWORD:-}" \
  -e PF_KEY_ALIAS="${PF_KEY_ALIAS:-}" \
  -e PF_KEY_PASSWORD="${PF_KEY_PASSWORD:-}" \
  -w /repo/android \
  "$IMAGE" bash -eu -c "
    # buildPython 확인 — Chaquopy는 타깃과 **같은 마이너 버전**의 파이썬을 빌드 머신에서 찾는다.
    # 안 맞으면 :app:installDebugPythonRequirements 에서 죽는다(이미지가 이걸 보장한다).
    echo \"== buildPython \$(python3 -V)\"

    # Android SDK — 마운트된 /sdk에 남아 다음 실행에서 재사용된다.
    if [ ! -x /sdk/cmdline-tools/latest/bin/sdkmanager ]; then
      echo '== cmdline-tools 설치'
      curl -fsSL -o /tmp/clt.zip \
        https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip
      mkdir -p /sdk/cmdline-tools && unzip -q /tmp/clt.zip -d /sdk/cmdline-tools
      mv /sdk/cmdline-tools/cmdline-tools /sdk/cmdline-tools/latest
    fi
    export PATH=/sdk/cmdline-tools/latest/bin:\$PATH
    yes | sdkmanager --licenses >/dev/null 2>&1 || true
    sdkmanager 'platform-tools' 'platforms;android-$COMPILE_SDK' \
               'build-tools;$BUILD_TOOLS' >/dev/null

    # Gradle 배포본 — 래퍼 jar를 레포에 커밋하지 않기 위해 여기서 직접 받는다.
    GRADLE_VER=8.9
    if [ ! -x /gradle/dist/gradle-\$GRADLE_VER/bin/gradle ]; then
      echo '== gradle 설치'
      mkdir -p /gradle/dist
      curl -fsSL -o /tmp/g.zip https://services.gradle.org/distributions/gradle-\$GRADLE_VER-bin.zip
      unzip -q /tmp/g.zip -d /gradle/dist
    fi

    echo '== gradle \$GRADLE_VER / \$(uname -m)'
    /gradle/dist/gradle-\$GRADLE_VER/bin/gradle --no-daemon $TASK
  "
