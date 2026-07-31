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
IMAGE="eclipse-temurin:17-jdk"

COMPILE_SDK=35
BUILD_TOOLS=35.0.0

mkdir -p "$SDK_DIR" "$GRADLE_DIR"

# binfmt 전제 확인 — 없으면 빌드가 알 수 없는 곳에서 죽는다. 여기서 먼저 잡는다.
if [ ! -e /proc/sys/fs/binfmt_misc/qemu-x86_64 ]; then
  echo "❌ amd64 에뮬레이션이 등록돼 있지 않다. 먼저:"
  echo "   docker run --privileged --rm --network host tonistiigi/binfmt --install amd64"
  exit 1
fi

echo "== amd64 컨테이너에서 빌드  (task=$TASK)"
echo "·  SDK    $SDK_DIR"
echo "·  gradle $GRADLE_DIR"

exec docker run --rm --network host --platform linux/amd64 \
  -v "$REPO_DIR:/repo" \
  -v "$SDK_DIR:/sdk" \
  -v "$GRADLE_DIR:/gradle" \
  -e ANDROID_HOME=/sdk -e ANDROID_SDK_ROOT=/sdk -e GRADLE_USER_HOME=/gradle \
  -e PF_KEYSTORE="${PF_KEYSTORE:-}" \
  -e PF_KEYSTORE_PASSWORD="${PF_KEYSTORE_PASSWORD:-}" \
  -e PF_KEY_ALIAS="${PF_KEY_ALIAS:-}" \
  -e PF_KEY_PASSWORD="${PF_KEY_PASSWORD:-}" \
  -w /repo/android \
  "$IMAGE" bash -eu -c "
    export DEBIAN_FRONTEND=noninteractive
    # buildPython: Chaquopy가 pip(Pillow)를 돌릴 때 **빌드 머신의** 파이썬을 쓴다.
    if ! command -v python3 >/dev/null; then
      apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        unzip curl python3 python3-pip >/dev/null
    fi

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
