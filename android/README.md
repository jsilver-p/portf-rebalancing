# android/ — 제3자 배포용 APK (Stage A3)

Termux(A2)는 배포 형태가 아니다. 여기서 **서명된 APK**로 만들어 내 개발 기기가 아닌 폰에
설치되는 것까지가 합격선이다.

## 설계 — 무엇을 옮기지 *않았는가*

| 층 | APK에서 | 왜 |
|---|---|---|
| 게이트·불변식·수량 사다리 (`finalize.py`, `enrich`) | **파이썬 그대로** (Chaquopy) | Kotlin으로 옮기면 900줄짜리 **두 번째 진실의 출처**가 생긴다. 그 순간 파리티는 무의미해진다 |
| 서버·API (`server.py`) | **그대로**, 127.0.0.1에 바인드 | A2와 같은 API·같은 `index.html`. 새 표면을 만들지 않는다 |
| 프론트 (`index.html`) | 빌드 시 레포 루트에서 복사 | 사본을 커밋하지 않는다 |
| OCR 엔진 | **여기만 교체** — RapidOCR → ML Kit | `agent/ocr.py`의 어댑터 계약(`[{text,x,y,w,h,conf}]`)에서 끊긴다. `bind.py` 이하는 재검증 대상이 아니다 |
| 비전 LLM | **없음** | 기기에 신경망은 ML Kit 하나뿐 |

`chaquopy.sourceSets`가 레포의 `agent/`를 **직접 가리킨다**(복사 없음). 즉 A1에서 파리티를
통과한 바로 그 코드가 APK 안에서 돈다.

## 빌드

이 호스트(Jetson Orin)는 **aarch64**인데 Android build-tools의 `aapt2`·`zipalign`은
**x86-64 ELF**다 — Google은 Linux/aarch64용을 배포하지 않는다. 실측:

```
$ file build-tools/34.0.0/aapt2
  ELF 64-bit LSB pie executable, x86-64 ...
$ ./aapt2 version
  cannot execute binary file: Exec format error
```

그래서 amd64 컨테이너에서 빌드한다. 한 번만:

```bash
docker run --privileged --rm --network host tonistiigi/binfmt --install amd64
```

이후:

```bash
bash android/build-in-docker.sh assembleDebug     # → app/build/outputs/apk/debug/
bash android/build-in-docker.sh assembleRelease   # 서명하려면 아래 환경변수
```

`--network host`가 필요한 이유: 이 호스트 커널에 iptables `raw` 테이블이 없어 docker 기본
bridge 네트워킹이 실패한다(Tegra 커널).

### 서명

keystore는 **레포 밖**에 두고 커밋하지 않는다. 값은 환경변수로만 넘긴다.

```bash
keytool -genkeypair -v -keystore ~/portf-agent/release.jks \
  -keyalg RSA -keysize 4096 -validity 10000 -alias portf

PF_KEYSTORE=~/portf-agent/release.jks PF_KEYSTORE_PASSWORD=… \
PF_KEY_ALIAS=portf PF_KEY_PASSWORD=… \
  bash android/build-in-docker.sh assembleRelease
```

## 제3자 배포 체크리스트 (= Stage A3 합격 기준)

| 항목 | 기준 | 상태 |
|---|---|---|
| 크기 | Play base 모듈 압축 **200MB** / 총 설치 **4GB** | ML Kit 한국어 + Python 런타임 — **측정 필요** |
| 가속기 | 플랫폼 API 경유 | ML Kit Text Recognition v2 ✅ |
| 권한 | 스크린샷 접근 | **photo picker** — 저장소 권한 0개 ✅ |
| 프라이버시 검증가능성 | 제3자가 **확인할 수 있어야** 한다 | 아래 참조 ✅ |
| Play Data Safety | 수집·전송 신고 | "수집 없음, 기기 내 처리"가 **코드로 참** |
| 서명 | keystore, versionCode | 절차 준비됨 — 실행 대기 |
| 법·표기 | 금융 조언 아님, OSS 라이선스(ML Kit) | **미작성** |
| **실기기 설치·동작** | 개발 기기가 **아닌** 폰에서 8장 추출 완주 | ❌ **미검증 — 기기 필요** |

### 프라이버시 주장을 어떻게 검증하나

"믿어주세요"가 아니라 세 군데서 확인된다:

1. `AndroidManifest.xml` — 권한은 `INTERNET` 하나. 저장소·미디어 권한 없음.
2. `res/xml/network_security_config.xml` — 평문 통신을 **127.0.0.1로만** 허용, 그 외 차단.
3. 추출 코드 경로에 네트워크 호출이 없다:
   `MlKitOcr.recognize` → `bind.bind` → `finalize` 게이트. 나가는 통신은 **심볼·시세·환율**
   조회뿐이고(`fetch_prices.py`, `resolve*.py`) **스크린샷과 금액은 나가지 않는다.**

기기 네트워크를 끈 상태에서 **추출이 성공**하는 것으로 실증한다(시세만 나중에 붙는다).

## 미검증 — 정직하게

- **박스 단위(granularity)가 ML Kit 스왑의 유일한 실질 위험이다.** RapidOCR은 구절 단위
  검출 박스를 내고, ML Kit은 Block > Line > Element 계층이다. `bind.py`의 `_amount_of`는
  '금액과 수익률이 한 박스에' 들어오는 것을 전제한다. Line이 가장 가깝다고 보고 기본값으로
  뒀지만 **기기 없이는 확인할 수 없다.** `OCR_MLKIT_GRANULARITY`로 line/element/block을
  바꿔가며 재서 고정한다.
- ML Kit 한국어 인식 품질 자체가 RapidOCR PP-OCRv5와 같은지 미측정. 파리티를 다시 받아야 한다.
- APK 크기, 콜드 스타트 시간, 실제 s/장 전부 미측정.
