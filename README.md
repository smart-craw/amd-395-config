# Instructions

## Initial setup
Follow https://github.com/kyuz0/amd-strix-halo-toolboxes to create distrobox installs.  Fastest for me with `qwen3.5 9b` was llama-vulkan-radv, though as rocm matures it may start being the better alternative.

## Provide sufficient GPU memory

Open /etc/default/grub with sudo and find the line GRUB_CMDLINE_LINUX_DEFAULT.

Set GRUB_CMDLINE_LINUX_DEFAULT to the following:

`iommu=pt amdgpu.gttsize=120000 ttm.pages_limit=30720000`

Then run

`sudo update-grub`

`sudo reboot`

## Start service

Put your choice of llama-start-*.sh in your $HOME/service folder.  Edit the shell script to put the name of your distrobox.  Add  llm-server.service to ~/.config/systemd/user/, creating the folder if it doesn't already exist.  Update llm-server.service to point to your actual shell script name.

Run

`systemctl --user daemon-reload`

`systemctl --user enable llm-server`

`systemctl --user start llm-server`

To run at boot without login:

`sudo loginctl enable-linger $USER`

## Run benchmarks

`
llama-bench -pg 512,128 \
-m ~/models/Qwen3.5-9B-Q4_K_M.gguf \
-m ~/models/gemma-4-26B-A4B-it-UD-Q4_K_S.gguf \
-m ~/models/Qwen3.5-35B-A3B-Q4_K_S.gguf \
-b 2048 -ub 512 \
-ngl 99 --flash-attn on
`

## Other altneratives

### ROCM pre-built binaries
The https://github.com/lemonade-sdk/llamacpp-rocm repository contains pre-built binaries for AMD systems.  I found that this was not quite as performant as distrobox, likely because vulkan has additional GPU optimizations that aren't fully mature in rocm.  As rocm matures I imagine the llamacpp-rocm will be an easier approach to performant AI use cases.

Example use:

`curl -o llama-rocm.zip -L https://github.com/lemonade-sdk/llamacpp-rocm/releases/download/b1223/llama-b1223-ubuntu-rocm-gfx1151-x64.zip`

`unzip llama-rocm.zip`

`chmod +x ./llama*`

### Lemonade with NPU...not recommended since it is slow and not fully open source but is technically an option

https://lemonade-server.ai/flm_npu_linux.html

`sudo add-apt-repository ppa:lemonade-team/stable`

`sudo apt update`

`sudo apt install libxrt-npu2 amdxdna-dkms lemonade-server`

`sudo reboot`

`curl -L https://github.com/FastFlowLM/FastFlowLM/releases/download/v0.9.38/fastflowlm_0.9.38_ubuntu25.10_amd64.deb -o fastflowlm.deb`

`sudo apt install ./fastflowlm.deb`
