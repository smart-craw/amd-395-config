# monitor — LLM server dashboard

A single-page dashboard for a vLLM / llama.cpp style model server. Three files
matter:

| path                  | what it is                                                  |
| --------------------- | ----------------------------------------------------------- |
| `serve.py`            | stdlib-only same-origin proxy + static file host            |
| `static/index.html`   | the dashboard (with `static/app.js`, `static/styles.css`)   |
| `llm-monitor.service` | systemd **user** unit that keeps `serve.py` running         |
| `tests/`              | proxy, markup-contract and headless UI tests (see below)     |

It polls the four read-only endpoints the model server already exposes —
`/health`, `/v1/models`, `/cache` and `/metrics` — through a tiny local proxy, so
the page and its API share one origin.

## Two layers: what this page is, and what it is not

Monitoring on this box has two layers with a hard boundary between them; the
decision and its rejected alternatives are recorded in
[ADR 0001](../docs/adr/0001-prometheus-grafana-owns-metrics.md).

* **Prometheus + Grafana own `/metrics` — history.** Anything derived from the
  Prometheus exposition format (throughput, latency quantiles, KV-cache pressure,
  cache and speculative-decode ratios, retention, alerting) is PromQL + a
  provisioned Grafana dashboard, described in `monitoring/` once that stack lands.
* **`serve.py` owns "right now"** — and its scope is deliberately frozen to what
  only it can do:
  1. a **same-origin proxy** for `/health`, `/v1/models` and `/cache`, which are
     ad-hoc JSON and *not* Prometheus metrics;
  2. a **raw scrape passthrough** — `GET /metrics` answers the upstream exposition
     body verbatim plus this proxy's own `llm_monitor_*` series — for the case
     where Prometheus cannot reach the model server directly. Direct scraping of
     the model server remains the preferred path;
  3. the **zero-dependency live status page**, including the raw exposition text for
     last-mile debugging — plus, as the page gets refocused, deep links into the
     equivalent Grafana panels (`--grafana-url` already feeds them; the link-out in
     `static/app.js` is still to come).

What `serve.py` is *not*: a time-series database, a query language, or the
historical view. The page keeps ~60 samples in tab memory for sparklines and that
is all the history it has — reload and it is gone. Anything older than a few minutes
is a Grafana question.

## Quick start

```sh
python3 monitor/serve.py
# then open http://localhost:8090/
```

Same thing with the defaults spelled out:

```sh
python3 monitor/serve.py --host 127.0.0.1 --port 8090 --url http://llm.home:8001
```

Nothing to install: `serve.py` is standard-library Python 3, and the UI is plain
HTML/CSS/JS with charts drawn as inline `<svg>` — no build step, no npm, no CDN,
no third-party deps, and nothing loaded from outside the box. `python3
monitor/serve.py --help` lists the flags and their defaults.

## Pointing it at a server

The upstream (the model server being watched) is resolved at startup, first match
wins:

| source                     | example                                          |
| -------------------------- | ------------------------------------------------ |
| `--url`                    | `python3 monitor/serve.py --url http://localhost:8000` |
| `$LLM_MONITOR_UPSTREAM`    | `export LLM_MONITOR_UPSTREAM=http://localhost:8080`   |
| built-in default           | `http://llm.home:8080`                           |

Three ways to change the target, cheapest first:

* **From the UI, no restart** — type a base URL in the endpoint selector (or hit
  a preset) and Apply. Every poll then carries `?url=<base>` on the proxy route,
  which overrides the upstream for that request only. The choice is kept in
  `localStorage`, so it survives a reload, but it never changes the server's own
  config — `/api/config` keeps reporting the startup value.
* **`--url` on the command line** — sets the server-side default that
  `?url=` overrides sit on top of. Restart the process to pick up a change.
* **`LLM_MONITOR_UPSTREAM`** — same thing without touching the command line;
  this is what `llm-monitor.service` uses, and `--url` wins if both are set.

There is no config file and no state on disk. Only absolute `http://` / `https://`
URLs with a host are accepted; anything else (`file:///etc/passwd`, a bare
`localhost:8000`) is refused with 400 on the proxy route. Because `?url=` lets a
caller make this server fetch an arbitrary URL, the default bind address is
loopback — pass `--host 0.0.0.0` only on a network you trust the requesters on.

## Run it as a service

[llm-monitor.service](./llm-monitor.service) is a systemd **user** unit for
`monitor/serve.py`, same shape as [../llm-server.service](../llm-server.service)
at the repo root. Put it in `~/.config/systemd/user/`, creating the folder if it
doesn't already exist, and update the `%h/service` path in `ExecStart` to point
at your actual checkout.

Run

```sh
systemctl --user daemon-reload
```

```sh
systemctl --user enable llm-monitor
```

```sh
systemctl --user start llm-monitor
```

The upstream comes from `Environment=LLM_MONITOR_UPSTREAM=http://llm.home:8001`
in the unit; edit that line (or add `--url` to `ExecStart`, which takes
precedence) and `systemctl --user restart llm-monitor` after any change. Logs go
to the journal:

```sh
journalctl --user -u llm-monitor -f
```

To run at boot without login:

```sh
sudo loginctl enable-linger $USER
```

## What the page shows

* **top bar** — status pill (`healthy` / `degraded` / `down`), the upstream being
  polled, `updated HH:MM:SS · next poll in mm:ss`.
* **endpoint selector** — editable upstream base URL, prefilled from
  `/api/config`, with presets taken from this repo's own launch scripts
  (`llm.home:8001`, `localhost:8000` from `qwen3-27b/llama-start-qwen3.8-27b.sh`,
  `localhost:8080` from `gemma4/llama-start-gemma4-26moe.sh`). Apply persists to
  `localStorage` and re-polls immediately with `?url=`, no reload.
* **poll interval** — 2s / 5s / 15s / paused, also persisted. Overlapping polls
  are prevented: the previous cycle is aborted and late answers are dropped.
* a 404 on an *optional* endpoint (`/cache`, `/metrics`: not stock routes, many
  servers do not have them) is shown as a muted note on that card — "this server
  does not answer /metrics (HTTP 404)" — and leaves the status pill healthy.
* **health card** — `/health`: status text, HTTP code, round-trip latency, last
  check, uptime since the first good poll, plus any other field the server sends.
* **models card** — `/v1/models`: count badge and a table of
  `id / object / owned_by / created` (human-readable date); an empty list says
  so explicitly.
* **cache card** — `/cache`: utilisation as a labelled progress bar with the raw
  numbers, then a key/value grid of everything the endpoint returned, then a raw
  JSON panel.
* **metrics card** — `/metrics`: a KV-cache progress bar, tiles for generate
  tok/s, prompt tok/s, requests in flight, requests total, draft-accept rate and
  prompt-cache hit rate, a throughput history chart, one row per scraped series
  and the raw exposition text. See “Why `/metrics` is parsed server-side”.
* every card has a ~60-sample latency sparkline (min / max / now labelled), an
  error strip with a retry button, stale styling once the last good poll is
  older than 3 intervals, and a full-width banner appears when *all* endpoints
  fail.

## Why the proxy exists

The model server sends **no CORS headers**, so a dashboard served from any other
origin (another port, another box, `file://`) would be blocked by the browser
from reading `/health`, `/v1/models` and `/cache` directly. So `serve.py` hosts
the UI and the API on the *same origin*: the page only ever talks to its own
server, and that server — not a browser, so no same-origin policy — does the
fetching and hands the result back. Nothing on the model server side has to
change.

## The API surface

| route                    | upstream                | notes                                    |
| ------------------------ | ----------------------- | ---------------------------------------- |
| `GET /api/health`        | `<upstream>/health`     | proxied                                  |
| `GET /api/models`        | `<upstream>/v1/models`  | proxied                                  |
| `GET /api/cache`         | `<upstream>/cache`      | proxied                                  |
| `GET /api/metrics`       | `<upstream>/metrics`    | proxied, **plus** `metrics` (parsed)      |
| `GET /api/config`        | *(no upstream call)*    | active upstream + the endpoints to offer |
| `GET /api/scrape-health` | `<prometheus>/api/v1/targets` | proxied digest; only useful with `--prometheus-url` |
| `GET /metrics`           | `<upstream>/metrics`    | **plain text, not an envelope**: the scrape passthrough Prometheus pulls through |

Any other `/api/...` path answers 404, a malformed `?url=` answers 400, and
everything that is not under `/api/` is served out of `static/` (directory
listings are disabled) — except `/metrics`, which is claimed by the passthrough and
never falls through to a static file. Each proxy route takes an optional
`?url=<http(s) base>` that overrides the upstream for that single request, including
on `/metrics`, so one proxy can be scraped as several distinct targets.

`GET /metrics` is the scrape passthrough (ADR 0001, contract C1): the upstream text
comes back byte for byte with
`Content-Type: text/plain; version=0.0.4; charset=utf-8` and the proxy's own
`llm_monitor_*` series appended — nothing renamed, nothing wrapped in JSON. A
non-2xx or unreachable upstream answers **HTTP 502** with a one-line plain-text
reason, because a scrape that silently returns garbage is worse than a failed
scrape. Exposing it past loopback is an information-disclosure decision: the whole
exposition (model ids, traffic shape, prompt-cache behaviour) is in that body.

### The `llm_monitor_*` self-metrics

Every successful passthrough appends the monitor's own series (ADR 0001, C2) —
underscores because they are ours, next to the upstream's untouched colon-names:

| series | type | meaning |
| ------------------------ | ------- | ---------------------------------------- |
| `llm_monitor_scrape_success_total{upstream}`  | counter | passthrough scrapes of that upstream that returned exposition text |
| `llm_monitor_scrape_ok{upstream}`             | gauge   | 1 when the most recent scrape of it succeeded |
| `llm_monitor_scrape_latency_ms{upstream}`     | gauge   | round-trip time of the most recent scrape |
| `llm_monitor_scrape_upstream_status{upstream}`| gauge   | upstream HTTP status of it (`0` = no answer) |
| `llm_monitor_parse_errors_total{upstream}`    | counter | exposition lines this proxy could not parse |
| `llm_monitor_parse_truncated{upstream}`       | gauge   | 1 when the series cap (`MAX_SERIES`) cut the last scrape short |
| `llm_monitor_series_count{upstream}`          | gauge   | series kept from the last successful scrape |
| `llm_monitor_build_info{version}`             | gauge   | constant 1; labels carry the monitor version |

Counters are process-lifetime (a monitor restart *is* a counter reset, which
PromQL handles), and a scrape of `/metrics?url=A` renders **only** A's row, so
`sum(llm_monitor_*)` across targets never double-counts.
`rate(llm_monitor_scrape_success_total[5m])` is the canonical "is the monitor
actually scraping" signal.

Bounds: the upstream body is capped at `MAX_BODY_BYTES` (4 MiB) exactly as the
other routes are, and a body that hits the cap says so — a
`# llm-monitor: upstream body truncated at … bytes; this scrape is incomplete`
comment line is appended rather than a torn scrape being handed over as if it were
whole. The parsed self-metrics reflect the `MAX_SERIES` (2000) cap via
`llm_monitor_parse_truncated` / `llm_monitor_series_count`; the body itself is
still the upstream's own bytes.

## The envelope

All six `/api/*` routes answer with the same shape, and always with HTTP 200
even when the upstream is on fire (upstream trouble lives inside the envelope,
not in the transport status):

```json
{"ok": true, "url": "http://llm.home:8001/health", "status": 200,
 "latency_ms": 4.2, "content_type": "application/json",
 "body": {"status": "ok"}, "error": null}
```

* `ok` — `true` only when the upstream answered 2xx/3xx.
* `url` — the absolute upstream URL actually fetched (`null` for `/api/config`).
* `status` — upstream HTTP status, `null` when there was no response.
* `latency_ms` — round-trip time, rounded to 2 decimals.
* `content_type` — upstream `Content-Type`, or `application/json` when the
  envelope was synthesised locally.
* `body` — parsed JSON when it is JSON (or claims to be), otherwise the raw text;
  `null` when there was no body.
* `error` — `null` on success, otherwise a one-line reason (timeout,
  connection refused, HTTP 500, ...).

A dead, slow or angry upstream is still HTTP 200 with `ok:false` and a populated
`error` — never a 500 that takes the dashboard down — and no request ever waits
longer than 5s for the upstream.

`/api/metrics` adds one key beyond the seven above: `metrics`, the parsed form of
`body` (see “Why `/metrics` is parsed server-side”). `/api/scrape-health` likewise
adds `scrape_health`; nothing else departs from the envelope, and `GET /metrics` (no
`/api/` prefix) departs from it entirely.

`/api/config` is the odd one out: it never calls the upstream, and its `body` is
`{"upstream": ..., "endpoints": ["/health", "/v1/models", "/cache", "/metrics"],
"default_upstream": ..., "grafana_url": ..., "prometheus_url": ..., "version":
...}`, with those three keys also copied onto the top level of the envelope so
`body.upstream` and `upstream` both work. `grafana_url` / `prometheus_url` come from
`--grafana-url` / `$LLM_MONITOR_GRAFANA_URL` and `--prometheus-url` /
`$LLM_MONITOR_PROMETHEUS_URL`; empty means "no Grafana / Prometheus on this box", a
supported configuration rather than a broken one — the scrape-health read-out (and,
once they land, the Grafana deep links) simply are not rendered.

`GET /api/scrape-health` is the monitor's view of the monitor: it proxies
`<prometheus>/api/v1/targets` and adds a `scrape_health` digest (`configured`,
`reachable`, `targets_total`, `targets_up`, and one `{job, instance, health,
last_error}` row per target). The browser never talks to Prometheus directly — that
would reintroduce the CORS problem this proxy exists to solve — and a down
Prometheus is an `ok:false` envelope, never a 500.

## Why `/metrics` is parsed server-side

`/metrics` is the one monitored endpoint that is not JSON: it answers the
Prometheus text exposition format (`text/plain; version=0.0.4`). Rather than ship
a second parser to the browser, `serve.py` leaves the text in `body` and adds one
extra key next to the envelope:

```json
{"format": "prometheus", "count": 17, "truncated": false, "errors": [],
 "families": [{"name": "llamacpp:requests_processing", "type": "gauge",
               "help": "Number of requests processing."}],
 "series": [{"name": "llamacpp:requests_processing", "labels": {},
             "value": 0.0, "value_text": "0"}]}
```

* `families` carries each metric's declared `type` and `help`; `series` carries
  the values, one entry per label set.
* `NaN` / `+Inf` / `-Inf` are legal in the exposition format and impossible in
  JSON, so such a sample has `value: null` and keeps its spelling in
  `value_text` — the response is always strict JSON.
* Anything the parser cannot read is skipped and named in `errors` (max 8) rather
  than losing the rest of the scrape; more than 2000 series sets `truncated`.
* If the server answers `/metrics` with something that is not Prometheus text at
  all, `metrics` is `null` and the card shows the raw payload.

The card never assumes a metric exists: each tile looks up the exact name this box
uses (`llamacpp:predicted_tokens_seconds`, `halogen:kv_pool_positions`, …) and
falls back to a name pattern, and a tile whose metric is missing is simply not
rendered. Two figures are derived rather than read: draft acceptance
(`draft_tokens_accepted_total / draft_tokens_total`) and prompt-cache hit rate
(`prompt_tokens_cached_total / (prompt_tokens_total + prompt_tokens_cached_total)`).

That parsing exists so the page can render *right now* without shipping a second
parser to the browser; it is explicitly **not** the system of record. Dividing two
instantaneous counter snapshots is not a rate — no window, no `rate()`, no
counter-reset handling — so those two derived tiles are transitional and are removed
once the Grafana panels cover them (ADR 0001, "Migration path for the bespoke
math"). Vendor name drift is meant to shrink here too: a new server family gets an
entry in the recording-rules file, not a new regex in `app.js`.

## Why `/cache` is parsed defensively

`/health` and `/v1/models` are inferable from this repo's launch scripts, but
`/cache` is not a stock vLLM/llama.cpp route and its schema cannot be confirmed
from here. So the cache card never assumes fields: it flattens whatever JSON (or
text) comes back, classifies each field as number / string / boolean, and looks
for a utilisation signal in this order —

1. a percentage-ish field (`*percent*`, `*pct*`, `*utiliz*`, `*occupanc*`),
2. a used + total pair, which includes `*_tokens` next to `n_ctx`
   (`used_tokens / n_ctx` → `used/total`),
3. a `free`/`available` field next to a total (`(total - free) / total`),
4. a lone `0..1` ratio field.

If none of that matches, the card still renders the full key/value grid plus the
raw payload — it is never blank on an unknown shape.

## Smoke test without a GPU box

A stand-in model server is enough to click through the whole UI:

```sh
# terminal 1 - a fake upstream on 127.0.0.1:8001
python3 - <<'PY'
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = {
            "/health": {"status": "ok"},
            "/v1/models": {"object": "list", "data": [
                {"id": "demo", "object": "model", "owned_by": "me",
                 "created": 1759000000}]},
            "/cache": {"used_tokens": 55000, "n_ctx": 131072},
        }.get(self.path.split("?")[0], {})
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass

ThreadingHTTPServer(("127.0.0.1", 8001), H).serve_forever()
PY

# terminal 2
python3 monitor/serve.py --url http://127.0.0.1:8001 --port 8090
```

Point the endpoint selector at a dead port (or stop the fake upstream) to see the
failure path: red pill, per-card error strips with retry, and the all-failed
banner.

`tests/mock_upstream.py` is that stand-in, kept as a script so it can be used
both by the tests and by hand, with modes for the pathological cases
(`healthy`, `weird`, `free`, `plain`, `empty`, `broken`, `wedged`, `nometrics`
for a server whose `/metrics` answers 404, `huge` for a `/metrics` with thousands
of series, and `unicode` for one whose label values are not ASCII):

```sh
python3 monitor/tests/mock_upstream.py --port 8001 --mode healthy
python3 monitor/serve.py --url http://127.0.0.1:8001 --port 8090
```

## Tests

Two suites, no dependencies to install (stdlib `unittest` + `node --test`):

```sh
python3 -m unittest discover -s monitor/tests -p "test_*.py"   # the proxy
node --test "monitor/tests/*.test.mjs"                         # the dashboard UI
```

| file                      | what it pins down                                                            |
| ------------------------- | ---------------------------------------------------------------------------- |
| `tests/test_serve.py`     | the JSON envelope, `?url=` override + validation, the 5 s cap per upstream request, dead / 5xx / non-JSON upstreams, the Prometheus parser (labels, NaN/Inf, garbage lines, the series cap), static hosting (bodies really get copied), traversal-proofing, and the `/metrics` scrape passthrough (verbatim bytes + Content-Type, `llm_monitor_*` self-metrics, 502 on a failed scrape, `?url=` rejection, the size/series caps, a stalled scrape not blocking the UI polls) |
| `tests/test_static.py`    | contracts no browser would warn about: every id app.js looks up exists, every class it toggles is styled, no off-box asset, upstream data never reaches `innerHTML` |
| `tests/test_app.test.mjs` | the real app.js in a headless DOM against a live `serve.py`: first paint, retargeting without a reload, an unknown `/cache` shape, the metrics tiles/gauge/chart, a server without `/metrics`, the unreachable-upstream path, zero uncaught errors |
| `tests/frontend_dom.mjs`  | the miniature DOM the last file needs (parses `index.html`, runs `app.js` unmodified) |
| `tests/mock_upstream.py`  | the model-server double, usable standalone                                    |

The `node --test` glob is quoted so node expands it itself. On a box without
node, the Python suite still covers the proxy end to end.
