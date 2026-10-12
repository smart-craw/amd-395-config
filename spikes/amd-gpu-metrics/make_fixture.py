#!/usr/bin/env python3
"""Generate a sysfs-shaped fixture of a gfx1151 (Strix Halo) card.

The dev containers this repo is developed in have no GPU, no `/dev/dri` and no
`/sys/class/drm`, so anything that wants to exercise an amdgpu reader needs a
double — the same job `monitor/tests/mock_upstream.py` does for the model server.

The layout is the one the kernel actually produces (kernel 6.18 sources:
`amdgpu_vram_mgr.c`, `amdgpu_gtt_mgr.c`, `pm/amdgpu_pm.c`, `amdgpu_fdinfo.c`),
including the two things that bite:

* `class/drm/cardN/device` is a **symlink** into `devices/pci.../`, and so is
  `class/hwmon/hwmonN` — a reader that does not follow symlinks, or a container
  that mounted `/sys/class/drm` alone, sees nothing;
* not every attribute exists. VRAM is tiny here because the BIOS carve-out was
  minimised (root README), and the model lives in the **GTT** whose size comes
  from `amdgpu.gttsize=120000` / `ttm.pages_limit=30720000`.

Values are plausible, not measured — they exist to make the parser and the
"absent is not zero" rule testable. Real numbers come from running
`amdgpu_sysfs_probe.py probe` on the box.

    ./make_fixture.py            # -> fixtures/sysfs-gfx1151
    ./make_fixture.py --all      # + sysfs-broken-device-symlink and sysfs-denied variants
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

# Values in the shape the kernel prints them (no trailing whitespace, one line
# unless the attribute is a table).
DEVICE_ATTRS: dict[str, str] = {
    "mem_info_vram_total": "536870912",          # 512 MiB BIOS carve-out, minimised
    "mem_info_vram_used": "16777216",
    "mem_info_vis_vram_total": "503316480",
    "mem_info_vis_vram_used": "16777216",
    "mem_info_gtt_total": "125829120000",        # amdgpu.gttsize=120000 (MiB)
    "mem_info_gtt_used": "36936790016",          # ~34 GiB: weights + KV, the number that matters
    "gpu_busy_percent": "43",
    "mem_busy_percent": "17",
    "power_dpm_force_performance_level": "auto",
    "pp_cur_state": "5",
    "unique_id": "0x0000003f",
    "pcie_bw": "15.75 GB/s x16 gen4",
    "pp_dpm_sclk": "0: 360MHz\n1: 600MHz\n2: 900MHz\n3: 1100MHz\n4: 1250MHz *\n",
    "pp_dpm_mclk": "0: 1600MHz *\n1: 7500MHz\n",
    "pp_dpm_fclk": "0: 512MHz\n1: 1024MHz *\n",
    "pp_dpm_socclk": "0: 200MHz\n1: 400MHz *\n",
}

# `vcn_busy_percent` and `mem_info_vram_vendor` are deliberately *not* created:
# on an APU the driver omits some of them, which is exactly the case a collector
# must survive by reporting "absent".

HWMON = {
    "name": "amdgpu",
    "temp1_input": "55111",          # edge, millidegrees
    "temp2_input": "61000",          # hotspot
    "power1_average": "78125000",   # microwatts = 78.125 W
    "power1_cap": "100000000",
}

MODULE_PARAMS = {
    "amdgpu/gttsize": "120000",
    "ttm/pages_limit": "30720000",
}

#: A `--path.sysfs=` tree that lacks `devices/system/cpu/` makes node_exporter's
#: cpu collector panic outright (1.12.1, cpu_linux.go `updateOnline`), so the
#: fixture carries enough CPU topology for the real binary to run against it.
CPUS = 4

UEVENT = """DRIVER=amdgpu
KERNEL=card0
MAJOR=226
MINOR=0
BUS_PCI=PCI:0000:c3:00.0
PCI_CLASS=30000
PCI_ID=1002:1586
PCI_SUBSYS_ID=1002:0158
PCI_SLOT_NAME=0000:c3:00.0
"""


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def build(root: Path, broken_symlink: bool = False, denied: bool = False) -> Path:
    """(Re)create the fixture under `root` and return the card directory."""
    if root.exists():
        shutil.rmtree(root)
    pci = "0000:c3:00.0"
    real = root / "devices" / "pci0000:00" / pci
    real.mkdir(parents=True)

    _write(real / "uevent", UEVENT)
    for name, value in DEVICE_ATTRS.items():
        _write(real / name, value)
    if denied:
        # An attribute that exists but cannot be read -> the EACCES branch, which is
        # what a non-root collector hits on `/sys/module/amdgpu/parameters/*` (0600).
        # Running as root reads it anyway; the test skips itself in that case.
        _write(real / "mem_info_gtt_used", "36936790016", mode=0o000)
    (real / "power").mkdir()
    _write(real / "power" / "runtime_status", "active\n")

    hwmon = real / "hwmon" / "hwmon3"
    for name, value in HWMON.items():
        _write(hwmon / name, value)

    card = root / "class" / "drm" / "card0"
    card.mkdir(parents=True)
    target = Path(os.path.relpath(real, card)) if not broken_symlink else Path("../../devices/gone/" + pci)
    (card / "device").symlink_to(target)
    # ... and the hwmon symlink the other half of the tree hangs off
    hwmon_link = root / "class" / "hwmon" / "hwmon3"
    hwmon_link.parent.mkdir(parents=True, exist_ok=True)
    hwmon_link.symlink_to(os.path.relpath(hwmon, hwmon_link.parent))

    for name, value in MODULE_PARAMS.items():
        _write(root / "module" / name, value)

    cpu_root = root / "devices" / "system" / "cpu"
    rng = f"0-{CPUS - 1}"
    for name in ("online", "possible", "present"):
        _write(cpu_root / name, rng + "\n")
    for n in range(CPUS):
        _write(cpu_root / f"cpu{n}" / "topology" / "core_id", f"{n}\n")
        _write(cpu_root / f"cpu{n}" / "cpufreq" / "scaling_cur_freq", "3600000\n")
        _write(cpu_root / f"cpu{n}" / "cpufreq" / "cpuinfo_max_freq", "5000000\n")
    return card


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(Path(__file__).parent / "fixtures" / "sysfs-gfx1151"))
    ap.add_argument("--all", action="store_true",
                    help="also emit the two pathological variants the parser must survive")
    args = ap.parse_args()
    here = Path(args.out).parent
    for out, kwargs in [
        (Path(args.out), {}),
        *([  # the narrow-mount case: /sys/class/drm was bind-mounted without /sys/devices
            (here / "sysfs-broken-device-symlink", {"broken_symlink": True}),
            # the permissions case: the attribute is there, the collector may not read it
            (here / "sysfs-denied", {"denied": True}),
        ] if args.all else []),
    ]:
        card = build(out, **kwargs)
        print(f"{out.name:32} sysfs root {card.parents[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
