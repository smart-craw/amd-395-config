# Instructions

Much of this is based off https://github.com/kyuz0/amd-strix-halo-toolboxes.

Eg:

```sh
distrobox create llama-rocm-10.0 \
  --image docker.io/kyuz0/amd-strix-halo-toolboxes:rocm-10.0 \
  -- --device /dev/dri --device /dev/kfd --group-add video --group-add render --group-add sudo \
  --security-opt seccomp=unconfined
```

## Initial setup
Follow https://github.com/kyuz0/amd-strix-halo-toolboxes to create distrobox installs.  Fastest for me with `qwen3.5 9b` was llama-vulkan-radv, though as rocm matures it may start being the better alternative.

## Minimize GPU allocation in BIOS

The BIOS can pre-allocate GPU memory.  This would work well for (eg) video games which could then use the dedicated pre-allocated memory.  It does not work well for LLMs, which use the shared memory available to the OS.

To get around this, minimize the GPU allocation in BIOS.  On my Beelink this is found in Advanced → AMD CBS → NBIO Common Options → GFX Configuration → Dedicated Graphics Memory.

## Provide sufficient GPU memory

Open /etc/default/grub with sudo and find the line GRUB_CMDLINE_LINUX_DEFAULT.

Set GRUB_CMDLINE_LINUX_DEFAULT to the following:

`iommu=pt amdgpu.gttsize=120000 ttm.pages_limit=30720000`

Then run

```sh
sudo update-grub
```

```sh
sudo reboot
```

## Start service

Put your choice of llama-start-*.sh in your $HOME/service folder.  Edit the shell script to put the name of your distrobox.  Add  llm-server.service to ~/.config/systemd/user/, creating the folder if it doesn't already exist.  Update llm-server.service to point to your actual shell script name.

Run

```sh
systemctl --user daemon-reload
```

```sh
systemctl --user enable llm-server
```

```sh
systemctl --user start llm-server
```

To run at boot without login:

```sh
sudo loginctl enable-linger $USER
```

## Run benchmarks

This only works without mtp.  You need to use another type of benchmarking (eg a custom testing scaffold) to fully test mtp.

```sh
llama-bench -pg 512,128 \
-m ~/models/gemma4-26B/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf \
-m ~/models/qwen3.8-27B/Qwen3.8-27B-UD-Q4_K_M.gguf \
-b 2048 -ub 512 \
-ngl 99 --flash-attn on --load-mode none
```

## Monitor dashboard

A dependency-free dashboard for whichever model server is running: `monitor/serve.py` hosts the page and proxies `/health`, `/v1/models`, `/metrics`, and `/cache` from the same origin, so no CORS setup is needed.  Point it at any server (defaults to `http://llm.home:8080`) and open `http://localhost:8090/`.

Monitoring is two layers (see [ADR 0001](./docs/adr/0001-prometheus-grafana-owns-metrics.md)): Prometheus + Grafana own `/metrics` -- history, rate math, retention, alerting -- and `monitor/serve.py` owns "right now".  The proxy keeps the live status page for the JSON endpoints (`/health`, `/v1/models`, `/cache`, which are not Prometheus metrics), answers `GET /metrics` as a raw scrape passthrough for Prometheus when the model server is not directly reachable, and grows deep links into the matching Grafana panels.  Nothing in the stack talks to anything outside the box; the Prometheus/Grafana compose stack lives in `monitoring/` once landed.

```sh
python3 monitor/serve.py --url http://llm.home:8080 --port 8090
```

Prometheus can scrape through the proxy instead of reaching the model server directly: point a job at the proxy with `metrics_path: /metrics`, and use `/metrics?url=http://llm.home:8001` to front several servers from one proxy.  The body is the upstream exposition text verbatim plus the proxy's own `llm_monitor_*` series (scrape success, latency, upstream status, parse errors, series count, build info), so "is the monitor actually scraping?" is a PromQL question.  Like `?url=`, that route is an information-disclosure decision -- it hands out model ids, traffic shape and prompt-cache behaviour -- which is why the default bind address is loopback and `--host 0.0.0.0` stays deliberate.

There is a `llm-monitor.service` systemd user unit for it, installed the same way as `llm-server.service`.  See [monitor/README.md](monitor/README.md).

## Other altneratives

### ROCM pre-built binaries
The https://github.com/lemonade-sdk/llamacpp-rocm repository contains pre-built binaries for AMD systems.  I found that this was not quite as performant as distrobox, likely because vulkan has additional GPU optimizations that aren't fully mature in rocm.  As rocm matures I imagine the llamacpp-rocm will be an easier approach to performant AI use cases.

Example use:

```sh
curl -o llama-rocm.zip -L https://github.com/lemonade-sdk/llamacpp-rocm/releases/download/b1223/llama-b1223-ubuntu-rocm-gfx1151-x64.zip
```

```sh
unzip llama-rocm.zip
```

```sh
chmod +x ./llama*
```

### Lemonade with NPU...not recommended since it is slow and not fully open source but is technically an option

https://lemonade-server.ai/flm_npu_linux.html

```sh
sudo add-apt-repository ppa:lemonade-team/stable
```

```sh
sudo apt update
```

```sh
sudo apt install libxrt-npu2 amdxdna-dkms lemonade-server
```

```sh
sudo reboot
```


```sh
curl -L https://github.com/FastFlowLM/FastFlowLM/releases/download/v0.9.38/fastflowlm_0.9.38_ubuntu25.10_amd64.deb -o fastflowlm.deb
```

```sh
sudo apt install ./fastflowlm.deb
```
