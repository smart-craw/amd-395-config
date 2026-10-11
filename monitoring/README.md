# monitoring/ — Prometheus + Grafana for this box

The self-hosted, in-git system of record for `/metrics` history: throughput,
latency quantiles, KV-cache pressure, cache/spec-decode ratios, retention and
alert rules. `monitor/serve.py` stays the "right now" page. The decision, its
rejected alternatives and the contracts this directory implements live in
[ADR 0001](../docs/adr/0001-prometheus-grafana-owns-metrics.md); this file is the
**operator's runbook** for the stack.

Everything below is run from the **repo root**. Nothing in the stack contacts
anything outside the box at run time: no plugins, no update checks, no analytics,
no CDN assets, both UIs bound to loopback.

| path | what it is |
| --- | --- |
| `compose.yaml` | the stack: prometheus + grafana, pinned images, memory/CPU/pids ceilings |
| `.env.example` | every deployment knob, documented (ports, upstream, retention, limits, password) |
| `.env` | your machine-local values — **not committed** (it holds the admin password) |
| `check_monitoring.py` | offline pre-flight: cross-file agreements, policy invariants, promtool/compose validation |
| `monitoring.service` | optional systemd **user** unit so the stack survives reboot |
| `node-exporter.service` | systemd **user** unit for the host + GPU exporter (loopback, `--collector.drm`) |
| `install_node_exporter.sh` | installs the exporter at a **pinned version + sha256**, verified before it runs |
| `collect_host_baseline.sh` | one command on the box: probe output, the alert thresholds' baseline, the footprint row |
| `prometheus/prometheus.yml` | scrape config: direct job, passthrough job, host job, self-scrape |
| `prometheus/targets/llm-servers.json` | the direct scrape target list (hot-reloaded) |
| `prometheus/targets/host.json` | the host exporter's target (hot-reloaded; loopback-scoped) |
| `prometheus/rules.yml.tmpl` + `prometheus/alert_thresholds.env` | alert rules template + the thresholds to tune per box |
| `prometheus/rules.yml` | **generated** from those two — committed, drift-checked |
| `prometheus/recording_rules.yml` | the neutral `llm:*` aliases + the `host:*` baselines (hand-written) |
| `prometheus/unit-tests.yml` | `promtool test rules` unit tests for the alert rules |
| `render_rules.py` | renders `rules.yml` from the template + thresholds (`--check` for CI) |
| `grafana/provisioning/` | datasource + dashboard provider, mounted read-only |
| `grafana/dashboards/` | provisioned dashboard JSON (see "The dashboard") |
| `grafana/make_dashboard.py` | generator for the dashboard JSON (edit this, not the JSON) |
| `PARITY.md` | the page-vs-Grafana parity record: which number to trust, and why (workspace-szw.11) — generated, do not hand-edit |
| `parity/PARITY.template.md` | the source of that document: prose by hand, numbers by generator |
| `parity/fill_parity_doc.py` | injects a run's numbers into `PARITY.md` |
| `parity/evidence/` | the committed artifacts of the run `PARITY.md` quotes |
| `parity/parity_check.py` | the harness that measures it: paired page/panel figures, delta classes, seven probes |
| `parity/run_parity.sh` | one command: throwaway Prometheus + light load + N hours of paired samples + report |

## First time up

```sh
cp monitoring/.env.example monitoring/.env
$EDITOR monitoring/.env           # GRAFANA_ADMIN_PASSWORD is required; set LLM_UPSTREAM_URL
python3 monitoring/check_monitoring.py       # offline pre-flight, no stack needed
docker compose -f monitoring/compose.yaml up -d
```

`check_monitoring.py` is optional but cheap: it fails on the two mistakes that are expensive
to notice later — a `.env` upstream that disagrees with the Prometheus target
list, and an accidental non-loopback port bind.

Compose refuses to interpolate the file at all without
`GRAFANA_ADMIN_PASSWORD` — the stack will not come up rather than come up with a
known password. That is deliberate.

Verify (all of it is loopback):

```sh
docker compose -f monitoring/compose.yaml ps                # both Up
curl -fsS http://127.0.0.1:9090/-/ready
curl -fsS http://127.0.0.1:3000/api/health
# the targets that matter, with their health:
curl -s http://127.0.0.1:9090/api/v1/targets \
  | python3 -c 'import json,sys; [print(t["labels"]["job"], t["labels"].get("instance"), t["health"], t.get("lastError","")) for t in json.load(sys.stdin)["data"]["activeTargets"]]'
```

Expected: `llm-server` up (direct), `llm-server-via-proxy` up **only if serve.py
is running** (`python3 monitor/serve.py --url …`), `prometheus` up, and `host` up
**only if the host exporter is deployed** (`monitoring/node-exporter.service` — see
[Host + GPU telemetry](#host--gpu-telemetry-node_exporter); a box without it fires
`NodeExporterAbsent`, which is the point rather than a defect). Then open
<http://127.0.0.1:3000>, log in as `admin` + the password from `.env`, and find
the dashboard in the **LLM** folder. There is nothing to click into existence —
datasource, dashboards and rules are all provisioned from git.

## Day to day

```sh
docker compose -f monitoring/compose.yaml up -d          # start / apply a change
docker compose -f monitoring/compose.yaml down           # stop (history survives)
docker compose -f monitoring/compose.yaml restart prometheus
docker compose -f monitoring/compose.yaml logs -f --tail=100 prometheus
docker compose -f monitoring/compose.yaml exec prometheus promtool check config /etc/prometheus/prometheus.yml
```

Config and rules are mounted **read-only** from this directory, and
`--web.enable-lifecycle` is off (an unauthenticated POST endpoint that can install
a new config is not a thing on a box like this), so **a change needs a restart**:
`docker compose -f monitoring/compose.yaml restart prometheus`. Same for Grafana
after editing a dashboard JSON — the provider polls, but a restart is immediate
and deterministic.

`down` removes containers, not volumes: the TSDB and Grafana's database survive.
`down --volumes` destroys both — see "Purging the TSDB".

## Which knobs live where

Three families, and the split is forced by what Prometheus can and cannot do:

| lives in | what | why |
| --- | --- | --- |
| `monitoring/.env` | ports, resource ceilings, retention time/size, Grafana admin user/password, `LLM_UPSTREAM_URL` + `MONITOR_PROXY_URL` (declarations) | compose interpolates these before the container starts |
| `prometheus/prometheus.yml`, `targets/llm-servers.json` | scrape targets, intervals, relabelling | Prometheus expands `${VAR}` in a handful of credential/SD fields and `global.external_labels` **only** — never in `scrape_configs` targets or rule files |
| `prometheus/alert_thresholds.env` → `rules.yml` | alert thresholds | same env-expansion limitation, so the substitution happens at render time |

Because the middle row cannot take variables, `.env`'s upstream declaration and
the Prometheus files that actually scrape have to be kept equal. That is exactly
what `python3 monitoring/check_monitoring.py` verifies (it also fails if `rules.yml` has
drifted from its template + thresholds).

### Why the target list is a file, not a .env variable

`prometheus/targets/llm-servers.json` is a `file_sd` target list, refreshed
every 60 s. Adding a second model server means adding an entry and waiting a
minute — no restart, no re-render, and the file is the reviewed artifact.

The alternative — `${LLM_UPSTREAM_URL}` in `prometheus.yml` — is not available:
Prometheus does not interpolate environment variables in scrape targets. Making
it work would mean a render step between `git pull` and `docker compose up`, i.e.
the file you reviewed would no longer be the file that runs. A committed target
file plus a drift check is the same safety with one fewer moving part.

## The two `llm` scrape jobs — which one to keep

| job | target | what it contributes |
| --- | --- | --- |
| `llm-server` | `llm.home:8001` (from `targets/llm-servers.json`) | **the model server's metrics, directly** — the preferred path |
| `llm-server-via-proxy` | `host.docker.internal:8090` → serve.py `/metrics?url=…` | only `llm_monitor_*` (monitor-the-monitor), by the `keep` rule |
| `prometheus` | `localhost:9090` | Prometheus' own health, head series, TSDB size, RSS |

**Do not scrape the same server twice.** As shipped, the passthrough job keeps
only `llm_monitor_.*`, so the model server's series exist exactly once and "is
the monitor itself scraping?" is still a PromQL question. If you remove that
`keep` rule *and* leave the direct job in place, every `sum(rate(…))` in every
panel silently double-counts — no error, just a chart that reads twice the
throughput. The rules of thumb:

* direct reachable (default) → leave both jobs as they are;
* direct **not** reachable from the container network (firewall, no DNS for
  `llm.home`, model server bound to `127.0.0.1` on the host) → comment out the
  `llm-server` job **and** delete the `keep` rule from `llm-server-via-proxy`, so
  the passthrough becomes the only source of the model server's series.

The dashboard's `$job` variable is the escape hatch when you *do* point both jobs
at one server: pin it to `llm-server` and no panel mixes the two sources.

## Host + GPU telemetry (node_exporter)

The stack started out watching only the model server, which left this box's actual
failure mode unmonitored: a shared-memory AMD APU (gfx1151) whose model weights, KV
cache and activations are all allocated from the **GTT** pool, sized by the
`amdgpu.gttsize=120000` kernel parameter in the root README. On that hardware "the
box ran out of memory" and "the GPU could not allocate" are the *same event*, and
nothing in `/metrics` from llama.cpp can see it.

The decision was made and measured in `workspace-szw.10`
([spikes/amd-gpu-metrics/FINDINGS.md](../spikes/amd-gpu-metrics/FINDINGS.md));
this is the deployment, and none of it is open for re-litigating:

* **one process: `node_exporter` on the host**, run as a systemd **user** unit
  ([node-exporter.service](./node-exporter.service)) with
  `--web.listen-address=127.0.0.1:9100 --collector.drm`. It is a host process on
  purpose: putting it in a container (the model server's distrobox, or this
  stack's compose network) buys nothing and puts host telemetry behind a container
  something else can restart.
* **no ROCm, no `/dev/kfd`, no `/dev/dri`, no root.** Every GPU signal here is an
  attribute of the **amdgpu kernel driver** (`mem_info_gtt_total/used`,
  `mem_info_vram_*`, `gpu_busy_percent`, the hwmon temps and power), and the
  Vulkan path llama.cpp already uses reads the same sysfs the ROCm path would.
  Installing ROCm to read them would be paying for a stack to read numbers the
  kernel gives away.
* **`--collector.drm` is the GPU half**; `hwmon` is default-on upstream, so
  `node_hwmon_temp_celsius{chip="amdgpu"}` and
  `node_hwmon_power_average_watt{chip="amdgpu"}` come along with it. The rest of
  the default collectors are not a bonus either: on a shared-memory APU the GTT
  *is* system RAM, so meminfo / PSI / swap / load are the other half of the same
  question.
* **scraped as the `host` job** from
  [prometheus/targets/host.json](./prometheus/targets/host.json) — `file_sd`, the
  same pattern as `targets/llm-servers.json`, so pointing at another box is an
  edit and a minute, not a restart.
* **panels** in the dashboard's **GPU & host** row, **alerts** `Node*` in
  `rules.yml`, **cost** in [Footprint](#footprint).

### Installing it

```sh
monitoring/install_node_exporter.sh            # pinned version + sha256 per arch, verified
cp monitoring/node-exporter.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now node-exporter
sudo loginctl enable-linger $USER              # same requirement as the other user units
```

`install_node_exporter.sh --check` reports whether what is installed matches the
pin. The pin (version + a sha256 per architecture) lives in that script, is
verified before anything is executed, and is bumped deliberately the way the
compose images are — never `curl … | sh`, because an unreviewed upstream re-push
should not become a change on this box.

Verify layer by layer, so "the GPU panels are empty" has one answer instead of
five:

```sh
# 1. the exporter itself, from the host
systemctl --user status node-exporter --no-pager
curl -s http://127.0.0.1:9100/metrics | grep -E '^node_(drm|hwmon)' | head
# 2. what the kernel answers, and what it refuses — absent is NOT zero
python3 spikes/amd-gpu-metrics/amdgpu_sysfs_probe.py probe
# 3. what Prometheus actually managed to store
curl -sG --data-urlencode 'query=up{job="host"}'                       http://127.0.0.1:9090/api/v1/query
curl -sG --data-urlencode 'query=node_scrape_collector_success{collector="drm"}' http://127.0.0.1:9090/api/v1/query
```

`node_scrape_collector_success{collector="drm"}` has to be **1**. It is allowed to
be 0 and it is the single most misleading 0 in this stack: see the runbook entry
"`node_scrape_collector_success{collector="drm"}` is 0".

**Proving the plumbing without the GPU.** The spike's fixture reproduces the card's
attribute shapes, so the collector's selectors and the box script can be exercised end to
end on a machine with no GPU in it:

```sh
node_exporter --web.listen-address=127.0.0.1:9100 --collector.drm \
    --path.sysfs=spikes/amd-gpu-metrics/fixtures/sysfs-gfx1151 &
monitoring/collect_host_baseline.sh --fixture spikes/amd-gpu-metrics/fixtures/sysfs-gfx1151
```

Sections 1–2 of its output fill in — including the `node_hwmon_*` sensor names the
temperature selectors have to match, which is the one fact about this row that cannot
otherwise be checked before touching the box (`amdgpu` / `temp1`, `temp2`, `power1` on the
fixture). Everything needing Prometheus or real hardware prints `—`. That is also the
"no GPU attached → absent, not zero" test case, runnable without a GPU.

### Why the target is `host.docker.internal`, and the loopback rule

The exporter binds `127.0.0.1:9100` **on the host** and must not bind anywhere
else: `node_exporter` publishes this machine's hardware inventory, process list,
mount table and network counters, and neither it nor Prometheus has any
authentication — so loopback *is* the access control, exactly as it is for the two
UIs. That leaves one hop to think about, because Prometheus runs in a container:
the scrape has to cross from the container network to the host's loopback.

* **Rootless docker** (which is what a stack built entirely out of *user* units
  implies): the container's gateway reaches the host's loopback scope, so the
  shipped target `host.docker.internal:9100` works — the same path the
  `llm-server-via-proxy` job already uses for `serve.py` on `127.0.0.1:8090`.
* **Rootful docker**: `host.docker.internal` is a bridge address and a
  loopback-only listener is not reachable through it — the scrape fails with
  `connection refused`. Two ways to close that gap, in preference order:
  1. **give the `prometheus` service `network_mode: host`** and bind it with
     `--web.listen-address=127.0.0.1:9090`. Everything stays on loopback — this
     option changes no exposure at all, it just shares the namespace so "host
     loopback" and "container loopback" are the same place (Grafana then needs
     `network_mode: host` + `GF_SERVER_HTTP_ADDR: 127.0.0.1` too, and the
     datasource URL becomes `http://127.0.0.1:9090`). Not shipped as the default
     because host networking also voids the published-port mechanism that
     `compose.lan.yaml` and `workspace-szw.12` build on — it is a deliberate swap,
     not a free one.
  2. bind the exporter to one specific bridge address *in addition* to loopback
     (`--web.listen-address=127.0.0.1:9100 --web.listen-address=172.17.0.1:9100`,
     and put that address in `targets/host.json`). This is a real disclosure
     decision of the `workspace-szw.12` class, and `check_monitoring.py` fails
     `targets/host.json` if it names anything outside
     `127.0.0.1 / localhost / ::1 / host.docker.internal`, so it cannot happen by
     accident.

And the trap that cost a whole investigation in the spike: **if this exporter is
ever containerised, mount all of `/sys` read-only.** `card0/device` is a symlink
into `/sys/devices/pci…/` and `node_exporter` resolves it, so a
`-v /sys/class/drm:/sys/class/drm:ro` mount makes the card vanish and every scrape
fails — `node_scrape_collector_success{collector="drm"} 0`, verified in the spike.
`/dev/dri` and `/dev/kfd` are **not** needed to read any of this.

### What is deliberately not here

* **no ROCm stack, and none of the ROCm-shaped exporters** (`device-metrics-exporter`,
  `amd/amd_smi_exporter`, `rudimk/rocm-smi-exporter`): all need ROCm, `/dev/kfd`
  and root, and their metric sets are Instinct/HPC-shaped (HBM, XGMI, ECC).
  `amdgpu_top` links `libdrm`, is TUI-first, and has no Prometheus endpoint at all.
  Third-party consumer-amdgpu exporters are each another pin and another CVE
  surface for a subset of what `node_exporter` already does.
* **no bespoke exporter.** `spikes/amd-gpu-metrics/amdgpu_sysfs_probe.py` stays a
  probe and a diagnostic, not a daemon.
* **no per-process GPU memory yet** (the deferred v2 panel). The kernel publishes
  per-client `drm-resident-memory-{vram,gtt}` in `/proc/<pid>/fdinfo`, which answers
  *who grew the GTT*; `amdgpu_sysfs_probe.py textfile DIR` already renders it and a
  15 s `systemd` timer + the `textfile` collector would add it at zero resident cost
  (~80 ms CPU per run, ~23 MiB peak). It waits on one box-only question — whether a
  `llama-server`'s fdinfo is visible from the namespace the exporter runs in.
* **VRAM is never an alert.** It is the BIOS carve-out, deliberately minimised (root
  README); a flat, small VRAM line is the correct shape on this box.

## Retention sizing

Both caps are mandatory on this box (`compose.yaml`):

```
--storage.tsdb.retention.time=15d     # how far back
--storage.tsdb.retention.size=2GB     # the hard ceiling
```

The time cap answers "was the KV cache full overnight" and the 7-day questions
with margin. The **size** cap exists because this box trades RAM against model
weight memory (see `amdgpu.gttsize` / `ttm.pages_limit` in the root README) and
telemetry must never be the thing that wedges it.

Order-of-magnitude arithmetic, so the 2 GB ceiling is not a magic number:

* 15 s interval → `86400 / 15 = 5760` samples per series per day;
* TSDB compression lands around ~1.5 bytes per sample for this kind of data;
* a llama.cpp-style server with TTFT histograms plus Prometheus' own self-scrape
  is ~2 000–3 000 series in total (histogram **buckets** dominate that count, so
  the number scales with whatever the server exposes, not with traffic);
* **the `host` job adds ~1 263 series** (measured: `node_exporter` default
  collectors + `--collector.drm` against a gfx1151-shaped sysfs; ~1 000 of them are
  the host, ~13 are the GPU) — see [Footprint](#footprint);
* ⇒ `3 000 × 5760 × 1.5 B ≈ 26 MB/day` → **~0.4 GB over 15 days**, i.e. the size
  cap is insurance for a second server, a chattier `/metrics`, or a leaked
  high-cardinality label — not the normal working limit. With the host job in the
  mix the same arithmetic gives ~4 300 series → **~37 MB/day, ~0.55 GB over 15 d**,
  still inside the 2 GB cap with room.

Read the real numbers instead of trusting the estimate:

```sh
curl -s 'http://127.0.0.1:9090/api/v1/query?query=prometheus_tsdb_head_series' | python3 -c 'import json,sys;print("head series:",json.load(sys.stdin)["data"]["result"][0]["value"][1])'
curl -s 'http://127.0.0.1:9090/api/v1/query?query=prometheus_tsdb_storage_blocks_bytes' | python3 -c 'import json,sys;print("blocks bytes:",json.load(sys.stdin)["data"]["result"][0]["value"][1])'
du -sh "$(docker volume inspect --format '{{ .Mountpoint }}' llm-monitoring_prometheus-tsdb)"   # may need sudo
```

Lower `PROMETHEUS_RETENTION_SIZE` (or `_TIME`) in `.env` and restart if it grows;
nothing else changes.

## Footprint

This box deliberately starves itself of RAM for model weights (`amdgpu.gttsize`,
`ttm.pages_limit` in the root README), so the monitoring stack's footprint is
bounded, not best-effort. The limits below are
[ADR 0001 C5](../docs/adr/0001-prometheus-grafana-owns-metrics.md),
which is the budget `workspace-szw.9` measures and confirms; `compose.yaml` ships
exactly these defaults and `.env` can only move them.

| | limit | how it is enforced |
| --- | --- | --- |
| prometheus | 512 MiB, 0.50 CPU, ≤ 256 processes | `mem_limit` **and** `memswap_limit` (equal, so no swap escape into the model's page cache), `cpus`, `pids_limit` |
| grafana | 256 MiB, 0.25 CPU, ≤ 512 processes | same |
| both | read-only rootfs, `cap_drop: ALL`, `no-new-privileges`, 10 MiB of logs each | `read_only`, `tmpfs: /tmp`, `cap_drop`, `security_opt`, `logging.max-size/max-file` |

The ADR's rule when the numbers do not fit: **retention.size comes down before
the memory limit goes up.** Prometheus head memory scales with the number of
series, not with how much history is on disk, so shrinking retention costs
history but not RAM headroom — which is the resource this box cannot spare.

Measure it (this is what `workspace-szw.9` should publish):

```sh
docker stats --no-stream llm-prometheus llm-grafana
docker inspect --format '{{.Name}} mem={{.HostConfig.Memory}} nano-cpus={{.HostConfig.NanoCpus}} restart={{.HostConfig.RestartPolicy.Name}}' llm-prometheus llm-grafana
# Prometheus scrapes itself, so the box can report its own RSS:
curl -s 'http://127.0.0.1:9090/api/v1/query?query=process_resident_memory_bytes{job="prometheus"}' \
  | python3 -c 'import json,sys;[print(r["metric"]["instance"], int(float(r["value"][1])/2**20), "MiB") for r in json.load(sys.stdin)["data"]["result"]]'
# RSS double-counts shared pages, which is misleading when two containers share a
# daemon; PSS (proportional set size) is the honest "what did this cost" number:
for c in llm-prometheus llm-grafana; do
  pid=$(docker inspect -f '{{.State.Pid}}' "$c")
  printf '%s ' "$c"; grep '^Pss:' "/proc/$pid/smaps_rollup" 2>/dev/null || echo '(needs root)'
done
# and the thing that actually matters: the model server's memory headroom is unchanged
free -m
```

Expected shape, before anyone measures it for real: Prometheus in the tens of MB
of RSS at a few thousand series (the 512 MiB is compaction-burst headroom, not a
target), Grafana the heavier of the two at a couple of hundred MB — so 256 MiB is
the limit most likely to be tight. If Grafana is OOM-killed, cut the number of
series a panel pulls at once (larger `$interval`, fewer `legendFormat` series)
rather than raising its limit.

### What has actually been measured

Read this before quoting a number: the two container rows are **still not
measured**. This tree was built in a development container with no Docker and no GPU
in it, and a fabricated footprint number is worse than an empty cell. What *is*
measured is the monitor, and — new in `workspace-szw.13` — the exporter itself, run
for real with the flags the unit ships:

| process | RSS | PSS | swap | CPU | provenance |
| --- | --- | --- | --- | --- | --- |
| `monitor/serve.py`, idle | 26.6 MB | 17.1 MB | 0 MB | ~0 % | measured, dev container, python 3.14.5 x86_64 |
| `monitor/serve.py`, after ~500 proxied requests | 29.2 MB | 18.7 MB | 0 MB | ~1 % | measured, same |
| **`node_exporter` 1.12.1, `--web.listen-address=127.0.0.1:… --collector.drm`** (default collectors + drm, gfx1151-shaped sysfs) | **23.6 MiB** | **23.6 MiB** | 0 MiB | **0.8 %** | measured, dev container (aarch64), 25 s window, 1 236 series at `/metrics` — the same binary + flags as `node-exporter.service`, against the fixture that reproduces the box's attribute shapes. **Re-measure on the box**: `monitoring/measure_footprint.sh --proc node_exporter=node_exporter` |
| `node_exporter`, same flags, no GPU visible at all | 23.6 MiB | 23.6 MiB | 0 MiB | 0.8 % | measured — enabling `drm` when there is no amdgpu card costs nothing |
| **Prometheus, head delta from scraping the whole `host` job** | **+1 263 series** | **+2.4 MiB** | — | 0.3 % | measured, pinned prometheus 3.4.1, deployed 15 s scrape interval, mock model server as the baseline job (`spikes/amd-gpu-metrics/prom_head_cost.py`). The spike's 5 s-interval run measured +4.4 MiB for the same series set: single-run noise is a few MiB, so **quote ≤ 5 MiB** |
| TSDB growth from the `host` job | — | — | — | — | **~11 MB/day** → ~0.16 GB over 15 d (1 263 series × 5 760 samples/day × ~1.5 B), against the 2 GB `retention.size` cap |
| `prometheus` | — | — | — | — | **not measured** — run `measure_footprint.sh` on the box |
| `grafana` | — | — | — | — | **not measured** — run `measure_footprint.sh` on the box |
| TSDB growth per day | — | — | — | — | predicted ≈ 5 MB/day at ~400 series, see Retention sizing |

`monitoring/measure_footprint.sh` prints this table for real: it reads
`Pss` from `/proc/<pid>/smaps_rollup` for the monitor, `docker stats --format json`
for the containers, and the TSDB size from the volume — and prints `not running`
instead of a plausible-looking zero. Run it once with the stack down
(`--before`) and once with the stack up and the model loaded, then paste the output
into the table above and commit it. That is the deliverable `workspace-szw.9`.

**The budget with the exporter in it** (ADR 0001 C5, as amended by
`workspace-szw.13`): prometheus 512 MiB + grafana 256 MiB + **node_exporter
≤ 32 MiB** (measured 23.6, unit ceiling `MemoryMax=40M` to absorb a bigger `/sys`)
= **≤ 800 MiB**, and the go/no-go line below moves from 768 to 800 MiB accordingly.
`measure_footprint.sh` counts the exporter in its "added total" when it is given
`--proc node_exporter='node_exporter --web.listen-address'` — and pass
`FOOTPRINT_BUDGET_MIB=800`, because the script's own default is still 768 (its
`--budget-mib` default belongs to `workspace-szw.9`'s edit of that file; do not make
this ticket fight it for the line). The exporter belongs **inside** the total the budget
judges, not beside it.

The other half of the budget is the one that is not telemetry at all: the model's
shared-memory headroom must not move. Re-run the benchmark from the root README
with the stack up and down and compare —

```sh
llama-bench -pg 512,128 -m ~/models/qwen3.8-27B/Qwen3.8-27B-UD-Q4_K_M.gguf \
  -b 2048 -ub 512 -ngl 99 --flash-attn on --load-mode none
```

— remembering the caveat already written there: `llama-bench` does not exercise MTP,
so a speculative-decode regression will not show up in it.

With the exporter deployed there is a second, box-specific version of that check,
because the GTT ceiling is *shared with the model*: a monitoring regression can in
principle steal GTT pages. Run the benchmark once with `node-exporter` up and once
with it stopped and compare; the expected answer is nothing, and `collect_host_baseline.sh`
prints the reminder with the GTT numbers beside it.

### Still open on the box (what could not be measured from a development container)

Everything above is either config-in-git or measured on a machine with **no GPU in it**.
These are the items that need the strix, and they are the ticket's acceptance criteria, so
they are listed as commands rather than as a wish:

- [ ] **GTT ceiling is steady-state** (findings note §7 item 2): watch
      `node_drm_memory_gtt_used_bytes` across a full day with real traffic and set
      `GTT_PRESSURE_WARN` from what you see, not from the 0.85 placeholder — the placeholder
      exists so the alert *works*, not so it is *right*. `collect_host_baseline.sh --window 24h`
      prints min/mean/max and the `host:*:max_2d` baselines to paste in.
- [ ] **The `NodeGpuTempHigh` / `NodeMemoryPressure*` thresholds are placeholders with the
      same status**, from the same script (§4 of its output). Two days of the `host:*`
      recording rules is the honest floor for any of these; anything earlier is a guess
      with a decimal point.- [ ] **Payoff panel provenance**: the GTT/KV overlay has to be looked at over a real window
      with real traffic, because "is this correlation actually there?" is not answerable
      from a fixture. If it turns out flat, the honest move is to change the panel, not to
      explain the panel.
- [ ] **Sensor names**: `node_hwmon_chip_names{chip="amdgpu"}` will say something like
      `amdgpu as /devices/pci0000:c3:00.0/.../hwmon/hwmonN`; the temp alerts deliberately use
      the bare `chip="amdgpu"` prefix match so they survive the suffix, but confirm the
      sensor set (§2 of `collect_host_baseline.sh`) and that `edge`/`junction` are the ones
      you want to page on rather than `hotspot`.
- [ ] **Footprint on the box**: `FOOTPRINT_BUDGET_MIB=800 monitoring/measure_footprint.sh
      --proc node_exporter='node_exporter --web.listen-address'` into the table above, plus
      the `llama-bench` comparison with the exporter up and down — the go/no-go below is
      decided by those two numbers and neither is measured here.
- [ ] **Per-process GPU memory** (findings note §7 item 5) stays deferred with it: it needs the
      fdinfo walk plus the textfile collector and a timer, and the visibility question it
      depends on (`amdgpu_sysfs_probe.py metrics`) has to be answered from inside the model
      server's own cgroup first.

Nothing above is a reason not to deploy: the alerts that would fire *wrong* are all
thresholds marked `{{THRESHOLD}}` in the template, so setting them is a one-line edit and a
restart, and the config refuses to pretend they are measured until someone writes them in.

### <a name="go-no-go"></a>Go / no-go

Decided in advance, so nobody has to decide it at 02:00:

* **Go** — measured total ≤ 800 MiB *and* `llama-bench` unchanged: keep both
  services always-on, which is the default. (800, not the original 768: ADR 0001 C5
  was amended by `workspace-szw.13` to carry `node_exporter` ≤ 32 MiB inside the
  same budget rather than beside it. The exporter is 23.6 MiB measured, so this is
  not a squeeze — but the number is written down so that the next addition has to
  argue with a number instead of with a memory.)
* **No-go** — measured total > 800 MiB, or the model lost headroom: keep
  **Prometheus always-on** (it is what makes the 7-day questions answerable and it
  is cheap at this series count) and run **Grafana on demand**:

  ```sh
  docker compose -f monitoring/compose.yaml stop grafana     # give the RAM back
  docker compose -f monitoring/compose.yaml start grafana    # when you need a chart
  ```

  Second fallback, in order: drop `PROMETHEUS_RETENTION_SIZE` (2GB → 512MB), then
  the scrape interval (15s → 30s, which halves the sample rate for free). The
  memory ceiling goes up last, not first — see the ADR's rule above.

Record the outcome here rather than in a chat window: *date / measured total /
decision*: —

## Purging the TSDB

The history lives in a named volume, `llm-monitoring_prometheus-tsdb` (the
`llm-monitoring-` prefix is the compose project name in `compose.yaml`), not in
the checkout:

```sh
docker volume inspect --format '{{ .Mountpoint }}' llm-monitoring_prometheus-tsdb
# typically /var/lib/docker/volumes/llm-monitoring_prometheus-tsdb/_data
```

To wipe the history (containers only — volumes survive `down`):

```sh
docker compose -f monitoring/compose.yaml down
docker volume rm llm-monitoring_prometheus-tsdb
docker compose -f monitoring/compose.yaml up -d      # recreated empty
```

`docker compose -f monitoring/compose.yaml down --volumes` removes **both**
volumes, including Grafana's database — which also throws away the admin password
hash (reprovisioned from `.env` on the next `up`, so it recovers, but any
UI-side state is gone). Prefer the explicit `docker volume rm`. Prometheus does
not need the data directory to exist; it creates it.

## Upgrading the pinned images

Both images are pinned by **tag and digest**, e.g.
`prom/prometheus:v3.4.1@sha256:9abc6cf6…`. The tag is for humans, the digest is
what actually gets pulled, and a tag can be re-pushed upstream while a digest
cannot. Bump both, deliberately:

```sh
# what does the tag point at right now?
docker buildx imagetools inspect --raw prom/prometheus:v3.4.1 >/dev/null 2>&1 \
  && docker buildx imagetools inspect prom/prometheus:v3.4.1 | head
# or, without docker at all (prints docker-content-digest for the tag):
curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:prom/prometheus:pull" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])' > /tmp/tok
curl -s -o /dev/null -D - -H "Authorization: Bearer $(cat /tmp/tok)" \
  -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json" \
  https://registry-1.docker.io/v2/prom/prometheus/manifests/v3.4.1 | grep -i docker-content-digest
```

Then: edit the `image:` line in `compose.yaml` (tag **and** digest), read the
release notes for anything that touches this stack (`promtool check config` on
`prometheus/prometheus.yml`, Grafana dashboard `schemaVersion`), and

```sh
docker compose -f monitoring/compose.yaml pull
docker compose -f monitoring/compose.yaml up -d
python3 monitoring/check_monitoring.py
```

Rollback is `git revert` of the pin bump + the same two commands — the old digest
is still pullable. Never switch either image to `latest`: it turns an unreviewed
remote push into a change on this box on the next restart.

Alert rules and dashboards are not versioned with the images, so an upgrade is
also the moment to re-run `promtool test rules monitoring/prometheus/unit-tests.yml`.

## Alerts

promtool says the rules are valid; whether they *fire* on the right shape of
bad day is what `unit-tests.yml` pins.

```sh
python3 monitoring/render_rules.py           # after editing alert_thresholds.env
python3 monitoring/render_rules.py --check   # CI: fails if rules.yml is stale
promtool test rules monitoring/prometheus/unit-tests.yml
```

Rules live in `prometheus/rules.yml.tmpl` (the PromQL + wording) and
`prometheus/alert_thresholds.env` (the numbers). `rules.yml` is **generated**
and committed, because Prometheus does not expand variables in rule files and a
render step at `up` time would break "one command, config in git". `monitoring/.env`
overrides the committed thresholds for a per-box tune; `--check` then reports the
render as intentionally different, so keep box-specific tuning out of git and put
it in `.env`. Coverage today: server down, metrics absent, monitor scrape failing
or absent, KV pressure warn/critical, queueing, decode-throughput collapse,
prompt-cache going cold — and (workspace-szw.13) the host half: exporter absent,
a collector failing, GPU telemetry gone while the exporter is up, GTT pressure and
GTT-headroom-exhausted, GPU junction temperature warn/critical, memory PSI
warn/critical.

**Two of those thresholds are meant to be re-tuned from the box, not trusted.**
`GTT_PRESSURE_WARN` and the GPU temperature numbers are the classic "either never
fires or pages at 02:00" risk, so the baseline is *recorded* rather than guessed:
`host:gtt_usage_ratio:max_2d`, `host:psi_memory_waiting_ratio:max_2d` and
`host:gpu_temp_celsius:max_2d` (see
[recording_rules.yml](./prometheus/recording_rules.yml)) hold the box's own
2-day maximum from the first scrape on, and
`monitoring/collect_host_baseline.sh` prints them with the command to re-render
the rules. Until two days of real traffic exist, the baseline-free
`NodeGttExhausting` rule ("at this growth rate the GTT pool is gone inside 10
minutes") is what protects a fresh box.

**There is no Alertmanager**, deliberately: pointing it anywhere means choosing an
outbound notification channel (email, webhook, Telegram), and outbound egress is a
separate decision. The `alertmanager` block in `compose.yaml` is commented out
with the reasoning; the rules are useful without it (they render in the Prometheus
UI's Alerts page and in Grafana's alerting UI on the provisioned datasource).

## The dashboard

One dashboard, `LLM Server`, uid `llm-server`, at
[`grafana/dashboards/llm-server.json`](grafana/dashboards/llm-server.json). It is
**provisioned from git**, never hand-built in the UI: the provider
(`grafana/provisioning/dashboards/dashprovider.yml`) reads the directory Grafana
mounts at `/var/lib/grafana/dashboards`, which `compose.yaml` mounts from
`grafana/dashboards/` read-only. (The ticket wording said
`provisioning/dashboards/llm-server.json`; the JSON is the *provisioned* file and
the provider that provisions it is in `provisioning/dashboards/`. One mount, one
source of truth, same guarantee: nothing in the Grafana database is the only copy
of anything.)

Generated by [`grafana/make_dashboard.py`](grafana/make_dashboard.py) — see that
file's docstring for why. Rows, and the question each answers:

| row | the question |
| --- | --- |
| Overview | decode / prompt tok/s, requests running / queued / per second, structured share, per-model tok/s, KV % and the tile-sized now-values |
| Latency | TTFT, time-per-output-token and end-to-end at p50/p90/p99 — the panel set the bespoke page could not render at all |
| Cache & KV | "was the KV cache full overnight?", tokens vs pool, prompt-cache hit ratio vs reprocessed tokens |
| Speculative decode | draft acceptance, drafts per accepted token, the speculation time budget and whether speculation is still paying |
| Scrape health | `up` per job, the monitor's own `llm_monitor_*` series, scrape staleness, parse errors |
| GPU & host | is the GTT pool the model grows about to run out, is the GPU hot, is the box out of memory — and is the exporter that answers those questions even up? |

The **GPU & host** row (workspace-szw.13) is the one with a *payoff panel*:
*GPU memory pressure vs KV cache* puts `node_drm_memory_gtt_used_bytes /
node_drm_memory_gtt_size_bytes` and `llm:kv_cache_usage_ratio` on one chart (with the
GTT budget as a byte series on the right axis) so "KV growth turns into GPU memory
pressure" is something you can look at rather than infer. It is deliberately plotted
as two fractions of *different* ceilings — KV of the KV pool, GTT of the whole
`amdgpu.gttsize` budget — and its description says out loud what a non-correlation
would mean, because a panel that can only confirm the story is not an instrument.
Temperatures are read by hwmon **chip**, not by sensor name, and the multi-unit panels
use right/hidden axes rather than plotting watts against degrees.

Deliberately **not** on it: a time-series panel over every `llamacpp:*` / `halogen:*`
family. That is the raw-series list, it belongs on the proxy page next to the
exposition text, and a dashboard of 60 unlabeled series answers no question.

### Template variables

`$job`, `$instance`, `$model` (plus `$datasource`). All three are multi with
**All** selected and `allValue: .*`, so an absent label widens the selector instead
of emptying the panel. Notes that cost something to learn and are easy to undo by
accident:

* Grafana parses `label_values(a, b)` with a regex that cannot contain a comma, and
  hands `a` to Prometheus as `match[]`, so `a` must be a plain **series selector** —
  `sum(...)` or `label_replace(...)` in there does not error, it silently leaves the
  variable with no values (`check_monitoring.py` fails the build if one grows a
  comma). The model-label normalization therefore happens *in the panels*, not in the
  variable: `label_replace(..., "model", "$1", "model_name", "(.+)")`, with `(.+)`
  because `(.*)` matches an **absent** `model_name` on the empty string and blanks a
  `model` label that file_sd set correctly.
* `$model` lists `model` values from the token counters and the `llm:*` aliases, so a
  vLLM server shows up once its `model_name` has been rule-normalized. A llama.cpp
  server that nobody labelled shows nothing, and the per-model panel draws one line
  called `unknown` — which is the answer, not a failure. Put the name in
  `prometheus/targets/llm-servers.json` labels to get a name.
* The `$model` filter is applied to both spellings in the per-model panel
  (`{model=~"$model"} or {model_name=~"$model"}`) because PromQL cannot match "either
  of two labels" in one selector, and `label_match()` — which could — is an optional
  Prometheus 3.x extension this box does not assume.
* One panel ("Decode tok/s — one panel per $instance") uses **repeat-by-variable**,
  so N servers get N charts instead of N lines fighting over one axis.

### Refresh, range, step

`refresh: 15s` (the scrape interval — refreshing faster cannot show anything new),
default range 6h, minimum step 15s on the dashboard and on every target. Grafana's
own floor is `GF_DASHBOARDS_MIN_REFRESH_INTERVAL: 15s` in `compose.yaml`.

### What "no data" means, and what it does not

Quantiles need `_bucket` series. Most `llama-server` builds publish counters and
average gauges only, so the latency panels say **"no histogram on this server"**
(`fieldConfig.defaults.noValue`) rather than the useless "No data", and each also
plots the mean from `_sum` / `_count`, which exists even when buckets do not. The
"Latency families this server publishes" panel counts the bucket / count series per
instance, which turns "is this empty because of me or because of the server?" into a
lookup. Scrape health is the other half: `up == 0` is Prometheus could not reach the
target, `llm_monitor_scrape_ok == 0` is the monitor could not reach the model.

### Deep links

`monitor/serve.py --grafana-url` links
`/d/llm-server/llm-server?var-instance=<host:port>&viewPanel=<id>`. Panel ids 1-7
are reserved by [ADR 0001 C4](../docs/adr/0001-prometheus-grafana-owns-metrics.md)
and `check_monitoring.py` checks both ends: the ids exist, keep their titles, and
still match the list in `monitor/static/app.js`. Row ids come from a separate range
(100+) so a row can never shadow a panel id — with a duplicate id, `viewPanel=`
lands on whichever the API returns first.

### Checking the queries against a running Prometheus

`check_monitoring.py` is offline and needs nothing. Once the stack is actually up,
the panel queries can be run for real — every expression, `$var`s widened to `.*`,
against the same Prometheus the dashboard talks to:

```sh
# one expression, by hand
promtool query instant http://localhost:9090 'sum(rate(llamacpp:tokens_predicted_total[5m]))'
# or through the HTTP API, which is also how Grafana asks
curl -sG --data-urlencode 'query=count({__name__=~"llamacpp:kv_cache_usage_ratio"})' \
  localhost:9090/api/v1/query | python3 -m json.tool
```

Worth doing after touching a selector: a query can be valid PromQL and still return
nothing (a label that does not exist, a family name the server does not use), and
that is invisible until someone opens the panel.

## Which view is primary, and how that was checked

**Grafana is the primary view for anything derived from `/metrics`.** Throughput as a
rate, latency quantiles, prompt-cache hit ratio, draft acceptance, request rates, and
every "over the last N minutes" question: those are PromQL, they run with a real
window, counter-reset handling and `promtool` tests, and they are the numbers an alert
and a post-mortem should use. `monitor/serve.py` keeps the live view — the JSON
endpoints `/metrics` cannot represent (`/health`, `/v1/models`, `/cache`), the raw
exposition text, and the raw gauges, each labelled as which kind of number it is.

That split used to be a promise rather than a measurement, and the bespoke page still
computed three figures itself (draft acceptance, prompt-cache hit ratio, lifetime
tokens/s) plus a fuzzy regex per metric name to guess at other vendors' spelling.
`workspace-szw.11` measured the two views side by side against the live server,
classified every difference, and deleted the client-side arithmetic once each
difference had a name. The record — figures, delta classes, which side to trust, and
the four probes for the cases a quiet afternoon cannot show — is
[`PARITY.md`](PARITY.md); the harness that produces it is `parity/`:

```sh
monitoring/parity/run_parity.sh --duration 1h              # the ticket's week: 7d
python3 monitoring/parity/parity_check.py report monitoring/parity/runs/<run>/samples.jsonl
python3 monitoring/parity/parity_check.py selftest         # offline, no server needed
```

It runs a throwaway Prometheus (the pinned version, this stack's scrape interval, the
production recording rules) and scrapes the model server beside three doubles —
`monitor/tests/mock_upstream.py --mode fanout` (one metric name, two `model` label
sets), `--mode nan` (NaN / +Inf) and `--mode reset` (a counter that restarts on a
schedule) — so the label-fan-out, NaN and counter-reset behaviours are measured rather
than described. Its exit code is non-zero when a paired difference falls into none of
the named classes: a run that cannot fail cannot mean anything. Classification happens
per row at record time, and `report --reclassify` re-runs that judgement over the raw
samples when the classifier itself improves, which is how a long run survives a
classifier fix instead of being thrown away with it.

The committed run under `parity/evidence/` is what `PARITY.md` quotes; it is regenerated
into `parity/runs/<stamp>/` (gitignored) and injected into the document by
`parity/fill_parity_doc.py`, which exits non-zero on a placeholder it cannot fill.

Two things follow from that record and belong in the runbook, not just the write-up:

* **the server's `llamacpp:*_tokens_seconds` gauges are not comparable figures.** They
  average over the interval since *whoever last read `/metrics`*, so scraping the box
  from two places changes what each one sees, and an idle server reports 0 rather than
  "no data". Grafana's `rate()` is the number to quote.
* **a server whose `/metrics` answers 404 is a supported configuration, not a degraded
  one.** Prometheus then sees nothing at all, so the proxy page is the only view that
  server has — by design, and test-covered (`monitor/tests/mock_upstream.py --mode
  nometrics`). Grafana being primary is a statement about `/metrics` telemetry, not a
  claim that every server has any.

## Recording rules vs raw PromQL in panels

`recording_rules.yml` publishes the neutral `llm:*` aliases ADR 0001 C3 names as
the vendor-drift seam: `llm:decode_tokens_per_second`,
`llm:prompt_tokens_per_second`, `llm:requests_per_second`, `llm:requests_running`,
`llm:requests_queued`, `llm:kv_cache_usage_ratio`, `llm:prompt_cache_hit_ratio`,
`llm:draft_acceptance_ratio` and the latency quantiles
`llm:time_to_first_token_seconds:{p50,p90,p99}` (plus the
`time_per_output_token` / `end_to_end_response` siblings the latency row needs).

The dashboard plots the **raw expression and the alias side by side** (the alias
legend says `(rule)`), rather than the alias alone: a panel must render against a
server whose rules have never run, and "No data" has to mean no data, not no rule.
Where the two disagree the rule is stale — or the windows differ, since the rules use
a fixed 5m and the panels use `$__rate_interval`. Aliases keep `job`, `instance` and
the normalized `model` so a panel's filters still mean something; the ADR's contract
is the alias names, not their label set.

The same file carries a second namespace, **`host:*`** (workspace-szw.13):
`host:gtt_usage_ratio` with its `:max_2d` / `:mean_2d` baselines, plus the memory-PSI
and hottest-GPU-sensor maxima. Those are not the vendor-drift seam C3 is about —
nothing host-side has a vendor spelling — they exist so the thresholds this box has to
tune itself against are **recorded in Prometheus** instead of in someone's memory: two
days after the exporter goes up,
`promtool query instant http://127.0.0.1:9090 'host:gtt_usage_ratio:max_2d'` is the
number `GTT_PRESSURE_WARN` gets set from (and `monitoring/collect_host_baseline.sh`
prints the whole set).

Adding a serving stack means adding its metric names to the selector constants in
`make_dashboard.py` and to `recording_rules.yml` — not a new regex in `app.js`, and
not twenty panel edits. `check_monitoring.py` fails if a panel plots an `llm:*` or
`host:*` name that no rule records.

## Changing a panel

Never in the UI: `allowUiUpdates: false`, `disableDeletion: true` and
`updateFolderItems: false` in the provisioned provider make the committed JSON the
only version that matters, and a pane you "fixed" in the browser is silently
reverted on the next poll.

```sh
# edit monitoring/grafana/make_dashboard.py, then:
python3 monitoring/grafana/make_dashboard.py      # regenerate the JSON
python3 monitoring/check_monitoring.py            # structural checks + drift + PromQL lint
docker compose -f monitoring/compose.yaml restart grafana
```

Edit the **generator**, not the JSON: `make_dashboard.py --check` (which
`check_monitoring.py` runs) fails the build when the committed JSON and the
generator disagree, so a hand-edit to the JSON is a failure rather than a
workflow. Each panel's `description` is generated from its own target expressions,
which is why the note next to a chart can be trusted to be the query above it.

Panel **ids are a contract**: [ADR 0001 C4](../docs/adr/0001-prometheus-grafana-owns-metrics.md)
reserves `viewPanel=1..7` for specific panels because `monitor/serve.py`
deep-links into them, and the uid must stay `llm-server`. New panels take free ids
from 20 and rows from 100 (`check_monitoring.py` enforces uniqueness and the
reserved titles), and the uid/variable names are pinned there too.

## Keeping it up across reboots

`monitoring.service` is a systemd **user** unit shaped like
`monitor/llm-monitor.service`, so it installs the same way as the rest of this repo:

```sh
mkdir -p ~/.config/systemd/user
cp monitoring/monitoring.service ~/.config/systemd/user/   # or symlink it
# if your checkout is not at ~/service, edit the %h/service paths in the unit
systemctl --user daemon-reload
systemctl --user enable --now monitoring.service
journalctl --user -u monitoring -f
```

**Linger is required**, or the stack dies with your login session:

```sh
loginctl show-user $USER | grep Linger   # Linger=no means it will not survive
sudo loginctl enable-linger $USER        # same requirement as llm-server.service
```

The unit is `Type=oneshot` + `RemainAfterExit=yes`: `start` = `up -d`, `stop` =
`down` (volumes, i.e. history, survive), `reload` = re-apply `compose.yaml`/`.env`
after a `git pull`. Resilience comes from `restart: unless-stopped` on the
containers, not from the unit — the docker daemon is a **system** service that a
user unit may not `Require` (the unit's `Wants=docker.service` is a wish, not a
guarantee, so `Restart=on-failure` covers a daemon that is not up yet). You need
either rootless docker or membership of the `docker` group; with `podman`,
`podman compose` works provided `host-gateway` is supported (podman ≥ 4.1) for
the passthrough job.

## Validating without running anything

```sh
python3 monitoring/check_monitoring.py                       # everything below, plus the cross-file checks
python3 monitoring/render_rules.py --check                   # committed rules.yml == the render of the template
python3 monitoring/grafana/make_dashboard.py && git diff --exit-code monitoring/grafana/dashboards
promtool check config monitoring/prometheus/prometheus.yml
promtool check rules monitoring/prometheus/{rules,recording_rules}.yml
promtool test rules monitoring/prometheus/unit-tests.yml
docker compose -f monitoring/compose.yaml config -q
```

`check_monitoring.py --promtool` additionally borrows promtool's PromQL parser to
**type-check every panel expression in the dashboard**: it writes each `expr` to a
throwaway recording-rule file (`$__rate_interval` substituted for a real duration)
and runs `promtool check rules` on it. That is what catches a `sum by (le) rate(x)`
missing its parens, an `avg_over_time(max(...))` that would need a subquery step, or
an unbalanced paren — none of which a JSON schema can see, and all of which otherwise
surface as a panel that silently says "No data" a week later. CI runs it
(`.github/workflows/test.yml`, `monitoring` job).

`promtool` is a single static binary from the Prometheus release matching the
pinned image (and it is inside the pinned image too: `docker compose -f
monitoring/compose.yaml exec prometheus promtool …`).
`check_monitoring.py --promtool` runs the promtool checks when it is installed,
so the checker never becomes the reason a box can't verify itself.

## Deliberately not here

* **LAN exposure by default.** Both ports are published on `127.0.0.1` and nothing
  else, because Prometheus has no authentication at all — loopback *is* its access
  control. When publishing is wanted, [compose.lan.yaml](./compose.lan.yaml) is the
  opt-in override (anonymous read-only Viewer, Prometheus still loopback-bound) and
  it states the disclosure trade-off in its own header; the shared-non-admin-account
  alternative is documented there too. `workspace-szw.12` keeps the hardened-proxy
  variant (nginx + basic auth + `GF_SERVER_ROOT_URL`) open.
* **Alertmanager / any egress** — see the Alerts section; the routing file is
  [alertmanager.yml.example](./alertmanager.yml.example), deliberately not mounted.
* **A committed `.env`.** It holds the admin password; only `.env.example` is in
  git, and `.gitignore` keeps the real one out.
* **Monitoring of the box itself** was, until `workspace-szw.13`, in this list. It
  is now a **separate process on the host**, not part of this compose stack:
  `node_exporter --collector.drm` in its own systemd user unit, scraped as the
  `host` job — see [Host + GPU telemetry](#host--gpu-telemetry-node_exporter). What
  is still deliberately not here: any GPU exporter that needs ROCm, a containerised
  exporter with a partial `/sys` mount, and anything published beyond loopback.

## <a name="runbook"></a>Runbook

Questions this box actually gets asked, in the order you would ask them. The
one-line answer first, then where the evidence is.

**“Is it up right now?”** → the monitor page (`http://127.0.0.1:8090`). It needs
neither Prometheus nor Grafana, and it is the only layer that answers without
history. Its own status: `systemctl --user status llm-monitor`.

**“What was p99 TTFT yesterday?”** → Grafana ▸ LLM Server ▸ Latency, range on the
incident window. If the range is shorter than `PROMETHEUS_RETENTION_TIME`, the
answer does not exist — that is a retention decision, not a bug.

**“Was the KV cache saturated overnight?”** → Cache & KV row, range 12–24 h: the
usage series with its 1 h average and the 80 %/97 % bands drawn on the chart. The
Past Alerts tab in Prometheus (`:9090/alerts`) is the same answer in words, from
`LLMKVCachePressureWarn` / `…Critical`.

**“Why is decode slow when KV is at 30 %?”** → KV is not the only ceiling.
Overview ▸ *Requests running / queued*: `requests_deferred > 0` with KV low means
slot starvation or a CPU/PCIe bottleneck, not token starvation. Compare decode
tok/s against TTFT and TPOT on the same window; `LLMDecodeThroughputCollapse`
fires on a drop against the target's own baseline, which catches throttling that
looks normal in absolute terms.

**“Grafana says No data everywhere.”** In order, cheapest first:

1. `docker compose -f monitoring/compose.yaml ps` — both containers up?
2. `http://127.0.0.1:9090/targets` — is the target **up**? If not: `curl -sS
   http://127.0.0.1:8090/metrics | head` proves the passthrough, and `curl -sS
   $LLM_UPSTREAM_URL/metrics | head` proves the server.
3. `http://127.0.0.1:9090/rules` — did the rule files load? (A bad rule file is
   fatal at start-up, so a running Prometheus with no rules means it never got
   them mounted.)
4. Edited config but no restart? Everything is mounted read-only and
   `--web.enable-lifecycle` is off by design: `docker compose -f
   monitoring/compose.yaml restart prometheus`.
5. Fresh checkout: panels need one scrape. `LLMMonitorAbsent` (nothing is scraping
   the monitor) and `LLMMetricsAbsent` (this server has no `/metrics`) tell you
   which of the two you are looking at.

**“The model server has no `/metrics` at all.”** Supported, not broken. The monitor
page still answers, because `/health`, `/v1/models` and `/cache` are not Prometheus
metrics; only the Grafana panels are empty, and `LLMMetricsAbsent` says so in those
words. Reproduce with `mock_upstream.py --mode nometrics`. If this box is meant to
be metrics-less permanently, silence that alert for it rather than letting it cry.

**“The monitor page is down.”** Prometheus keeps scraping the upstream directly, so
history continues; what you lose is the live page, the passthrough job's
`llm_monitor_*` series, and `LLMMonitorAbsent` fires. `journalctl --user -u
llm-monitor -n 50` is the log (stderr, because the unit has no `StandardOutput=`).
A monitor that is up but failing to scrape shows as `LLMScrapeFailing` instead —
which is the alert that distinguishes *blind* from *idle*.

**“Add a second model server.”** One edit:
[prometheus/targets/llm-servers.json](./prometheus/targets/llm-servers.json) —
file_sd re-reads it every 60 s, no restart. Give it a `model` label if the server
does not label its own metrics, or the per-model panels group it under `unknown`.
Then check `:9090/targets` shows both, and that the `$job` / `$instance` variables
in Grafana list it.

**“Prometheus hit the retention size cap.”** Nothing to fix: it deletes whole blocks
oldest-first (2 h units, so it moves in steps) and keeps running; watch
`prometheus_tsdb_storage_blocks_bytes` and `prometheus_tsdb_oldest_timestamp_seconds`.
If the oldest timestamp is *not* advancing while over the cap, the volume is out of
host disk (`docker system df`) — the failure the cap exists to prevent.

**“The box is short on RAM.”** `docker compose -f monitoring/compose.yaml stop
grafana` first: it is the bigger and less essential half, and Prometheus keeps
recording history without it. Re-measure with `measure_footprint.sh` (see go/no-go).

**“The box ran out of GTT.”** The one-line answer: *something grew the GPU pool past
its budget — cut slots or context before you touch anything that needs a reboot.*
This box's GPU memory is system RAM handed to the driver as the GTT pool, so the
symptoms are an allocation failure, not a slowdown: the model server logs a failed
allocation / cannot load the model / the largest context suddenly refuses, while
`free -m` still claims there is memory, and `NodeGttExhausting` (critical) or
`NodeGttPressureHigh` (warning) is firing.

What to look at, in this order:

1. **GPU & host row ▸ Memory pools, bytes** — GTT used against the dashed GTT budget,
   plus the *GTT headroom* tile (bytes left, which is what an allocation actually
   needs). Same numbers without Grafana:
   ```sh
   curl -sG --data-urlencode 'query=node_drm_memory_gtt_used_bytes / node_drm_memory_gtt_size_bytes' http://127.0.0.1:9090/api/v1/query
   curl -sG --data-urlencode 'query=node_drm_memory_gtt_size_bytes - node_drm_memory_gtt_used_bytes' http://127.0.0.1:9090/api/v1/query
   ```
2. **Is today abnormal?** `host:gtt_usage_ratio:max_2d` (or
   `max_over_time(host:gtt_usage_ratio[6h])`) is the box's own baseline. Flat all
   week with a step at 14:20 means someone loaded a model, raised a context, or
   started a second server — the step's timestamp is the whole investigation.
3. **The host half**, same row: `MemAvailable`, memory PSI and swap. GTT is carved
   from system RAM, so *both* climbing is a memory problem (see “The box is short on
   RAM”); GTT climbing while MemAvailable stays comfortable is a *sizing* problem in
   the model config. Different fixes, and this is the panel that tells them apart.
4. `journalctl -k | grep -i amdgpu` for the driver's own refusals, and
   `python3 spikes/amd-gpu-metrics/amdgpu_sysfs_probe.py probe` to confirm the numbers
   you are reading are what the kernel is reporting (absent ≠ 0).

What to cut first — cheapest and most reversible at the top:

1. **Slots, then context.** `-c/--ctx-size` and the slot count (`--parallel`) size the
   KV pool, which is the part that *grows*; dropping either frees GTT immediately,
   no reboot. Unloading the second model, if there is one, is the bigger version of
   the same move.
2. **The client that is doing it.** Per-process GPU memory is the deferred panel; until
   it exists, `amdgpu_sysfs_probe.py`'s fdinfo read-out names the process that owns
   the GTT, rootless.
3. **RAM, not GTT?** Then stop Grafana (go/no-go above) — that is a different pool and
   cutting KV slots to fix it just loses context for nothing.
4. **Only last the kernel parameters.** `amdgpu.gttsize` / `ttm.pages_limit` (root
   README) need a reboot, and raising them takes RAM away from everything else on a
   box that was deliberately tuned the other way. Check the BIOS first: a BIOS reset
   silently restores *Dedicated Graphics Memory*, which is a carve-out **out of**
   what the GTT pool can ever be — the fix there is the BIOS, not `gttsize`.
5. After the incident, re-tune the threshold: the alert existed to be re-tuned
   (`GTT_PRESSURE_WARN` ← `host:gtt_usage_ratio:max_2d`).

Never alert on **VRAM** on this box: it is the BIOS carve-out, small and flat by
design, and a VRAM-based alert fires on a healthy machine.

**“The GPU & host row is empty.”** Cheapest first, one layer per command:

1. `systemctl --user status node-exporter` — is the exporter running at all? If not,
   `NodeExporterAbsent` should be firing, and `journalctl --user -u node-exporter -n 50`.
2. `curl -s http://127.0.0.1:9100/metrics | grep '^node_drm'` — does the *host* see it,
   and does it contain GPU lines?
3. `up{job="host"}` in Prometheus — does the container reach it? If steps 1-2 are fine
   and this is 0, the problem is the namespace hop: see
   [why the target is `host.docker.internal`](#why-the-target-is-hostdockerinternal-and-the-loopback-rule).
   Do **not** fix it by binding the exporter on `0.0.0.0`; that file is checked.
4. Series present but the GPU ones missing while `up{job="host"}` is 1 →
   `NodeGpuTelemetryAbsent`: the drm collector found no card (kernel/BIOS change), and
   `node_scrape_collector_success{collector="drm"}` is the next number to look at.

**`node_scrape_collector_success{collector="drm"}` is 0.** The GPU half of the scrape
failed, and it is worth taking seriously precisely because everything else still
looks healthy. In order of how often it happens:

1. **an exporter containerised with only `/sys/class/drm` mounted.** `card0/device` is
   a symlink into `/sys/devices/pci…/`; if the mount makes it dangle, the whole card
   vanishes and node_exporter logs `collector failed name=drm … no such file or
   directory`. Fix: run it on the host as `node-exporter.service` does, or mount **all
   of `/sys` read-only** — there is no useful middle ground. (`/dev/dri` and `/dev/kfd`
   are *not* needed for any of this.)
2. **started without `--collector.drm`** — `systemctl --user cat node-exporter`, and
   remember the collector is default-disabled upstream, so it is opt-in forever.
3. **the card really is gone**: module reloaded, kernel updated, driver refused to bind.
   `ls -l /sys/class/drm/` and the probe's output say so in one line.

**“I want to change a panel.”** See [Changing a panel](#changing-a-panel) — the
short version is that panels live in git, and the dashboard JSON is generated.

## Troubleshooting

| symptom | look at |
| --- | --- |
| prometheus restarts in a loop | `docker compose logs --tail=50 prometheus` — a rule file that does not parse or does not exist is fatal at start-up, not a warning |
| grafana exits immediately | a read-only rootfs needs every write path on a volume/tmpfs; `GF_PATHS_LOGS` is set for exactly this, so a new `mkdir: permission denied` in the log means a new write path |
| `llm-server` target `context deadline exceeded` | the container network cannot reach the upstream — use the passthrough job (see the two-jobs section) |
| `llm-server-via-proxy` DOWN | serve.py is not running, or its `--url`/`LLM_MONITOR_UPSTREAM` differs from the `llm_upstream` label in `prometheus.yml` |
| panels say "No data" but targets are UP | the server spells its metrics differently — add the spelling to `recording_rules.yml` / the dashboard's `{__name__=~"a\|b"}` selectors |
| `bind: address already in use` | change `PROMETHEUS_PORT` / `GRAFANA_PORT` in `.env` |
| TSDB growing past the cap | the size cap deletes whole blocks on compaction; check `prometheus_tsdb_head_series` for a label explosion (`promtool check config` won't catch high cardinality, only a query will) |
| GPU & host row says "No data" / "no GPU telemetry" | the exporter, not the GPU: `systemctl --user status node-exporter`, `curl -s 127.0.0.1:9100/metrics`, then `up{job="host"}` — runbook “The GPU & host row is empty.” |
| `node_scrape_collector_success{collector="drm"}` is 0 | the GPU half of the scrape failed and everything else still looks healthy: partial `/sys` mount, missing `--collector.drm`, or the card is gone. Runbook entry of the same name. |
| GPU panels show a flat 0 °C or 0 % busy | probably "could not ask", not "cold and idle" — node_exporter zero-fills unreadable attributes; `amdgpu_sysfs_probe.py probe` distinguishes them |
| forgot the Grafana password | `docker compose -f monitoring/compose.yaml exec grafana grafana cli admin reset-admin-password 'new'` — then keep `.env` and the stored hash in step |
