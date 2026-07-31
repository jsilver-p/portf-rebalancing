# APK 빌드 이미지 (amd64). 한 번 만들어 두고 재사용한다 — qemu 에뮬레이션에서 apt는 비싸다.
#
# **왜 bookworm인가**: Chaquopy는 buildPython이 타깃 파이썬과 **같은 마이너 버전**이어야 한다
# (실측: eclipse-temurin:17-jdk 는 Python 3.14뿐 →
#        "Couldn't find Python 3.11. See .../android.html#buildpython" 로 실패).
# Debian bookworm의 기본 python3가 정확히 3.11이라 app/build.gradle.kts 의 version="3.11"과 맞는다.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        openjdk-17-jdk-headless \
        python3 python3-pip python3-venv \
        curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
