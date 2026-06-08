#!/bin/bash
# 2 concurrent, each with 128 K context
# 4 concurrent made mtp way slower
exec distrobox enter llama-vulkan-radv -- llama-server \
  -m ~/models/gemma-4-26B-A4B-it-UD-Q5_K_M.gguf \
  --host 0.0.0.0 \
  --port 8080 \
  -c 262144 -np 2 \
  --model-draft ~/models/gemma-4-26B-A4B-it-MTP-Q8_0 \ # from https://huggingface.co/unsloth/gemma-4-26B-A4B-it-GGUF/tree/main/MTP
  --spec-type draft-mtp \
  --spec-draft-n-max 4 \
  --jinja \
  --reasoning on \
  --reasoning-format none \
  -ngl 99 --flash-attn on --no-mmap \
  -b 2048 -ub 512 \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --temp 1.0 --top-p 0.95 --top-k 64 # https://unsloth.ai/docs/models/gemma-4#recommended-settings
