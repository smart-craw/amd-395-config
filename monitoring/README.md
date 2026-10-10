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
| `prometheus/prometheus.yml` | scrape config: direct job, passthrough job, self-scrape |
| `prometheus/targets/llm-servers.json` | the direct scrape target list (hot-reloaded) |
| `prometheus/rules.yml.tmpl` + `prometheus/alert_thresholds.env` | alert rules template + the thresholds to tune per box |
| `prometheus/rules.yml` | **generated** from those two — committed, drift-checked |
| `prometheus/recording_rules.yml` | the neutral `llm:*` aliases (hand-written) |
| `prometheus/unit-tests.yml` | `promtool test rules` unit tests for the alert rules |
| `render_rules.py` | renders `rules.yml` from the template + thresholds (`--check` for CI) |
| `grafana/provisioning/` | datasource + dashboard provider, mounted read-only |
| `grafana/dashboards/` | provisioned dashboard JSON (see "Changing a panel") |
| `grafana/make_dashboard.py` | generator for the dashboard JSON |

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
is running** (`python3 monitor/serve.py --url …`), `prometheus` up. Then open
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
* ⇒ `3 000 × 5760 × 1.5 B ≈ 26 MB/day` → **~0.4 GB over 15 days**, i.e. the size
  cap is insurance for a second server, a chattier `/metrics`, or a leaked
  high-cardinality label — not the normal working limit.

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

`promtool` says the rules are valid; whether they *fire* on the right shape of
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
prompt-cache going cold.

**There is no Alertmanager**, deliberately: pointing it anywhere means choosing an
outbound notification channel (email, webhook, Telegram), and outbound egress is a
separate decision. The `alertmanager` block in `compose.yaml` is commented out
with the reasoning; the rules are useful without it (they render in the Prometheus
UI's Alerts page and in Grafana's alerting UI on the provisioned datasource).

## Recording rules vs raw PromQL in panels

`recording_rules.yml` publishes the neutral `llm:*` aliases the ADR names as the
vendor-drift seam (`llm:decode_tokens_per_second`, `llm:kv_cache_usage_ratio`,
…). The provisioned dashboard deliberately spells the raw expressions instead —
so it renders against a server that has never seen these rules, and a panel that
shows "No data" means *no data*, not *no rule*. Hand-written queries and future
alert rules should use the `llm:*` aliases; adding a new serving stack means
adding one rule there, not a new regex in four places.

## Changing a panel

Never in the UI: `allowUiUpdates: false`, `disableDeletion: true` and
`updateFolderItems: false` in the provisioned provider make the committed JSON
the only version that matters, and a pane you "fixed" in the browser would be
silently reverted on the next restart.

```sh
# edit monitoring/grafana/dashboards/llm-server.json
#   (or edit grafana/make_dashboard.py and re-run it, then commit both)
python3 monitoring/check_monitoring.py
docker compose -f monitoring/compose.yaml restart grafana
```

Panel **ids are a contract**: [ADR 0001 C4](../docs/adr/0001-prometheus-grafana-owns-metrics.md)
reserves `viewPanel=1..7` for specific panels because `monitor/serve.py`
deep-links into them, and the uid must stay `llm-server` (`check_monitoring.py`
pins the uid and the datasource variable). Panel ids themselves are reviewed by
hand — and note that as shipped the row panels reuse their children's ids, which
makes a `viewPanel=` link ambiguous; tightening that is `workspace-szw.4`'s work.

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
promtool check config monitoring/prometheus/prometheus.yml
promtool check rules monitoring/prometheus/{rules,recording_rules}.yml
promtool test rules monitoring/prometheus/unit-tests.yml
docker compose -f monitoring/compose.yaml config -q
```

`promtool` is a single static binary from the Prometheus release matching the
pinned image (and it is inside the pinned image too: `docker compose -f
monitoring/compose.yaml exec prometheus promtool …`).
`check_monitoring.py --promtool` runs the promtool checks when it is installed,
so the checker never becomes the reason a box can't verify itself.

## Deliberately not here

* **LAN exposure.** Both ports are published on `127.0.0.1` and nothing else.
  Prometheus has no authentication at all — loopback *is* its access control.
  Publishing either UI on the LAN is its own opt-in, hardened ticket
  (`workspace-szw.12`), not a value to tweak in `.env`.
* **Alertmanager / any egress** (above).
* **A committed `.env`.** It holds the admin password; only `.env.example` is in
  git, and `.gitignore` keeps the real one out.
* **Monitoring of the box itself** (CPU, RAM, GPU, disk). This stack watches the
  model server; node metrics are a separate decision with its own memory cost.

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
| forgot the Grafana password | `docker compose -f monitoring/compose.yaml exec grafana grafana cli admin reset-admin-password 'new'` — then keep `.env` and the stored hash in step |
