##!/bin/bash
exec distrobox enter llama-vulkan-radv -- llama-server \
  # -m ~/models/Qwen3.8-27B-UD-Q4_K_M.gguf \
  # use --models-dir with the pi harness
  --models-dir ~/models \
  --host 0.0.0.0 \
  --port 8080 \
  -c 262144 -np 2 \
  --jinja \
  --reasoning on \
  --reasoning-format none \
  -ngl 99 --flash-attn on --no-mmap \
  -b 2048 -ub 512 \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --presence_penalty 0.0 --repeat_penalty 1.0 \
  --spec-type draft-mtp --spec-draft-n-max 4 \
  --temp 1.0 --top-p 0.95 --top-k 20 # https://huggingface.co/unsloth/Qwen3.8-27B-GGUF#best-practices
