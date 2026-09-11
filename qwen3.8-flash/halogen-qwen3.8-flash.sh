#!/bin/bash
# see qwen-next.md for network creation
podman run --rm --network webserver-net \
  -p 8731:8731 \
  --device /dev/kfd --device /dev/dri --group-add keep-groups \
  --security-opt seccomp=unconfined --ipc=host --ulimit memlock=-1:-1 \
  -v ~/models/qwen3.8-flash-halogen:/models:ro \
  ghcr.io/peonist-ai/halogen-flash-server:0.4.4
