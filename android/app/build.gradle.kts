plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// 레포 루트 — 파이썬 소스와 index.html의 **단일 출처**. 복사본을 만들지 않는다.
val repoRoot = rootProject.projectDir.parentFile

android {
    namespace = "com.portfrebalance.edge"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.portfrebalance.edge"
        // 26: Chaquopy·ML Kit 모두 여유 있게 넘고, WebView가 시스템 업데이트를 받는 하한.
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0-spike"

        ndk {
            // 실기기(S23)는 arm64-v8a. x86_64는 에뮬레이터용 — 넣으면 APK가 두 배가 되므로 뺀다.
            abiFilters += listOf("arm64-v8a")
        }
    }

    signingConfigs {
        // 서명은 **로컬 keystore로만** 한다. 값은 환경변수로 받고 레포에 넣지 않는다.
        // (없으면 이 설정 자체를 만들지 않아 debug 빌드는 그대로 된다.)
        // isNullOrEmpty로 보는 이유: docker -e PF_KEYSTORE="" 는 **빈 문자열**을 넘긴다.
        // null 검사만 하면 통과해 `file("")`에서 죽는다.
        if (!System.getenv("PF_KEYSTORE").isNullOrEmpty()) {
            create("release") {
                storeFile = file(System.getenv("PF_KEYSTORE"))
                storePassword = System.getenv("PF_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("PF_KEY_ALIAS")
                keyPassword = System.getenv("PF_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false      // Chaquopy는 파이썬을 리플렉션으로 부른다 — 축소는 나중에
            signingConfig = signingConfigs.findByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

chaquopy {
    defaultConfig {
        version = "3.11"

        // **핵심**: 파이썬 소스를 복사하지 않고 레포의 agent/를 그대로 가리킨다.
        // finalize·enrich·게이트·수량 사다리가 포팅 없이 APK에 들어간다 — 두 번째 진실의
        // 출처를 만들지 않는 게 이 설계의 요점이다.
        pip {
            install("Pillow")   // EXIF 캡처시각(수량 사다리 T4의 기준시각). 유일한 외부 의존.
        }
    }
    sourceSets {
        getByName("main") {
            srcDir(File(repoRoot, "agent"))
        }
    }
}

// index.html도 단일 출처를 지킨다 — 빌드 시 레포 루트에서 assets로 복사한다.
val copyIndexHtml by tasks.registering(Copy::class) {
    from(File(repoRoot, "index.html"))
    into(layout.buildDirectory.dir("generated/assets"))
}
android.sourceSets.getByName("main").assets.srcDir(layout.buildDirectory.dir("generated/assets"))
tasks.matching { it.name.startsWith("merge") && it.name.endsWith("Assets") }
    .configureEach { dependsOn(copyIndexHtml) }

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.3")
    // 한국어 온디바이스 텍스트 인식. 번들형(bundled)이라 최초 실행에 다운로드가 없다.
    implementation("com.google.mlkit:text-recognition-korean:16.0.1")
}
