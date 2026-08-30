#!/bin/bash
# -hf unsloth/gemma-4-26B-A4B-it-qat-GGUF:UD-Q4_K_XL \
# 2 concurrent, each with 128 K context
# 4 concurrent made mtp way slower
exec distrobox enter llama-vulkan-radv -- llama-server \
  -m ~/models/gemma4-26B/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf \
  --model-draft ~/models/gemma4-26B/mtp-gemma-4-26B-A4B-it-Q4_0.gguf \
  --host 0.0.0.0 \
  --port 8080 \
  -c 262144 -np 2 \
  --spec-type draft-mtp \
  --spec-draft-n-max 4 \
  --jinja \
  --reasoning on \
  --reasoning-format auto \
  -ngl 99 --flash-attn on --load-mode none \
  -b 2048 -ub 512 \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --temp 1.0 --top-p 0.95 --top-k 64 # https://unsloth.ai/docs/models/gemma-4#recommended-settings
