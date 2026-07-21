# 로컬 VLM 파인튜닝으로 스크린샷 추출 에이전트 만들기

증권사 앱 스크린샷에서 보유내역을 JSON으로 뽑는 일을, 클라우드 API 없이 **온디바이스(Jetson)에서
돌아가는 파인튜닝 비전-언어 모델(VLM)**로 해결하는 전 과정을 담은 가이드다. "무엇을 왜 이렇게
하는지" 중심으로, 처음 따라 하는 개발자가 막히지 않게 도구·용어 설명과 공식 문서 링크를 곁들였다.

이 방법으로 이 레포는 **7B와 3B 두 모델**을 만들었다. **방법 자체는 베이스 모델 크기와 무관**하다 —
그래서 본문은 크기에 얽매이지 않게 서술하고, "어떤 크기·어떤 양자화를 왜 고르나"를 **5절의 별도
결정 단계**로 다룬다. 현재 프로덕션은 모바일 사정권의 `qwen2.5vl:3b-ft-q8`이고, 7B(`qwen2.5vl:7b-ft2-q4`)는
정확도 여유가 큰 참조/폴백이다.

> **왜 로컬인가.** 증권사 스크린샷은 계좌·잔액이 담긴 민감정보다. 외부 API로 보내지 않는 것이
> 요구사항이다. 그래서 추출 모델도, 그 학습도 전부 온디바이스에서 한다.
>
> **민감정보 취급 원칙(중요).** 학습 데이터는 **전부 합성**이다. 실제 스크린샷은 레이아웃·색·글꼴
> 같은 *생김새 참조*로만 쓰고, 픽셀과 숫자는 모두 렌더러가 만든 가짜다. 손에 든 실제 화면 몇 장은
> **평가 전용(held-out)**으로만 두고 학습에 절대 넣지 않는다 — 그래야 "외운 게 아니라 읽는다"를
> 정직하게 측정할 수 있다.

---

## 전체 그림

```
[1] 합성 데이터 렌더  ──►  [2] LoRA 학습  ──►  [3] 배포 경로에서 판정(+계속학습)
   (render_synth.py)       (LLaMA-Factory)        (ollama에서 채점)
        │                                                    │
        ▼                                                    ▼
[4] 병합 → GGUF 변환 → 양자화  ──►  [5] 크기·양자화 고르기  ──►  [6] ollama 등록
   (llama.cpp)                       (배포경로 채점으로 결정)      (Modelfile)
                                                                    │
                                                                    ▼
                                            [7] 에이전트 연결  ──►  [8] 端到端 검증
                                              (agent/server.py)      (parity)
```

각 단계 산출물:

| 단계 | 도구 | 산출물 |
|---|---|---|
| 1 데이터 | `eval/ft/render_synth.py` | 합성 이미지 + `synth.json`(학습쌍) |
| 2 학습 | LLaMA-Factory | LoRA 어댑터 |
| 3 판정 | ollama + `parity.py` | PASS/FAIL, 부족하면 계속학습 어댑터 |
| 4 변환 | llama.cpp | LLM `.gguf`(여러 양자화) + `mmproj-*.gguf` |
| 5 결정 | ollama + `parity.py` | 채택 크기·양자화 (예: `3b-ft-q8`) |
| 6 등록 | ollama `Modelfile` | `qwen2.5vl:<크기>-ft-q<n>` |
| 7 연결 | `agent/server.py` | 추출 API |
| 8 검증 | `verify_prod.py` + `parity.py` | 실화면 PASS + 속도 |

> **★ 이 문서를 관통하는 원칙 하나 먼저.**
> **정확도 판정은 학습 프레임워크(HF/PyTorch)가 아니라 실제 배포 경로(ollama/llama.cpp)에서 한다.**
> 같은 가중치라도 추론 구현이 다르면 미세한 수치 차이가 생기고, 그 차이가 "이 숫자가 평가금액이냐
> 손익이냐" 같은 **얇은 결정 경계**를 뒤집을 수 있다. 학습 쪽에서 100점이어도 서빙에서 틀릴 수
> 있으니, 합격 도장은 항상 배포와 같은 경로에서 찍는다. 아래 3·4·5·8단계가 이걸 실천한다.

---

## 0. 환경 준비

- 하드웨어: Jetson AGX Orin 64GB, JetPack 6, CUDA 12.6 (iGPU 공유 메모리)
- 학습/변환용 가상환경: `ft-spike/venv` (레포 밖 작업 공간)
- 핵심 스택: [PyTorch](https://pytorch.org) 2.8.0 + torchvision 0.23.0(Jetson 전용 휠),
  [Transformers](https://huggingface.co/docs/transformers) 4.52.4,
  [PEFT](https://huggingface.co/docs/peft)(LoRA 구현),
  [LLaMA-Factory](https://github.com/hiyouga/LLaMA-Factory) **v0.9.3**, gguf, PIL
- 변환 도구: [llama.cpp](https://github.com/ggml-org/llama.cpp) (소스 빌드 —
  `convert_hf_to_gguf.py` + `build/bin/llama-quantize`)

> 처음 보는 도구는 문서 끝 **부록 A(도구 한눈에)**와 **부록 B(주요 용어)**에 한 줄 설명 + 링크가 있다.

```bash
python3.10 -m venv ft-spike/venv && source ft-spike/venv/bin/activate
# Jetson 전용 torch/torchvision 휠 (jetson-ai-lab devpi)
pip install torch==2.8.0 torchvision==0.23.0 \
  --index-url https://pypi.jetson-ai-lab.io/jp6/cu126
pip install "transformers==4.52.4" peft gguf pillow
git clone --branch v0.9.3 https://github.com/hiyouga/LLaMA-Factory
pip install -e LLaMA-Factory
```

> ⚠ **플랫폼 전용 torch 휠을 쓸 것.** 범용 PyPI `torch`는 aarch64에서 CPU 전용 휠이 잡혀
> GPU를 못 쓴다. Jetson은 jetson-ai-lab devpi의 CUDA 휠을 명시해야 한다.
> ⚠ **`numpy==1.26.4`로 고정.** `gguf` 등이 numpy 2.x를 끌어올리면 torch↔numpy 브리지가
> 깨져 변환이 실패한다. 마지막에 `pip install "numpy==1.26.4"`로 되돌린다.
> ⚠ **LLaMA-Factory는 v0.9.3.** 최신 버전은 Python ≥3.11을 요구하는데 Jetson torch 휠은
> cp310뿐이다. py3.10과 맞는 마지막 계열이 v0.9.3.

---

## 1. 합성 학습 데이터 만들기

렌더러 `eval/ft/render_synth.py`가 6가지 화면 클래스(종합잔고 병합셀표, 외화예수금, 원화예수금,
보유잔고 상세, 자산현황 요약, 계좌별 잔고)를 프로그램으로 그린다. 각 이미지에 정답 JSON을 짝지어
sharegpt 형식(부록 B)으로 내보낸다.

```bash
source ft-spike/venv/bin/activate
# python render_synth.py <출력폴더> <시드> [클래스당 장수]
python eval/ft/render_synth.py ft-spike/data_full 42 400   # 6클래스 × 400 = 2,400장
```

**좋은 데이터가 갖춰야 할 것 (렌더러가 지키는 것들):**

- **입력 분포를 추론과 똑같이 맞춘다.** 실제 에이전트는 이미지를 `×0.5 LANCZOS` 축소 후 변의
  길이를 28px 배수로 스냅해서 모델에 넣는다(비전 토크나이저 패치 격자에 맞추면 토큰이 낭비 없이
  깔끔하다). 그래서 렌더 출력도 **동일하게 ×0.5 + 28px 스냅 JPEG**로 저장한다. 학습에서 본 것과
  추론에서 주는 것이 같은 픽셀 분포여야 성능이 나온다.
- **함정 케이스를 정답 규칙에 박아 넣는다.** 예: 계좌별 잔고 화면엔 계좌번호·예금주(가짜) 이름이
  같이 그려지지만, 정답 `name`에는 그것들을 **넣지 않는다**. "이름 칸엔 항목명만"이라는 규칙을
  모델이 데이터로 배우게 하려는 의도적 트랩이다.
- **증강으로 강인성 확보.** 밝기·대비를 살짝 흔들고 JPEG 품질을 85~95로 무작위화한다. 실제 기기
  캡처의 편차를 흡수한다.

데이터셋을 LLaMA-Factory에 등록한다 — `ft-spike/dataset_info.json`:

```json
{
  "synth_full":    { "file_name": "data_full/synth.json",   "formatting": "sharegpt",
                     "columns": {"messages": "messages", "images": "images"} },
  "synth_fxheavy": { "file_name": "data_fxheavy/synth.json", "formatting": "sharegpt",
                     "columns": {"messages": "messages", "images": "images"} }
}
```

각 학습쌍은 `{"messages":[{user:"<image>"+프롬프트}, {assistant: 정답JSON}], "images":[경로]}`
꼴이다. 프롬프트는 에이전트가 실제로 쓰는 것과 **같은 것**(`eval/harness/prompt4e.txt`)을 쓴다 —
학습·추론의 지시가 어긋나면 안 된다.

> ⚠ **held-out 실화면은 학습에 절대 넣지 않는다.** 실제 화면은 평가 채점(3·5·8단계)에서만 쓴다.
> 학습에 새면 "읽기"가 아니라 "외우기"를 측정하게 되어 평가가 거짓말을 한다.

---

## 2. LoRA 학습

베이스는 [Qwen2.5-VL](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct)-`<크기>`-Instruct
(이 레포는 7B와 3B 둘 다 학습). 전체를 다시 학습하지 않고 **LoRA**(원 가중치는 얼리고 작은 어댑터만
학습 — 부록 B) 어댑터만 얹는다. 레시피는 크기별로 `model_name_or_path`·`output_dir`만 다른 쌍이다:
`eval/ft/lora_full.yaml`(7B) / `eval/ft/lora_full_3b.yaml`(3B). 내용(발췌):

```yaml
model_name_or_path: .../models/Qwen2.5-VL-<크기>-Instruct
finetuning_type: lora
lora_rank: 16
lora_target: all          # 어텐션+MLP 전 선형층에 LoRA (언어부; ViT는 동결)
dataset: synth_full
template: qwen2_vl        # Qwen2.5-VL 전용 채팅/이미지 템플릿
image_max_pixels: 760000  # 이미지당 상한 (메모리 보호)
per_device_train_batch_size: 1
gradient_accumulation_steps: 8
learning_rate: 1.0e-4
max_steps: 300            # 2,400장 ≈ 1 epoch
bf16: true
gradient_checkpointing: true
dataloader_num_workers: 0
```

```bash
llamafactory-cli train eval/ft/lora_full_3b.yaml   # 3B ~4시간 / 7B ~7시간(Orin)
```

**왜 이 설정인가:** `lora_rank: 16`은 이 정도 도메인 특화엔 충분하면서 가볍다. `lora_target: all`로
언어부의 모든 선형층에 어댑터를 얹어 표 읽기 규칙을 넉넉히 학습시킨다. **ViT(비전 인코더)는 동결**
(부록 B) — 글자·표 인식 능력은 이미 훌륭하니 건드리지 않고, "이 도메인의 표를 어떻게 JSON으로
옮기느냐"만 가르친다. 배치 1 × 누적 8로 유효 배치 8을 만든다(iGPU 메모리 절약).

> ⚠ **메모리 절약 설정을 처음부터 켠다.** iGPU는 시스템 메모리를 공유해 쉽게 OOM 난다.
> `image_max_pixels`(큰 렌더 상한), `gradient_checkpointing`, `dataloader_num_workers: 0`을
> 처음부터 두는 게 안전하다. 세로로 긴 화면이 특히 위험하니 렌더러에서 픽셀 상한도 함께 건다.

---

## 3. 배포 경로에서 판정하고, 부족하면 마진을 넓힌다

여기가 이 프로젝트의 핵심 관행이다. 학습이 끝나면 **바로 채점하는데, 학습 프레임워크가 아니라
서빙 경로(ollama)에서** 한다. 절차는 4·6단계로 GGUF까지 만들어 ollama에 올린 뒤, held-out 실화면
8장을 넣어 `parity.py`로 채점하는 것이다(8단계 참고).

**마진이 얇은 곳을 계속학습으로 넓힌다.** 특정 화면 클래스(예: 외화예수금)에서 값 하나가 아슬아슬하게
갈려 틀리면, 그 클래스를 많이 담은 데이터로 **짧게 이어서 학습**한다 — `eval/ft/lora_cont.yaml`(7B) /
`eval/ft/lora_cont_3b.yaml`(3B):

```yaml
adapter_name_or_path: .../out/lora_full   # 앞 어댑터에서 이어감
dataset: synth_fxheavy                     # 해당 클래스 비중을 크게 (+증강)
learning_rate: 5.0e-5                       # 낮춰서 (미세 조정)
max_steps: 100
```

```bash
python eval/ft/render_synth.py ft-spike/data_fxheavy 7 ...   # 해당 클래스 편중 세트
llamafactory-cli train eval/ft/lora_cont.yaml               # 약 2시간
```

계속학습은 결정 경계를 그 클래스 쪽으로 넓혀, 추론 수치차가 흔들어도 정답이 유지되게 만든다.

> ⚠ **계속학습은 "특정 클래스" 도구다 — "전역 정밀도 부족"의 해법이 아니다.** 어떤 클래스 하나가
> 아슬아슬할 때는 잘 듣지만, 모델 전체의 양자화 마진이 얇아 여기저기 아슬아슬하면 한 클래스를
> 고치는 동안 **다른 클래스로 실패가 옮겨갈 수 있다**(5절 3B 사례). 그 경우의 옳은 처방은 계속학습이
> 아니라 더 무거운 양자화나 더 큰 모델이다.
> ⚠ **HF에서 통과해도 서빙에서 같지 않다.** 학습 프레임워크 추론으론 전부 맞던 가중치가
> llama.cpp 서빙에선 한 화면에서 틀릴 수 있다(양자화 탓이 아니라 추론 구현의 수치 미세차 탓).
> **합격 판정은 반드시 배포 경로에서** 내린다. 이 원칙 하나가 "학습은 됐는데 실제로는 틀림"을 막는다.

---

## 4. 어댑터 병합 → GGUF 변환 → 양자화

ollama가 읽는 형식(**GGUF** — 부록 B)으로 바꾼다. 먼저 LoRA 어댑터를 베이스에 병합해 일반 HF
모델로 만든다 — `eval/ft/export_merge2.yaml`(7B) / `eval/ft/export_merge_3b.yaml`(3B):

```yaml
model_name_or_path: .../models/Qwen2.5-VL-<크기>-Instruct
adapter_name_or_path: .../out/lora_cont   # 또는 계속학습 안 했으면 lora_full
export_dir: .../out/merged_<크기>_ft
```

```bash
llamafactory-cli export eval/ft/export_merge_3b.yaml
```

[llama.cpp](https://github.com/ggml-org/llama.cpp)로 GGUF 변환 — VLM은 **언어부(LLM)와 비전
프로젝터(mmproj)를 따로** 뽑는다(mmproj 개념은 부록 B). 그리고 **양자화는 여러 정밀도를 만들어
둔다** — 어느 것을 채택할지는 5절에서 배포경로 채점으로 고른다:

```bash
M=ft-spike/out/merged_3b_ft ; O=ft-spike/out           # 예: 3B
# (1) 언어부 → f16 GGUF
python llama.cpp/convert_hf_to_gguf.py "$M" --outfile "$O/3b-ft-f16.gguf" --outtype f16
# (2) 비전 프로젝터 → mmproj (같은 스크립트에 --mmproj)
python llama.cpp/convert_hf_to_gguf.py "$M" --mmproj --outfile "$O/mmproj-3b-ft.gguf"
# (3) 언어부 양자화 — 후보를 둘 이상 만든다
llama.cpp/build/bin/llama-quantize "$O/3b-ft-f16.gguf" "$O/3b-ft-q8.gguf"   Q8_0
llama.cpp/build/bin/llama-quantize "$O/3b-ft-f16.gguf" "$O/3b-ft-q4km.gguf" Q4_K_M
```

산출물 예(3B): 언어부 `3b-ft-q8.gguf`(3.28G)·`3b-ft-q4km.gguf`(1.93G) + 비전 `mmproj-3b-ft.gguf`(1.34G).
mmproj는 양자화하지 않고 그대로 쓴다.

> ⚠ **양자화 정밀도는 배포 경로 채점으로 고른다(3단계 원칙).** "q4가 제일 가벼우니 q4"가 아니다.
> f16/q8/q4를 만들어 ollama에서 각각 채점해, **정확도를 지키는 가장 가벼운 것**을 택한다 — 그
> 판정을 다음 절에서 한다.

---

## 5. 모델 크기·양자화 고르기 — 배포경로 채점으로

**모델 크기도, 양자화 정밀도도 기본값이 아니라 *결정*이다.** 여러 조합(f16/q8/q4 × 7B/3B)을 만들어
4·6단계로 ollama에 올리고 held-out 8장을 `parity.py`로 채점한 뒤, **정확도가 통과하는 가장 가볍고
빠른(=모바일 사정권) 조합**을 고른다. 이 레포의 실측 결과:

| 모델 | 채택 양자화 | q4_K_M | q8_0 | 속도(신선) | LLM+mmproj 크기 |
|---|---|---|---|---|---|
| 7B | **q4_K_M** | **PASS**(마진 여유) | (통과, 무거움) | 13.9s/장 | 4.68G+1.35G |
| 3B | **q8_0** | **FAIL**(한 행 반전) | **PASS** | 10.6s/장 | 3.28G+1.34G ≈ 4.6G |

읽는 법과 교훈:

- **용량이 작을수록 q4 마진이 얇다.** 7B는 q4에서도 여유 있게 통과했지만, 3B는 q4에서 두 행이
  뒤집혔다(한화오션 qty·price 놓침·'한화오선' 오독, ACE금현물 price=1.00). **같은 3B 가중치인데
  f16·q8은 정확했다** → 이건 3B의 능력 한계가 아니라 **양자화 마진** 문제. 그래서 3B는 q8을 택한다.
- **⚠ 이때 계속학습으로 q4를 억지로 살리려 하지 말 것.** 실제로 detail 편중 증강셋으로 3B를 100step
  계속학습해 봤더니, q4에서 ACE금현물은 고쳤지만 **FX/USD 화면을 깨뜨렸다** — 실패가 고쳐진 게
  아니라 옮겨간 것이다. 전역 q4 마진 부족의 옳은 처방은 **더 무거운 양자화(q8)나 더 큰 모델**이었다.
  (첫 full 어댑터가 최선이었다 — 매몰비용에 끌려 반복하지 말 것.)
- **크기 선택 기준 = 배포경로에서 통과하는 가장 작은 조합.** 3B-q8은 LLM+mmproj ≈ **4.6GB**로
  고사양폰 사정권 → 모바일 스윗스폿이라 프로덕션으로 채택. 7B-q4는 정확도 여유가 커 참조/폴백.
- **이 방식이 소형 모델도 구제하는가? — 그렇다(구조적으로).** 파인튜닝 전 base 3B는 이 과제에서
  출력이 **완전히 붕괴**했다(재현율 0/31). 같은 합성 LoRA-FT가 그걸 배포경로 parity 전판 PASS로
  되살렸다. 즉 "붕괴"는 FT가 완치하고, 남는 건 서빙 정밀도(양자화) 하한뿐이다.

> **핵심 한 줄:** 정확도의 최종 판정도, 크기·양자화 선택도 전부 **배포 경로(ollama)에서** 한다.
> 학습 프레임워크의 점수는 착수 신호일 뿐 합격 도장이 아니다.

**구체 인스턴스 매핑(단일 출처)** — 본문의 `<크기>` 자리는 아래 파일로 치환한다:

| | 7B (참조/폴백) | 3B (프로덕션) |
|---|---|---|
| 베이스 | `Qwen2.5-VL-7B-Instruct` | `Qwen2.5-VL-3B-Instruct` |
| 학습 레시피 | `lora_full.yaml`(+`lora_cont.yaml`) | `lora_full_3b.yaml` |
| 병합 레시피 | `export_merge2.yaml` | `export_merge_3b.yaml` |
| Modelfile | `Modelfile.7bft2-q4` | `Modelfile.3bft-q8` |
| ollama 태그 | `qwen2.5vl:7b-ft2-q4` | `qwen2.5vl:3b-ft-q8` |
| 가중치(레포 밖) | `ft-spike/out/7b-ft2-*.gguf` | `ft-spike/out/3b-ft-*.gguf` |

---

## 6. ollama에 등록

[ollama](https://ollama.com) [Modelfile](https://github.com/ollama/ollama/blob/main/docs/modelfile.mdx) —
언어부와 mmproj **두 개의 FROM**, Qwen2.5-VL 공식 채팅 템플릿, 결정성을 위한 낮은 temperature
(예: `eval/ft/Modelfile.3bft-q8`):

```dockerfile
FROM .../3b-ft-q8.gguf
FROM .../mmproj-3b-ft.gguf
TEMPLATE """...(qwen2.5vl 공식 템플릿)..."""
SYSTEM You are a helpful assistant.
PARAMETER temperature 0.0001
```

```bash
ollama create qwen2.5vl:3b-ft-q8 -f eval/ft/Modelfile.3bft-q8
```

배칭·상시 로드는 ollama 서버 환경변수라 systemd 드롭인으로 준다:

```bash
sudo systemctl edit ollama
#   [Service]
#   Environment="OLLAMA_NUM_PARALLEL=2" "OLLAMA_KEEP_ALIVE=-1"
sudo systemctl restart ollama
```

- `OLLAMA_NUM_PARALLEL=2`: 비전 요청 2개를 동시에 받아 디코드 스텝을 배칭 → 가중치 스트리밍을
  공유해 총 처리량↑.
- `OLLAMA_KEEP_ALIVE=-1`: 모델을 메모리에 상주시켜 요청마다의 콜드 로딩(~90s)을 없앤다.

> ⚠ **`num_ctx`를 매 요청에 명시한다(예: 8192).** 지정하지 않으면 서버가 컨텍스트를 과다하게
> 자동 확장해 메모리를 낭비할 수 있다.

---

## 7. 로컬 에이전트에 연결

추출 API 서버 `agent/server.py`가 ollama를 호출한다. 세 가지가 맞물려야 한다.

- **입력 전처리를 학습 분포와 똑같이.** `resample_half_b64()`가 들어온 스크린샷을 `×0.5 LANCZOS`
  + 28px 스냅으로 리샘플한다 — **1단계 렌더러와 같은 처리**. 학습에서 본 픽셀과 추론 입력이 같아진다
  (이미지 토큰도 ~1/4로 줄어 빨라진다). 모든 ollama 호출부(핸들러·`extract`·`_vision`)에 적용한다.
- **병렬 비전 호출.** `extract_batch()`가 화면 여러 장을 `NP`개씩 동시에 쏜다(ollama 슬롯 수와
  일치). 요청은 화면당 1개라 "행→화면" 귀속은 구조적으로 보존된다.
- **같은 프롬프트.** `eval/harness/prompt4e.txt` — 학습 때 쓴 그 지시문.

기본값(`agent/server.py`, `agent/run-agent.sh`) — 리샘플·프롬프트·NP은 7B/3B 공통이라 모델만 바뀐다:

```python
MODEL       = "qwen2.5vl:3b-ft-q8"    # 현행 프로덕션 (폴백: qwen2.5vl:7b-ft2-q4)
NP          = 2
PROMPT_FILE = "eval/harness/prompt4e.txt"
```

기동은 `agent/run-agent.sh` — ollama 확인 → **모델 워밍업**(첫 요청 콜드로딩을 기동 시점으로 이동)
→ 서버 실행 → [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/)
터널로 공개 URL 발급.

```bash
bash agent/run-agent.sh                          # 기본 3b-ft-q8
MODEL=qwen2.5vl:7b-ft2-q4 bash agent/run-agent.sh  # 폴백으로 7B 기동
```

---

## 8. 端到端 검증

프로덕션 코드 경로 그대로(리샘플 + 프롬프트 + 모델 + ollama) 실화면 8장을 돌려 채점한다.

```bash
python eval/speed3/verify_prod.py                # extract_batch로 8장 → raw 덤프
python eval/harness/parity.py eval/results/<덤프폴더> \
       --no-llm --controls                       # 채점
```

`MODEL` 환경변수로 대안 조합(예: 7B-q4, 또는 3B의 다른 양자화)을 겨냥해 같은 잣대로 채점할 수 있다 —
5절의 크기·양자화 결정이 바로 이 방식이다.

`parity.py`가 보는 것:

- **재현율**(모든 행을 빠짐없이) · **환각 0**(없는 행 지어내지 않기)
- 필드별 정확도(value/qty/price/cost/broker/accountType …)
- **게이트 침묵**: 계좌 합계 대조에서 불일치 경고가 없어야 정상
- **음성 대조군**: 화면 누락·빈 화면·환각행·순서 셔플을 일부러 주입해 **경고가 떠야** 통과(게이트가
  살아있는지 확인)

> ⚠ **속도는 "신선한" 상태에서 잰다.** ollama 인스턴스를 재시작해 캐시를 비운 뒤 측정하고,
> 응답의 prefill/decode 처리율(metrics)로 캐시 히트가 아님을 사후 확인한다. 같은 이미지를 다시
> 넣으면 캐시가 남아 실제보다 빠르게 보인다.

---

## 결과와 유지보수

- **프로덕션 = `qwen2.5vl:3b-ft-q8`**: 10.6s/장(신선), LLM+mmproj ≈ 4.6GB로 모바일 사정권.
  held-out 실화면 **배포 경로 전판 PASS**.
- **참조/폴백 = `qwen2.5vl:7b-ft2-q4`**: 13.9s/장(32B 기준 62s/장 대비 4.5배), 정확도 마진이 더 커
  안전한 폴백. 역시 배포 경로 전판 PASS.
- **유지보수**: 앱 UI가 바뀌면 렌더러(`render_synth.py`)에 새 레이아웃을 반영하고 재학습하면 된다
  (반나절 규모). 실화면을 모으거나 라벨링할 필요가 없는 게 이 합성-데이터 방식의 큰 장점이다.

의사결정 이력·대안 비교(32B 중간해, 크기·양자화 트레이드오프 상세)는 `eval/DECISION.md`의 **v2.5**
(7B 채택)와 **v2.6**(3B 모바일·q8 전환) 항목을 참고. 이 문서는 "어떻게 만드나", DECISION.md는
"무엇을 왜 골랐나"를 담는다.

---

## 부록 A. 도구 한눈에

| 도구 | 역할(한 줄) | 공식 문서 |
|---|---|---|
| PyTorch | 학습 런타임(텐서·자동미분·GPU 연산) | https://pytorch.org |
| Transformers | 모델 정의·로딩의 표준 프레임워크 | https://huggingface.co/docs/transformers |
| PEFT | LoRA 등 경량 미세조정 구현 | https://huggingface.co/docs/peft |
| LLaMA-Factory | LoRA 학습·병합을 YAML+CLI로 | https://github.com/hiyouga/LLaMA-Factory |
| Qwen2.5-VL | 베이스 비전-언어 모델 | https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct |
| llama.cpp | HF→GGUF 변환·양자화·C/C++ 추론 | https://github.com/ggml-org/llama.cpp |
| ollama | 로컬 모델 서빙(Modelfile로 등록) | https://ollama.com · [Modelfile](https://github.com/ollama/ollama/blob/main/docs/modelfile.mdx) |
| cloudflared | 로컬 서버를 임시 공개 URL로 터널 | [TryCloudflare](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/) |

## 부록 B. 주요 용어

**꼭 이해하고 넘어갈 개념**

- **LoRA (Low-Rank Adaptation)** — 원본 가중치는 얼려두고, 각 선형층에 작은 저랭크 행렬 한 쌍(A·B)만
  더해 **그것만** 학습한다. 전체 미세조정 대비 학습 파라미터·메모리가 수십~수백분의 1이고, 산출물은
  베이스와 분리된 작은 "어댑터" 파일이다. 이 과제엔 "표를 JSON으로 옮기는 규칙"만 얹으면 되므로
  저용량으로 충분하다. (논문 https://arxiv.org/abs/2106.09685 · 개념 가이드
  https://huggingface.co/docs/peft)
- **양자화(quantization, q4_K_M / q8_0)** — 가중치를 낮은 비트로 압축해 파일 크기·메모리 대역폭을
  줄이고 속도를 올린다. `q8_0`은 8비트(정확도 거의 무손실, 대신 큼), `q4_K_M`은 4비트(가장 가벼움,
  정밀도 손실↑). **어느 비트가 배포 경로에서 정확도를 지키는지가 5절 결정의 핵심**이다.
- **mmproj / VLM 이중 경로** — VLM은 이미지를 보는 인코더(**ViT**)와 글을 생성하는 언어모델(**LLM**)
  두 부분으로 이뤄진다. GGUF로 낼 때 LLM과, 비전 특징을 언어 임베딩 공간으로 잇는 **프로젝터
  (mmproj)**를 따로 뽑아 ollama에 둘 다 준다.
- **ViT 동결(freeze)** — 비전 인코더는 학습에서 얼린다. 글자·표 인식력은 이미 충분하니 건드리지
  않고 "이 도메인 표 → JSON" 매핑만 LoRA로 가르쳐 출력 붕괴·과적합을 막는다.
- **GGUF** — llama.cpp/ollama가 읽는 단일 파일 모델 포맷(가중치 + 메타데이터). HF 체크포인트를
  변환해 만든다. (스펙 https://github.com/ggml-org/ggml/blob/master/docs/gguf.md)

**한 줄 용어**

- **어댑터** — LoRA 학습의 산출물(작은 가중치 델타 파일). 베이스에 병합하면 일반 모델이 된다.
- **sharegpt 포맷** — `{messages, images}` 리스트로 대화형 학습쌍을 적는 데이터 스키마.
- **template(qwen2_vl)** — 모델별 프롬프트·이미지 토큰 배치 규칙. 학습·추론이 같은 걸 써야 한다.
- **held-out** — 학습에 넣지 않고 평가에만 쓰는 데이터. "외우기"가 아닌 "읽기"를 재는 장치.
- **prefill / decode 처리율** — prefill=입력(이미지·프롬프트) 한 번에 읽는 단계, decode=토큰을
  하나씩 생성하는 단계. 각 t/s로 속도를 분해해 캐시 히트가 아님을 확인한다.
- **`num_ctx`** — 요청당 컨텍스트 길이 상한. 명시 안 하면 과다 확장돼 메모리를 낭비할 수 있다.
- **`OLLAMA_KEEP_ALIVE` / `OLLAMA_NUM_PARALLEL`** — 모델 상주 시간 / 동시 처리 슬롯 수(디코드 배칭).
- **parity / 게이트 / 음성 대조군** — parity=필드별 정오 채점, 게이트=계좌 합계 대조 경보,
  음성 대조군=일부러 오류를 주입해 경보가 **떠야** 통과(안전장치가 살아있는지 확인).
