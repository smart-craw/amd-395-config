#!/bin/bash
# 4 concurrent, each with 128 K context
exec distrobox enter llama-vulkan-radv -- llama-server \
  -m ~/models/Qwen3.5-35B-A3B-Q4_K_S.gguf \
  --host 0.0.0.0 \
  --port 8080 \
  -c 524288 -np 4 \
  --jinja \
  --chat-template-kwargs '{"enable_thinking":true}' \
  --reasoning-format none \
  -ngl 99 --flash-attn on --no-mmap \
  -b 2048 -ub 512 \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.00 # https://unsloth.ai/docs/models/qwen3.5#thinking-mode
