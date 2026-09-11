#!/bin/bash
# -hf unsloth/gemma-4-26B-A4B-it-qat-GGUF:UD-Q4_K_XL \
# 2 concurrent, each with 128 K context
# note that benchmarks look great with MTP but real world I'm only getting ~55 TPS on
# MTP vs 52 TPS on non-MTP...and Prefill is much slower with MPT
exec distrobox enter llama-vulkan-radv -- llama-server \
  -m ~/models/gemma4-26B/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf \
  --host 0.0.0.0 \
  --port 8080 \
  -c 262144 -np 2 \
  --jinja \
  --reasoning on \
  --reasoning-format auto \
  -ngl 99 --flash-attn on --load-mode none \
  -b 2048 -ub 512 \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --temp 1.0 --top-p 0.95 --top-k 64 # https://unsloth.ai/docs/models/gemma-4#recommended-settings
