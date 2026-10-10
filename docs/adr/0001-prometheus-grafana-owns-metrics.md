# ADR 0001 — Prometheus + Grafana own `/metrics`; `monitor/serve.py` becomes a scrape passthrough + live-status page

| | |
| --- | --- |
| **Status** | **Accepted** (2026-10-10) |
| **Date** | 2026-10-10 |
| **Deciders** | danielstahl (reporter/owner) |
| **Beads** | decision `workspace-szw.1`, child of epic `workspace-szw` |
| **Unblocks** | `workspace-szw.2` (passthrough + `llm_monitor_*` self-metrics), `workspace-szw.3` (`monitoring/` compose stack) |
| **Follow-ups bound by this ADR** | `workspace-szw.4` (dashboard), `workspace-szw.5` (alert rules), `workspace-szw.6` (refocus the page), `workspace-szw.7` (docs/runbook), `workspace-szw.8` (CI), `workspace-szw.9` (footprint budget), `workspace-szw.11` (parity-check + delete bespoke math), `workspace-szw.12` (LAN exposure) |
| **Supersedes** | nothing (first ADR in this repo) |

> This file is the canonical record of the decision. Code that contradicts it is a
> bug; a change of mind means amending this file (new Status line + a "Amended by"
> line), not silently drifting.

## Summary

Monitoring is split into two layers with a hard boundary between them:

1. **Prometheus + Grafana become the system of record for `/metrics`.** Everything
   derived from the Prometheus exposition format — throughput, latency quantiles,
   KV-cache pressure, cache / speculative-decode ratios, retention, alerting —
   lives in PromQL, a committed recording-rules file, and a provisioned Grafana
   dashboard.
2. **`monitor/serve.py` stays, with its scope frozen** to the three things only it
   can do: (a) a **raw scrape passthrough** Prometheus can pull through when the
   model server is not reachable from the container network, (b) the
   zero-dependency **"right now"** status page for `/health`, `/v1/models` and
   `/cache` — which are *not* Prometheus metrics — plus the raw exposition text
   for last-mile debugging, and (c) **deep links** into the equivalent Grafana
   panels.

## Context

`monitor/serve.py` is a stdlib-only same-origin proxy + static host
([monitor/README.md](../../monitor/README.md)). It polls four read-only endpoints
on the model server. Three of them are ad-hoc JSON; `/metrics` is the Prometheus
text exposition format and holds the operationally interesting signal: decode and
prompt throughput, time-to-first-token histograms, KV-cache pressure, prompt-cache
hit rate, speculative-decode draft acceptance, queue depth.

The reporter's instinct — "`/metrics` holds the meat" — is correct, and the meat is
already in Prometheus's own wire format. What the bespoke page does with it today
is the problem:

* **The counter math is ours and it is wrong-ish.** `app.js` renders tok/s from
  server-supplied *average* gauges (e.g. `llamacpp:predicted_tokens_seconds`) and
  derives ratios from two instantaneous counter snapshots — draft acceptance as
  `draft_tokens_accepted_total / draft_tokens_total`, prompt-cache hit rate as
  `cached / (total + cached)`. There is no window, no `rate()`, no counter-reset
  or restart handling, and no staleness semantics. PromQL does all of that
  correctly for free.
* **No history.** `HISTORY_SAMPLES = 60` (`monitor/static/app.js:75`), held in
  browser memory per tab. Reload = amnesia. The page cannot answer "what was
  decode throughput at 03:00" and cannot alert on anything.
* **Vendor drift is unbounded.** `METRIC_SIGNALS`
  (`monitor/static/app.js:957-973`) is already a 15-entry table of exact names
  plus fuzzy regexes guessing between `llamacpp:*`, `halogen:*` and `llama:*`
  spellings, with a second fuzzy pass that silently matches "something close".
  Every new server family (vLLM, SGLang, the next llama.cpp) means more bespoke
  regexes. Grafana template variables plus a small recording-rules file absorb
  that variance instead.
* **No alerting, no cross-server aggregation, no retention policy.** All three are
  table stakes in the Prometheus stack and would be bespoke features here.

Re-implementing a TSDB, a query language and a charting engine inside a
stdlib-only page is the wrong place to spend effort.

## Constraints that shape the design (these are not free)

* **This box is RAM-starved by design.** The root README ships
  `amdgpu.gttsize` / `ttm.pages_limit` hacks specifically to free memory for model
  weights. Therefore Prometheus runs with
  `--storage.tsdb.retention.size` (a *size* cap, not only a time cap) and a hard
  container memory limit; Grafana is single-instance, no plugins, no external CDN
  assets.
* **"Nothing loaded from outside the box" is an existing principle**
  (`monitor/README`). So: fully self-hosted — no Grafana Cloud, no hosted Grafana —
  and dashboards, datasources, rules and compose files are **provisioned from git**,
  not clicked into existence.
* **`serve.py` binds loopback on purpose**, because `?url=` lets a caller make it
  fetch an arbitrary URL. The passthrough must not widen that SSRF surface: same
  URL validation, loopback default unchanged, and the *preferred* scrape path stays
  Prometheus → model server **directly**.
* **Some servers have no `/metrics` at all** (404 — see the `nometrics` mode of
  `monitor/tests/mock_upstream.py`). Prometheus cannot scrape those, which is
  exactly why `serve.py` is kept rather than deleted.

## Decision

### Layer 1 — Prometheus + Grafana (new, self-hosted, in git)

* A `monitoring/` directory holds a `docker compose` stack: `prometheus/prometheus`
  and `grafana/grafana`, image tags **pinned** (digest or exact tag, never
  `latest`), `mem_limit` and `cpus` set, `restart: unless-stopped`.
* Prometheus: `retention.time=15d` **and** `retention.size=2GB` (the size cap is
  mandatory on this box), `scrape_interval: 15s`, `scrape_timeout: 5s` (matching
  `serve.py`'s own 5 s cap — see `UPSTREAM_TIMEOUT_SECONDS`,
  `monitor/serve.py:103`), bound to `127.0.0.1:9090`.
* Grafana: provisioned datasource + provisioned dashboard, sign-up off, plugins
  disabled, update/analytics checks off, bound to `127.0.0.1:3000`.
* Alert rules live in `monitoring/prometheus/rules.yml` (Alertmanager is phase 2).
* Every knob is a committed `.env.example` entry.

### Layer 2 — `monitor/serve.py`, scope frozen

`serve.py` **may**:

1. proxy the non-Prometheus JSON endpoints (`/api/health`, `/api/models`,
   `/api/cache`) for the page, exactly as today;
2. answer `/api/metrics` — the JSON-shaped view (`body` verbatim + parsed
   `metrics`) that the page renders;
3. answer **`GET /metrics`** as a raw scrape passthrough for Prometheus, plus its
   own `llm_monitor_*` self-metrics ("monitor the monitor");
4. serve the static page and link out to Grafana;
5. report where Grafana / Prometheus live (`/api/config`) and, when configured, a
   proxied digest of Prometheus' target health (`/api/scrape-health`).

`serve.py` **must not**:

* store samples, keep history across a process restart, or grow a database —
  no SQLite, no ring buffers presented as history;
* implement rate/quantile/ratio math as the *authoritative* number. In-process
  derived tiles are transitional only and are removed once Grafana reaches parity
  (`workspace-szw.11`);
* claim to be the historical view. Anything on the page is "right now, ±60 samples
  in this tab" and must be labelled that way (`workspace-szw.6`);
* add a charting library, a query language, or any third-party dependency — the
  stdlib-only, no-build-step, nothing-from-outside-the-box property of `serve.py`
  is *why it survives* this decision;
* rename, relabel or otherwise rewrite upstream series (sub-decision below).

### Sub-decision 1 — do not normalize metric names at scrape time

llama.cpp emits `llamacpp:prompt_tokens_seconds`. Colons are legal in Prometheus
names but conventionally reserved for recording-rule output. **Keep native
names**: no `metric_relabel_configs` rewriting them into underscores. Rewriting
loses fidelity, breaks the passthrough (which is byte-for-byte by contract), and
makes copy-pasting upstream docs fail. Vendor variance is absorbed instead by
(i) a small recording-rules file publishing neutral `llm:*` aliases and (ii)
dashboard template variables. `metric_relabel_configs` may only ever *drop* noise.

### Sub-decision 2 — the passthrough is a fallback, not the primary path

Two scrape jobs are defined (`llm-server` direct, `llm-server-via-proxy` through
`serve.py`); `prometheus.yml` documents **which one to keep**, because scraping
both double-counts every panel. Direct scraping is the default; the passthrough
exists for the case where the model server is not reachable from the container
network.

### Sub-decision 3 — Grafana is configured from git only

Datasources, dashboard provider, dashboards, recording rules and alert rules are
committed files mounted read-only. A fresh checkout must stand the whole stack up
with one command and zero clicks. Dashboard JSON is reviewed like code
(`workspace-szw.8`).

## Target architecture

```
  model server /metrics (Prometheus text)
     │
     ├──(preferred: direct scrape)──────────┐
     │                                      v
     └──> serve.py  GET /metrics  ──────>  Prometheus  ── rules.yml ──> (Alertmanager, phase 2)
              (raw passthrough +           (size-capped      │
               llm_monitor_* self-metrics)  retention)      v
                                                     Grafana (provisioned
  serve.py static page = "right now":                datasource + dashboard
    /health /v1/models /cache + raw metrics         from git; vars:
    + llm_monitor_* scrape health                   instance, model
    + "open in Grafana" deep links           ◀────── deep links from the proxy page
```

## Contracts (these bind the follow-up tickets)

### C1 — `GET /metrics` on the proxy

| property | contract |
| --- | --- |
| path | `/metrics` on the proxy, claimed ahead of `static/` so it can never be shadowed by a static asset |
| upstream | `<upstream>/metrics`; `?url=<http(s) base>` override, run through the existing `normalize_upstream()` validation (so one proxy can be scraped as several distinct targets) |
| success | `200`, `Content-Type: text/plain; version=0.0.4; charset=utf-8`, body = upstream bytes **verbatim** (only a missing final newline repaired) + an appended `llm_monitor_*` block |
| upstream non-2xx / unreachable / over the 5 s cap | `502` + one-line `text/plain` reason, so Prometheus records a **scrape failure** instead of parsing garbage |
| malformed `?url=` | `400` + one-line `text/plain` reason |
| not the envelope | `/metrics` is the only non-JSON route; `/api/metrics` keeps the seven-key envelope + parsed `metrics` for the page |
| fidelity | no renaming, no relabelling, no reordering, no gzip trickery, no HTML |
| bounds | existing caps unchanged: `MAX_BODY_BYTES` 4 MiB (`monitor/serve.py:111`), `MAX_SERIES` 2000 for the parsed view, 5 s wall-clock per upstream request |
| binds | default stays loopback; `--host 0.0.0.0` is an explicit information-disclosure decision (the route hands out model ids, traffic shape and prompt-cache behaviour) |

### C2 — self-metrics (`llm_monitor_*`, underscores because they are ours)

| series | type | meaning |
| --- | --- | --- |
| `llm_monitor_scrape_success_total{upstream}` | counter | passthrough scrapes that returned exposition text |
| `llm_monitor_scrape_ok{upstream}` | gauge | 1 if the most recent passthrough scrape succeeded |
| `llm_monitor_scrape_latency_ms{upstream}` | gauge | round-trip of the most recent scrape |
| `llm_monitor_scrape_upstream_status{upstream}` | gauge | upstream HTTP status of it (0 = no answer) |
| `llm_monitor_parse_errors_total{upstream}` | counter | exposition lines this proxy could not parse |
| `llm_monitor_parse_truncated{upstream}` | gauge | 1 when the series cap cut the last scrape short |
| `llm_monitor_series_count{upstream}` | gauge | series kept from the last good scrape |
| `llm_monitor_build_info{version}` | gauge | constant 1; labels carry the monitor version |

Rules: counters are process-lifetime (a monitor restart *is* a counter reset,
which PromQL handles); one row per `upstream`, and a scrape of `/metrics?url=A`
renders **only** A's row so `sum(llm_monitor_*)` across targets never
double-counts. `rate(llm_monitor_scrape_success_total[…])` is the canonical
"is the monitor actually scraping" signal used by `workspace-szw.5`.

### C3 — recording rules publish the neutral aliases

`monitoring/prometheus/rules.yml` is the single place that knows vendor names.
Canonical aliases (quantiles carry the `:quantile` suffix per Prometheus
convention):

| alias | from |
| --- | --- |
| `llm:decode_tokens_per_second` | `rate(llamacpp:predicted_tokens_seconds[…])` / vendor equivalent |
| `llm:prompt_tokens_per_second` | `rate(llamacpp:prompt_tokens_seconds[…])` |
| `llm:time_to_first_token_seconds:p99` (and `:p50`, `:p90`) | `histogram_quantile(… , rate(*_bucket))` |
| `llm:kv_cache_utilization` | `llamacpp:kv_cache_usage_ratio` / `halogen:kv_pool_positions` ÷ pool size |
| `llm:prompt_cache_hit_ratio` | `rate(cached[5m]) / (rate(total[5m]) + rate(cached[5m]))` |
| `llm:draft_accept_ratio` | `rate(draft_tokens_accepted_total[5m]) / rate(draft_tokens_total[5m])` |
| `llm:requests_running`, `llm:requests_queued` | `requests_processing` / `requests_deferred` |
| `llm:scrape_ok`, `llm:scrape_success_ratio` | from `llm_monitor_*` (C2) |

Vendor spellings are resolved **here**, never in the browser. Adding a server
family means adding rules, not adding regexes.

### C4 — Grafana deep links

`/api/config` publishes `grafana_url` and `prometheus_url` (opt-in via
`--grafana-url` / `$LLM_MONITOR_GRAFANA_URL` and `--prometheus-url` /
`$LLM_MONITOR_PROMETHEUS_URL`; unset means "no Grafana on this box", which is a
supported configuration, not a broken one — the links simply do not render).

Link shape: `<grafana_url>/d/llm-server/llm-server?orgId=1&var-instance=<upstream
host:port>&var-model=<model id>[&viewPanel=<id>][&refresh=30s]`.

Dashboard **uid is `llm-server`** and these panel ids are reserved (the dashboard
ticket may add panels but must not renumber these):

| `viewPanel` | panel |
| --- | --- |
| 1 | decode throughput (tok/s) |
| 2 | prompt throughput (tok/s) |
| 3 | latency quantiles — TTFT p50/p90/p99 |
| 4 | KV-cache pressure over time |
| 5 | requests running / queued |
| 6 | prompt-cache hit ratio + draft-accept ratio |
| 7 | scrape health (`llm_monitor_*`) + uptime |

`var-instance` is the scrape `instance` label, i.e. the upstream's `host:port`;
that mapping is what lets a link from the page land on the right series.

### C5 — resource budget (provisional; `workspace-szw.9` confirms and publishes measured numbers)

| | limit |
| --- | --- |
| prometheus | `mem_limit: 512m`, `cpus: 0.50`, `retention.size: 2GB`, `retention.time: 15d` |
| grafana | `mem_limit: 256m`, `cpus: 0.25` |
| total added RSS | ≤ 768 MiB, measured, and **the model server's shared-memory headroom must be unchanged** |
| network | nothing in the stack may egress; both UIs bound to `127.0.0.1` |

If the measured footprint does not fit, the retention size comes down before the
memory limit goes up.

### C6 — config surface added by this decision

| surface | meaning |
| --- | --- |
| `--grafana-url` / `$LLM_MONITOR_GRAFANA_URL` | base URL of the Grafana that owns history; enables deep links |
| `--prometheus-url` / `$LLM_MONITOR_PROMETHEUS_URL` | base URL of the Prometheus scraping this box; enables the scrape-health read-out |
| `GET /api/scrape-health` | thin proxy of `<prometheus>/api/v1/targets` returning an envelope + a `scrape_health` digest (`targets_total`, `targets_up`, per-job health/last_error). The browser must not call Prometheus directly — that reintroduces the CORS problem this proxy exists to solve |
| `/api/config` | additive keys `grafana_url`, `prometheus_url`, `version`; the three pre-existing keys are unchanged |

## Alternatives considered and rejected

* **(A) Rebuild the Grafana feature set inside `serve.py`** — add a store, a
  query-language subset and a chart lib to the stdlib page. Months of work, still
  no alerting, and it breaks the no-dependencies principle *worse* than running two
  real, purpose-built services.
* **(B) Scrape into SQLite from the proxy and chart it client-side** — (A) with
  extra steps: hand-rolled retention, hand-rolled rates, still no PromQL.
* **(C) Managed Grafana / Grafana Cloud** — violates "nothing from outside the box"
  and ships request/usage data off the machine.
* **(D) Chosen: self-hosted Prometheus + Grafana** delivered as `docker compose`
  (this repo already lives in a distrobox/docker world — see
  `qwen3.8-flash/docker-compose.yml`), with all config, rules and dashboard JSON in
  git and reviewed like code.

## Consequences

**Becomes possible:** correct `rate()` semantics with counter-reset handling; 7-day
throughput and p99 TTFT; per-model and per-server comparison; "was the KV cache full
overnight"; alerting; aggregation; retention as a configuration knob instead of a
browser array.

**Becomes our problem:** two more long-running services on a memory-starved box
(bounded by C5, measured by `workspace-szw.9`); a second place to look, mitigated by
deep links (C4) and honest labelling (`workspace-szw.6`); config drift, mitigated by
provisioning from git (sub-decision 3) and CI validation (`workspace-szw.8`);
upgrade churn on two pinned images; Prometheus itself can be OOM-killed, which is
why its own scrape health is visible *through the monitor* (C2, `/api/scrape-health`)
rather than only inside Grafana.

**Deliberately not solved here:** auth/IAM, multi-host federation, per-request
tracing, LAN exposure of Grafana (`workspace-szw.12`), host/GPU exporters
(`workspace-szw.10`).

**Migration path for the bespoke math:** the derived tiles stay in `app.js` until
Grafana reaches parity for each one (`workspace-szw.11` parity-check), then the
client-side counter arithmetic is deleted and the page keeps raw series, raw
exposition text and link-outs. Nothing is deleted before parity, and nothing is
kept after it.

## Definition of success

* Grafana answers questions the current page cannot: throughput/latency over 7 days,
  p99 TTFT, per-model comparison, "was the KV cache full overnight".
* No hand-written counter math remains on the hot path in `app.js`; the page no
  longer claims to be the historical view.
* Total added RSS on this box is bounded and measured, and the model server's
  shared-memory headroom is unchanged.
* `docker compose` + the monitor still work standalone on a server with no `/metrics`
  at all.
* A fresh checkout can stand the whole stack up from committed files alone.

## Revisit this decision when

* more than one box needs to be correlated → reconsider federation / a central
  Prometheus rather than per-box Grafana;
* Prometheus + Grafana cannot fit inside C5 even after retention is trimmed → the
  fallback under consideration is a single-binary TSDB (Victoria Metrics +
  `vmagent`) with the same Grafana datasource contract, **not** (A) or (B);
* a supported server we adopt ships a maintained dashboard we can consume verbatim →
  consume it instead of maintaining `llm-server` dashboard JSON;
* the exposition format changes materially (OpenMetrics/OTLP-native servers with no
  text endpoint) → the passthrough contract C1 needs an amendment.

## Implementation status at acceptance

Landed in `monitor/serve.py` (in-tree at time of acceptance): `GET /metrics`
passthrough with verbatim body + `llm_monitor_*` self-metrics (C1, C2),
`/api/scrape-health`, `--grafana-url` / `--prometheus-url` + env, and the
`/api/config` additive keys (C4, C6). `monitor/README.md` documents those routes and
this layering.

Not yet done, by ticket: tests for the passthrough in `tests/test_serve.py`
(`workspace-szw.2` — which that work is still open for); the `monitoring/` compose
stack and provisioning (`workspace-szw.3`); dashboard JSON plus the `llm:*`
recording rules (C3, `workspace-szw.4`); alert rules (`workspace-szw.5`); the page
refocus and the Grafana deep links, including deleting the derived counter math
(C4, `workspace-szw.6`, `workspace-szw.11`); CI validation (`workspace-szw.8`);
measured footprint against the C5 budget (`workspace-szw.9`).
