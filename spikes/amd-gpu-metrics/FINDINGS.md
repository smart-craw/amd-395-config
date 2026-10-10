# Spike: AMD GPU + host telemetry for the monitoring stack

**Ticket:** `workspace-szw.10` · **Follow-up filed:** `workspace-szw.13`
**Outcome:** node_exporter on the host with `--collector.drm` — ~24 MiB, < 1 % CPU,
13 GPU series, no ROCm, no container, nothing deployed by this ticket.

---

## 0. Provenance of every number here

Read this before quoting anything below. This spike was written in the repo's
development container, which is **not the GPU box**: aarch64, kernel
`7.0.14-linuxkit`, python 3.14.5, **no GPU, no `/dev/dri`, no `/dev/kfd`, no
docker**. So:

* **measured** — process cost (RSS/PSS/CPU/series) of real binaries run here:
  `node_exporter 1.12.1` (linux-arm64 release), `prometheus 3.4.1` (the
  `monitoring/compose.yaml` pin), the spike's own python reader, and the metric
  output of each against a **gfx1151-shaped sysfs fixture**. Go binary memory is
  architecture-independent to first order, and the fixture reproduces the
  attribute *shapes* the box's kernel produces, so these transfer.
* **read from source** — which attributes exist, what they mean, who may read
  them, from **linux-6.18.55** (`amdgpu_vram_mgr.c`, `amdgpu_gtt_mgr.c`,
  `amdgpu_ttm.c`, `pm/amdgpu_pm.c`, `pm/swsmu/smu13/smu_v13_0_6_ppt.c` (the
  SMU-v13 Strix-class PPT that answers `gpu_busy_percent` on this class of part),
  `amdgpu_fdinfo.c`, `amdgpu_drv.c`, `ttm/ttm_tt.c`) and from
  `prometheus/procfs` / `node_exporter`'s `collector/drm_linux.go`.
* **not measured, flagged as such** — anything that needs the real card
  (sensor *names* on gfx1151, the APU's package-power attribute, whether
  llama.cpp's fdinfo is visible from a distrobox PID namespace). `workspace-szw.13`
  opens with the one command that collects those on the box.

Reproduce with [§6](#6-how-to-reproduce-every-number-in-this-note).

---

## 1. Answer: what the kernel already tells us, and what it does not

The headline: **the Vulkan path is not a blind spot.** Every signal the ticket
asked about is an attribute of the **amdgpu DRM driver**, which is loaded because
the GPU exists — not because ROCm is installed. `llama-server` talking Vulkan to
RADV and `rocm-smi` talking to KFD read the *same* sysfs. Getting these numbers
therefore needs no ROCm, no `/dev/kfd`, no root, and no device passthrough —
only `/sys`.

| signal | attribute (`/sys/class/drm/cardN/device/`) | source in linux-6.18.55 | non-root | verdict on *this* box |
| --- | --- | --- | :--: | --- |
| VRAM total / used | `mem_info_vram_total`, `mem_info_vram_used` | `amdgpu_vram_mgr.c`: `real_vram_size` / `ttm_resource_manager_usage()` | ✅ `S_IRUGO` | **near-useless here.** Total is the BIOS carve-out (`Dedicated Graphics Memory`, minimised per the root README); used is what little the carve-out holds. Do not alert on it. |
| visible VRAM | `mem_info_vis_vram_total`, `_used` | same file | ✅ | same story |
| **GTT total / used** | `mem_info_gtt_total`, `mem_info_gtt_used` | `amdgpu_gtt_mgr.c`; sized in `amdgpu_ttm.c`: `gtt_size = ttm_tt_pages_limit() << PAGE_SHIFT`, overridden by `amdgpu.gttsize` | ✅ | **this is the number.** On a shared-memory APU the model's buffers are TT/GTT allocations, so `gtt_used` grows with weights + KV and `gtt_total` is exactly the `amdgpu.gttsize=120000` from the README. `gtt_used / gtt_total` is the pressure the monitor is currently blind to. |
| GPU busy % | `gpu_busy_percent` | `pm/amdgpu_pm.c` → `AMDGPU_PP_SENSOR_GPU_LOAD`; on SMU 13.0.6-class parts `smu_v13_0_6_ppt.c` answers it with `METRICS_AVERAGE_GFXACTIVITY` | ✅ | expected to work — gfx1151 is GC IP 11.5.1 (`gfx_v11_0.c`, `soc21.c`), SMU v13 family; the probe confirms it in one command |
| VRAM busy % | `mem_busy_percent` | → `METRICS_AVERAGE_MEMACTIVITY` | ✅ | works; on an APU it is HBM/LPDDR-side activity, interpret with care |
| clocks | `pp_dpm_sclk`, `_mclk`, `_fclk`, `_socclk`, … | `pm/amdgpu_pm.c` `AMDGPU_DEVICE_ATTR` table, `ATTR_FLAG_BASIC`; flags only gate SR-IOV VF modes, bare metal sees all | ✅ | parse the line marked `*` = current state |
| temps | `hwmon/hwmonN/temp1_input`, `temp2_input` (edge, hotspot), mdeg | `pm/amdgpu_pm.c` hwmon groups | ✅ | names on gfx1151 to be confirmed on the box |
| power | `hwmon/hwmonN/power1_average`, `power1_cap` | `smu_v13_0_6_ppt.c`: `AMDGPU_PP_SENSOR_GPU_AVG_POWER` / `GPU_INPUT_POWER` → `METRICS_CURR_SOCKETPOWER` | ✅ | **verify on the box**: an APU reports *package* power, not "GPU power"; label the panel honestly |
| fans | `fan1_input`, `pwm1` | hwmon | ✅ | likely absent (silent/EC-controlled laptop-class cooling) |
| runtime PM state | `power/runtime_status` | PM core | ✅ | tells you *why* a busy reading was refused |
| **per-process GPU memory** | `/proc/<pid>/fdinfo/<fd>`: `drm-resident-memory-{vram,gtt,cpu}`, `drm-purgable-memory-*`, `amd-requested-{vram,gtt}`, `drm-engine-*` | `amdgpu_fdinfo.c` + `drm_file.c::drm_fdinfo_print_size` | own uid | **the underrated one.** Answers *which process grew the GTT* — the missing half of "KV growth became GPU memory pressure". llama.cpp runs as the same user as the collector, so this works rootless. |
| the ceilings themselves | `/sys/module/amdgpu/parameters/gttsize` (**0600 → root only**, `amdgpu_drv.c:303`), `/sys/module/ttm/parameters/pages_limit` (0644, `ttm_tt.c:53`) | — | ⚠️ partially | read `mem_info_gtt_total` instead: it *is* the effective value, and it is world-readable |
| rich SMU blob | `device/metrics/` (`gpu_metrics`; `gpu_metrics_v1_8` on SMU 13.0.6-class parts) | `pm/amdgpu_pm.c` | ✅ | binary struct; nothing in this spike parses it — `amdgpu_exporter`-family tools do |

### 1.1 The trap every exporter here has to get right: absent ≠ 0

`gpu_busy_percent` and friends go through `amdgpu_pm_get_access_if_active()`,
which **refuses** with `-EPERM` when the card is runtime-suspended rather than
waking it (other attributes use `amdgpu_pm_get_access()`, which *does* resume the
device — i.e. a naive poller can hold a sleeping dGPU awake just to measure it).
`node_exporter`'s `drm` collector reads each attribute with
`if v, err := readDRMCardField(...); err == nil { … }` and then publishes the
struct fields **unconditionally**, so a missing or refused attribute is exported
as `0`. `node_drm_gpu_busy_percent == 0` therefore means *"0 % or could not ask"*
— which on an idle box is exactly the shape of a lie you will not notice.

The spike's own reader takes the other branch: an unreadable attribute is
**omitted**, and `amdgpu_attr_status{attr=…,status="ok|missing|denied|busy|error"}`
says which. That is also the interface `workspace-szw.13` should reuse if it ever
needs a signal node_exporter does not expose. The fixture test
`test_absent_attribute_is_absent_not_zero` pins the behaviour.

### 1.2 `node_exporter`'s `amd_gpu` collector: it does not exist (the ticket's hunch, resolved)

There is no `amd_gpu` collector upstream in `prometheus/node_exporter`. What
exists is:

* **`drm`** — `collector/drm_linux.go`, **default-disabled**, must be turned on
  with `--collector.drm`. Emits `node_drm_gpu_busy_percent`,
  `node_drm_memory_{gtt,vram,vis_vram}_{size,used}_bytes`, `node_drm_card_info`.
  Verified output in [§4](#4-what-the-winners-actually-emit-measured).
* **`hwmon`** — **default-enabled**, so temps/power come for free with
  `node_exporter` regardless (`node_hwmon_temp_celsius{chip="amdgpu"}`,
  `node_hwmon_power_average_watt`).
* **`textfile`** — the escape hatch for whatever the two do not cover.

---

## 2. Candidates compared

Cost column is the measured number from [§3](#3-measured-cost); dependency
columns are what the option *requires*, from upstream docs/source.

| option | ROCm needed | `/dev/kfd` | `/dev/dri` | root | GTT | VRAM | busy % | clocks | temp/power | per-process | maintenance | verdict |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | --- | --- |
| **node_exporter, `--collector.drm` + `hwmon`** | no | no | no | no | ✅ | ✅ | ✅ | — | ✅ hwmon | — | upstream Prometheus project, very active | **winner.** One binary that also finally gives the *host* half (meminfo, PSI, cpufreq, disk) — and GTT is system RAM, so that half is not a bonus |
| **node_exporter `textfile` + spike's probe** (`amdgpu_sysfs_probe.py` on a timer) | no | no | no | no | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ fdinfo | ours (559 lines, stdlib) | **runner-up / add-on.** Zero resident cost (~80 ms CPU per run), the only option with per-process memory + clocks, honest absence. Costs a unit/timer and our maintenance |
| **ROCm `device-metrics-exporter` (DME)** (`rocm_exporthandler`, as the ticket named it — no project by that name exists) | **yes** | **yes** (`--device=/dev/kfd` in its own install docs) | docs mention both | **yes** | ✅ `gpu_{total,used}_gtt` | ✅ | ✅ | ✅ | ✅ | — | AMD, active | **rejected.** Container ships `libamd_smi`, docs are Instinct/HPC-shaped (HBM, XGMI, ECC, k8s operators, `rocpctl` profiler); on consumer silicon the sibling project's README documents metrics that read zero. On this box it means installing the ROCm stack to read numbers the kernel already exports |
| **`amd/amd_smi_exporter`** | **yes** | **yes** | no | typically | ✅ | ✅ | ✅ | — | ✅ | — | AMD, **quiet since 2025-05** | rejected: same ROCm tax, aimed at EPYC + Instinct |
| **`rudimk/rocm-smi-exporter`** | **yes** | yes | no | no | ✗ (VRAM % only) | ✅ % | ✅ | ✗ | ✅ | ✗ | **last commit 2024-09** | rejected: 4 metrics, ROCm required, unmaintained |
| **`amdgpu_top`** (Rust, active) | no | no | **yes — `libdrm` + `libdrm_amdgpu`** | no | ✅ | ✅ | ✅ | ✅ | ✅ | — | active | rejected: **it has no Prometheus endpoint at all** — the export surface is `--json` / `--json-fifo`, so it needs a JSON→exposition bridge (which is what the probe already is), and it is the one candidate that *does* link `libdrm`/`libdrm_amdgpu` (verified: the release binary will not start without them). Keep one tool in mind for a different job though: `--decode-gm` decodes the binary `gpu_metrics` blob, which §1 lists as unreadable-by-humans |
| third-party sysfs exporters — `home-operations/drm-exporter` (Rust, active), `reloaded/prometheus_consumer_amdgpu_exporter` (Go, RDNA-consumer), `kmulvey/radeon_exporter`, `Haseeb-Arshad-Dev/sysfs-gpu-exporter` | no | no | no | no | mostly | ✅ | ✅ | some | some | one | mixed, mostly hobby-scale | not chosen: each is another pin + another CVE surface for a subset of what `node_exporter` already does. `drm-exporter` (qmlib) needs kernel ≥ 6.13 for per-client memory and is k8s/DaemonSet-shaped; `prometheus_consumer_amdgpu_exporter` is the closest in spirit and is worth a look if the fdinfo/per-process story ever needs to be a daemon instead of a timer |

Also searched and found nothing named `rocm_exporthandler` / `exphet` on GitHub
(`amd/exphet` → 404), so the ticket's second candidate name is taken to mean the
ROCm Device Metrics Exporter. `amdgpu_top`'s *Prometheus exporter* mode — which
several write-ups remember it having — does not exist either; the flag list in
its own README is JSON-only.

Non-AMD vendor paths were out of scope and none was added to the table.

---

## 3. Measured cost

Dev container, `node_exporter 1.12.1` / `prometheus 3.4.1` (linux-arm64), GPU
emulated by `fixtures/sysfs-gfx1151`. PSS is the honest "what would killing it
free" number; RSS and PSS coincide for the static Go binaries.

| candidate | RSS | PSS | swap | CPU | series at `/metrics` | note |
| --- | --- | --- | --- | --- | --- | --- |
| node_exporter, default collectors | 21.4 MiB | 21.4 MiB | 0 | 0.8 % | 1 395 | 15 s window, 15 scrapes |
| node_exporter, default + `drm` + `hwmon` (real `/sys`, no card) | 23.2 MiB | 23.2 MiB | 0 | 0.9 % | 1 397 | enabling `drm` when there is no amdgpu card is free |
| node_exporter, default + `drm` + `hwmon` (**gfx1151 fixture**) | 23.6 MiB | 23.6 MiB | 0 | 0.9 % | 1 236 | +13 GPU series; the redirect of `--path.sysfs` *lowers* other collectors' series here, so do not read the total as a delta |
| node_exporter, `--collector.disable-defaults --collector.textfile` + probe's `amdgpu.prom` | 22.6 MiB | 22.6 MiB | 0 | 0.1 % | 97 | the Go runtime floor, nothing else |
| **probe `serve`** (stdlib python, HTTP endpoint) | **23.0 MiB** | **14.6 MiB** | 0 | 0.3 % | 42 | interpreter floor dominates; PSS is the fair number next to other pythons |
| **probe `textfile` one-shot × 25** | 23.0 MiB peak per run | **0 resident** | — | **79.5 ms CPU + 86.3 ms wall per run** | 42 | the cron/timer variant: nothing resident between runs |
| node_exporter GPU-only (`--collector.disable-defaults --collector.drm --collector.hwmon`) | 20.6 MiB | **12.0 MiB** | 0 | 0.2 % | 68 | the minimal GPU variant: of those 68 series **13 are GPU/hwmon**, the rest is `go_*`/`process_*` self-metrics. Cheapest option, but it throws away the host half |

### 3.1 …and what it costs Prometheus (the number ADR 0001 C5 is about)

`prom_head_cost.py`: pinned Prometheus 3.4.1, 5 s interval, mock model server as
the baseline job (`monitor/tests/mock_upstream.py`), 25 s warm-up + 25 s window:

| run | head series | Prometheus PSS | CPU |
| --- | --- | --- | --- |
| model server only | 617 | 84.0 MiB | 0.2 % |
| + GPU only (`disable-defaults`, drm+hwmon) | 712 (**+95**) | 82.1 MiB (−1.9) | 0.2 % |
| + whole host (default collectors + GPU) | 1 880 (**+1 263**) | 88.5 MiB (**+4.4**) | 0.2 % |

Single-run noise is a couple of MiB, so "GPU series are free" is the honest
reading: 95 extra series move head memory by less than the measurement's own
jitter. The whole-host row — the one actually proposed — costs **+4.4 MiB of
head**, and the TSDB arithmetic from `monitoring/README.md` agrees:
`1 263 series × 5 760 samples/day × ~1.5 B ≈ 11 MB/day` → **~0.16 GB over 15 d**
against the 2 GB `retention.size` cap.

### 3.2 Against the footprint budget (ADR 0001 C5)

C5 budgets prometheus 512 MiB + grafana 256 MiB with a **go/no-go at ≤ 768 MiB
measured total**. Adding the winner:

| | measured (dev container) | proposed ceiling |
| --- | --- | --- |
| node_exporter | 21–24 MiB RSS/PSS, < 1 % CPU (≈ 0.01 core) | **≤ 32 MiB, ≤ 0.05 CPU** |
| Prometheus growth | +4.4 MiB head, +~11 MB/day of TSDB | covered by the existing retention caps |
| total after the change | ≈ 768 + 24 MiB (before `.9`'s real numbers) | **amend C5 to ≤ 800 MiB measured**, exporter included |

The ADR's rule still applies and is not triggered: retention.size comes down
before any memory limit goes up. `.9` measures the real box; `.13` writes the
numbers in.

---

## 4. What the winners actually emit (measured)

`node_exporter --collector.drm --collector.hwmon --collector.disable-defaults --path.sysfs=<fixture>`
→ 13 GPU/hwmon series, all of them real output from the binary in this spike:

```
node_drm_card_info{card="card0",memory_vendor="",power_performance_level="auto",unique_id="0x0000003f",vendor="amd"} 1
node_drm_gpu_busy_percent{card="card0"} 43
node_drm_memory_gtt_size_bytes{card="card0"} 1.2582912e+11
node_drm_memory_gtt_used_bytes{card="card0"} 3.6936790016e+10
node_drm_memory_vis_vram_size_bytes{card="card0"} 5.0331648e+08
node_drm_memory_vis_vram_used_bytes{card="card0"} 1.6777216e+07
node_drm_memory_vram_size_bytes{card="card0"} 5.36870912e+08
node_drm_memory_vram_used_bytes{card="card0"} 1.6777216e+07
node_hwmon_chip_names{chip="amdgpu",chip_name="amdgpu"} 1
node_hwmon_power_average_watt{chip="amdgpu",sensor="power1"} 78.125
node_hwmon_power_cap_watt{chip="amdgpu",sensor="power1"} 100
node_hwmon_temp_celsius{chip="amdgpu",sensor="temp1"} 55.111000000000004
node_hwmon_temp_celsius{chip="amdgpu",sensor="temp2"} 61
```

(that `55.111000000000004` is node_exporter's own float artefact from dividing
millidegrees by 1000 — harmless, but set `decimals` on the panel or a legend
will show it.)

The spike's probe emits **42 series** for the same card (45 with fdinfo), of which
25 are `amdgpu_attr_status` — deliberately: that is the "what is readable on this
kernel" answer, in Prometheus rather than in a pastebin. Real output, same
fixture (the two `amdgpu_process_memory_*` lines come from the synthetic `/proc`
the fixture test builds — the parsing, not the kernel, is what is under test):

```
amdgpu_present{card="card0",pci="0000:c3:00.0",chip="1002:1586",driver="amdgpu"} 1
amdgpu_memory_total_bytes{card="card0",…,pool="gtt"} 1.25829e+11
amdgpu_memory_used_bytes{card="card0",…,pool="gtt"} 3.69368e+10
amdgpu_busy_percent{card="card0",…,kind="gpu"} 43
amdgpu_clock_ticks_per_second{card="card0",…,domain="sclk"} 1.25e+09
amdgpu_temperature_celsius{card="card0",…,sensor="temp1",hwmon="amdgpu"} 55.111
amdgpu_power_watts{card="card0",…,sensor="power1",hwmon="amdgpu"} 78.125
amdgpu_attr_status{card="card0",…,attr="vcn_busy_percent",status="missing"} 0
amdgpu_process_memory_used_bytes{comm="llama-server",pid="4242",pool="gtt"} 3.7581e+10
```

The two agree where they overlap (`55.111` °C, `78.125` W, the same GTT bytes),
which is the point: `node_exporter` is not leaving a gap the probe would have to
fill, it is leaving out *clocks* and *per-process* memory only.

---

## 5. The container question, answered

* **The exporter needs `/sys`, not `/dev/dri` and not `/dev/kfd`.** Device
  passthrough is for *using* the GPU (Vulkan needs `renderD128`, KFD/ROCm needs
  `/dev/kfd`); nothing in the sysfs read path touches a device node. The distrobox
  invocation in the root README (`--device /dev/dri --device /dev/kfd`) is
  therefore irrelevant to this problem — which is convenient, because the exporter
  should run on the **host** as a systemd user unit, outside every container.
* **If it is ever containerised, mount all of `/sys`, never `/sys/class/drm`.**
  `card0/device` is a symlink into `/sys/devices/pci…/`, and
  `prometheus/procfs` calls `filepath.EvalSymlinks` on it, so a narrow bind mount
  makes the whole card vanish. Verified here rather than assumed — with a fixture
  whose device symlink dangles:

  ```
  level=ERROR source=collector.go:168 msg="collector failed" name=drm \
      err="open …/class/drm/card0/device/uevent: no such file or directory"
  node_scrape_collector_success{collector="drm"} 0
  ```

  Prometheus-side hook for exactly that failure: `node_scrape_collector_success == 0`.
* Two related gotchas found the same way: `--path.sysfs` must point at a *complete*
  sysfs (pointing it at a tree without `devices/system/cpu/` makes
  `node_exporter`'s cpu collector **panic**, 1.12.1 `cpu_linux.go:360` — the
  fixture carries CPU dirs for that reason), and hwmon is behind a symlink too.
* Running a collector **inside the existing distrobox** would work (same `/sys`,
  the box's own `--device` flags are already there) but buys nothing and puts host
  telemetry behind a container that the model server can restart. Not recommended.
* One container-specific unknown for `.13`: whether `/proc/<pid>/fdinfo` of
  `llama-server` is visible from the host namespace (distrobox normally shares the
  host PID namespace, so *expected yes*, not verified here). Only affects the
  optional per-process panel.

---

## 6. How to reproduce every number in this note

```sh
cd spikes/amd-gpu-metrics
python3 make_fixture.py --all                 # gfx1151 + the two pathological variants
python3 -m unittest test_probe -v             # absent≠0, denied, clocks, fdinfo units, exposition validity
FIX=$PWD/fixtures/sysfs-gfx1151

python3 amdgpu_sysfs_probe.py --sysfs-root $FIX probe        # the capability matrix, as on the box
python3 amdgpu_sysfs_probe.py --sysfs-root $FIX metrics
python3 amdgpu_sysfs_probe.py --sysfs-root $FIX textfile /tmp/tf

NE=/path/to/node_exporter   # 1.12.1
python3 measure_cost.py --label "node_exporter + drm" --window 15 \
    --scrape http://127.0.0.1:9101/metrics -- \
    $NE --web.listen-address=127.0.0.1:9101 --path.sysfs=$FIX --collector.drm --collector.hwmon
python3 measure_cost.py --label "probe textfile one-shot" --repeat 25 -- \
    python3 amdgpu_sysfs_probe.py textfile /tmp/tf --sysfs-root $FIX

python3 prom_head_cost.py --prometheus /path/to/prometheus --node-exporter $NE   # §3.1
```

On the GPU box, the only command that matters is
`python3 spikes/amd-gpu-metrics/amdgpu_sysfs_probe.py probe` (rootless) — paste
its output into `workspace-szw.13`. It prints, per attribute: read OK with the
value, `missing`, `denied`, `busy` or `error` — that matrix is the acceptance
question this spike could not answer from a container with no GPU in it.

## 7. Open questions left for the box (all listed in `workspace-szw.13`)

1. The probe's capability matrix on the real card: which hwmon sensors exist and
   what they are called, whether `power1_average` exists on gfx1151 or only a
   package-power sensor, whether `vcn_busy_percent` / `pp_dpm_*` appear. Take
   `dmesg | grep -i amdgpu | head` alongside it — it names the GC/SMU IP versions
   this kernel bound, which is what §1's source citations should be checked against.
2. Steady-state `mem_info_gtt_used` at idle, at model-loaded, and at the largest
   context the box is used at → the alert thresholds for `.13` come from these,
   not from a guess.
3. Whether `node_drm_*` and `node_hwmon_*` labels survive the box's
   `udev`/systemd setup, and that `node_scrape_collector_success{collector="drm"} == 1`.
4. Confirm the CPU-vs-GPU headroom claim by re-running `measure_footprint.sh` with
   node_exporter up, and `llama-bench` once with the exporter running (the GTT
   ceiling is shared with the model, so a monitoring regression can in principle
   steal GTT pages — expected to be nothing, worth the one run).
5. If the per-process panel is wanted: verify fdinfo visibility and decide
   timer (`systemd timer` + textfile) vs daemon (`serve`).

## 8. Recommendation, in one paragraph

Run **node_exporter on the host as a systemd user unit**, pinned, loopback-bound,
with `--collector.drm` added to the default collector set; scrape it as a new
`host` job via file_sd; add a **GPU & host** row whose first panel is
`node_drm_memory_gtt_used_bytes / node_drm_memory_gtt_size_bytes` overlaid with
`llm:kv_cache_usage_ratio` (both `percentunit`) — that overlay is the ticket's
payoff: it shows KV growth turning into GPU memory pressure. Keep
`amdgpu_sysfs_probe.py` in the tree as the diagnostic (and as the optional
textfile writer for clocks + per-process GPU memory), and do not install ROCm
just to read the GPU. Cost: ~24 MiB, < 1 % CPU, +13 GPU series, +4.4 MiB
Prometheus head, ~11 MB/day of TSDB for the host job — all inside the budget with
room, which is what makes this an easy `workspace-szw.13`.
