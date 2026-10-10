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
| `grafana/dashboards/` | provisioned dashboard JSON (see "The dashboard") |
| `grafana/make_dashboard.py` | generator for the dashboard JSON (edit this, not the JSON) |

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

### What has actually been measured

Read this before quoting a number: the two container rows are **not measured**.
This tree was built in a development container with no Docker in it, and a
fabricated footprint number is worse than an empty cell. What *is* measured is the
baseline the stack is added on top of:

| process | RSS | PSS | swap | CPU | provenance |
| --- | --- | --- | --- | --- | --- |
| `monitor/serve.py`, idle | 26.6 MB | 17.1 MB | 0 MB | ~0 % | measured, dev container, python 3.14.5 x86_64 |
| `monitor/serve.py`, after ~500 proxied requests | 29.2 MB | 18.7 MB | 0 MB | ~1 % | measured, same |
| `prometheus` | — | — | — | — | **not measured** — run `measure_footprint.sh` on the box |
| `grafana` | — | — | — | — | **not measured** — run `measure_footprint.sh` on the box |
| TSDB growth per day | — | — | — | — | predicted ≈ 5 MB/day at ~400 series, see Retention sizing |

`monitoring/measure_footprint.sh` prints this table for real: it reads
`Pss` from `/proc/<pid>/smaps_rollup` for the monitor, `docker stats --format json`
for the containers, and the TSDB size from the volume — and prints `not running`
instead of a plausible-looking zero. Run it once with the stack down
(`--before`) and once with the stack up and the model loaded, then paste the output
into the table above and commit it. That is the deliverable `workspace-szw.9`.

The other half of the budget is the one that is not telemetry at all: the model's
shared-memory headroom must not move. Re-run the benchmark from the root README
with the stack up and down and compare —

```sh
llama-bench -pg 512,128 -m ~/models/qwen3.8-27B/Qwen3.8-27B-UD-Q4_K_M.gguf \
  -b 2048 -ub 512 -ngl 99 --flash-attn on --load-mode none
```

— remembering the caveat already written there: `llama-bench` does not exercise MTP,
so a speculative-decode regression will not show up in it.

### <a name="go-no-go"></a>Go / no-go

Decided in advance, so nobody has to decide it at 02:00:

* **Go** — measured total ≤ 768 MiB *and* `llama-bench` unchanged: keep both
  services always-on, which is the default.
* **No-go** — measured total > 768 MiB, or the model lost headroom: keep
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

Adding a serving stack means adding its metric names to the selector constants in
`make_dashboard.py` and to `recording_rules.yml` — not a new regex in `app.js`, and
not twenty panel edits. `check_monitoring.py` fails if a panel plots an `llm:*` name
that no rule records.

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
* **Monitoring of the box itself** (CPU, RAM, GPU, disk). This stack watches the
  model server. The host/GPU half was spiked in `workspace-szw.10` —
  see [`../spikes/amd-gpu-metrics/FINDINGS.md`](../spikes/amd-gpu-metrics/FINDINGS.md),
  which picks `node_exporter --collector.drm` (no ROCm, ~24 MiB, 13 GPU series)
  and measures it against the footprint budget. Deploying it is
  `workspace-szw.13`; until that ticket closes, GPU and host metrics are
  deliberately *not* here.

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
| forgot the Grafana password | `docker compose -f monitoring/compose.yaml exec grafana grafana cli admin reset-admin-password 'new'` — then keep `.env` and the stored hash in step |
