# spikes/

Timeboxed investigations, kept in git so the *evidence* outlives the ticket that
paid for it. A spike's deliverable is a findings note plus whatever instrument was
needed to make its claims checkable — **nothing in here is deployed**, and nothing
here is load-bearing for the running box. The production code that a spike
recommends lives where it belongs (`monitoring/`, `monitor/`), created by the
follow-up ticket the findings note files.

Each directory holds: `FINDINGS.md` (the note: decision, comparison, measured
cost, provenance of every number), the instrument(s) used to get those numbers,
and a fixture so a reader without the hardware can re-run them.

| spike | question | outcome |
| --- | --- | --- |
| [`amd-gpu-metrics/`](amd-gpu-metrics/FINDINGS.md) | `workspace-szw.10` — how do AMD GPU + host telemetry (VRAM, GTT, gpu busy %, power, thermals) get into the Prometheus dataset on a gfx1151 / shared-memory / distrobox + Vulkan box, and at what memory cost? | node_exporter on the host with `--collector.drm`; ~24 MiB, < 1 % CPU, 13 GPU series, +4.4 MiB Prometheus head; **no ROCm, no `/dev/kfd`, no container** — the Vulkan path exposes the same amdgpu sysfs as the ROCm path; **GTT, not VRAM, is the pool the model grows**. Deployed by `workspace-szw.13` |
