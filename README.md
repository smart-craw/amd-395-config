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

Put llama-start.sh and llm-server.service in your $HOME/service folder.  Edit llama-start.sh to put the name of your distrobox.

Run

`systemctl --user daemon-reload`

`systemctl --user enable llm-server`

`systemctl --user start llm-server`

To run at boot without login:

`sudo loginctl enable-linger $USER`

## Other altneratives

The https://github.com/lemonade-sdk/llamacpp-rocm repository contains pre-built binaries for AMD systems.  I found that this was not quite as performant as distrobox, likely because vulkan has additional GPU optimizations that aren't fully mature in rocm.  As rocm matures I imagine the llamacpp-rocm will be an easier approach to performant AI use cases.

Example use:

`curl -o llama-rocm.zip -L https://github.com/lemonade-sdk/llamacpp-rocm/releases/download/b1223/llama-b1223-ubuntu-rocm-gfx1151-x64.zip`

`unzip llama-rocm.zip`

`chmod +x ./llama*`
