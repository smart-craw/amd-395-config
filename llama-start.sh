#!/bin/bash
# 4 concurrent, each with 16 K context
exec distrobox enter llama-vulkan-radv -- llama-server \
  -m ~/models/Qwen3.5-9B-Q4_K_M.gguf \
  --host 0.0.0.0 \
  --port 8080 \
  -c 65536 -np 4 \
  --jinja \
  --chat-template-kwargs '{"enable_thinking":true}' \
  -ngl 99 --flash-attn on --no-mmap \
  -b 2048 -ub 512 \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.00
