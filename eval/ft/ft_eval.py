#!/usr/bin/env python3
"""FT-4: LoRA 어댑터를 held-out 실화면 8장(×0.5 LANCZOS)에 HF 추론으로 평가.

출력은 run_extract.py와 같은 JSON 스키마로 eval/results/<tag>/에 저장 → parity.py로 채점.
사용: venv/bin/python ft_eval.py <adapter_dir> <out_tag>
"""
import json, os, sys, time
import torch
from PIL import Image
from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration
from peft import PeftModel

BASE = "/home/omr/workspaces/ft-spike/models/Qwen2.5-VL-7B-Instruct"
SHOTS = "/home/omr/workspaces/portf-rebalancing/eval/speed3/shots_r050_lanczos"
PROMPT = open("/home/omr/workspaces/portf-rebalancing/eval/harness/prompt4e.txt").read().strip()
RESULTS = "/home/omr/workspaces/portf-rebalancing/eval/results"

def main():
    adapter, tag = sys.argv[1], sys.argv[2]
    outdir = os.path.join(RESULTS, tag)
    os.makedirs(outdir, exist_ok=True)

    processor = AutoProcessor.from_pretrained(
        BASE, min_pixels=256 * 28 * 28, max_pixels=700000)
    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        BASE, torch_dtype=torch.bfloat16, device_map="cuda")
    if adapter != "none":
        model = PeftModel.from_pretrained(model, adapter)
    model.eval()

    for fn in sorted(os.listdir(SHOTS)):
        if not fn.lower().endswith((".jpg", ".png")):
            continue
        img = Image.open(os.path.join(SHOTS, fn)).convert("RGB")
        messages = [{"role": "user", "content": [
            {"type": "image", "image": img},
            {"type": "text", "text": PROMPT}]}]
        text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = processor(text=[text], images=[img], return_tensors="pt").to("cuda")
        t0 = time.time()
        with torch.inference_mode():
            out = model.generate(**inputs, max_new_tokens=1024, do_sample=False,
                                 temperature=None, top_p=None, top_k=None)
        dt = time.time() - t0
        raw = processor.batch_decode(out[:, inputs.input_ids.shape[1]:],
                                     skip_special_tokens=True)[0]
        n_img_tok = int((inputs.input_ids == model.config.image_token_id).sum())
        with open(os.path.join(outdir, fn + ".json"), "w") as f:
            json.dump({"image": fn, "seconds": round(dt, 1), "raw": raw,
                       "num_ctx": "hf", "prompt_file": "prompt4e.txt",
                       "metrics": {"image_tokens": n_img_tok,
                                   "eval_count": int(out.shape[1] - inputs.input_ids.shape[1])}},
                      f, ensure_ascii=False, indent=1)
        print(f"{fn}  {dt:.1f}s  imgtok={n_img_tok}  chars={len(raw)}", flush=True)
    print("DONE ->", outdir)

if __name__ == "__main__":
    main()
