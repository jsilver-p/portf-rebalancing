#!/bin/bash
# 학습(lora_full_3b3) 완료 후: 병합 → GGUF(f16 LLM + mmproj) → q8 양자화 → ollama 등록.
# 실행: bash build_gguf_3b3.sh   (ft-spike 디렉토리에서)
set -euo pipefail
cd /home/omr/workspaces/ft-spike
LC=/home/omr/workspaces/llama.cpp
PY=venv/bin/python

echo "### 1) 어댑터 병합 → merged_3b_ft3"
venv/bin/llamafactory-cli export export_merge_3b3.yaml

echo "### 2) LLM f16 GGUF"
$PY $LC/convert_hf_to_gguf.py out/merged_3b_ft3 --outfile out/3b-ft3-f16.gguf --outtype f16

echo "### 3) mmproj GGUF"
$PY $LC/convert_hf_to_gguf.py out/merged_3b_ft3 --mmproj --outfile out/mmproj-3b-ft3.gguf

echo "### 4) q8 양자화"
$LC/build/bin/llama-quantize out/3b-ft3-f16.gguf out/3b-ft3-q8.gguf Q8_0

echo "### 5) 산출물 확인"
ls -la out/3b-ft3-q8.gguf out/mmproj-3b-ft3.gguf

echo "### 6) ollama 등록 (API 경유, sudo 불필요)"
ollama create qwen2.5vl:3b-ft3-q8 -f Modelfile.3bft3-q8

echo "DONE: qwen2.5vl:3b-ft3-q8"
