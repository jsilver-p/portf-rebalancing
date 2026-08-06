// 루트 빌드 — 플러그인 버전만 고정한다(모듈에서 apply).
// Chaquopy 17.0.0 지원 범위: AGP 7.3~9.2 / Python 3.10~3.14 (chaquo.com/chaquopy/doc/current/versions.html)
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    id("com.chaquo.python") version "17.0.0" apply false
}
