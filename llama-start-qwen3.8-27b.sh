#!/bin/bash
# 2 concurrent, each with 128 K context
exec distrobox enter llama-vulkan-radv -- llama-server \
  # use routing mode with pi.dev
  --models-preset ~/models/preset.ini \
  --host 0.0.0.0 \
  --port 8000 \
  --jinja \
  --reasoning on \
  --reasoning-format none \
  -ngl 99 --flash-attn on --load-mode none \
  -b 2048 -ub 512 \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.00 \
  --presence_penalty 0.0 --repeat_penalty 1.0  # https://huggingface.co/unsloth/Qwen3.8-27B-GGUF#best-practices
