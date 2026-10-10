# monitor — LLM server dashboard

A single-page dashboard for a vLLM / llama.cpp style model server. Three files
matter:

| path                  | what it is                                                  |
| --------------------- | ----------------------------------------------------------- |
| `serve.py`            | stdlib-only same-origin proxy + static file host            |
| `static/index.html`   | the dashboard (with `static/app.js`, `static/styles.css`)   |
| `llm-monitor.service` | systemd **user** unit that keeps `serve.py` running         |

It polls the three read-only endpoints the model server already exposes —
`/health`, `/v1/models` and `/cache` — through a tiny local proxy, so the page
and its API share one origin.

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
| built-in default           | `http://llm.home:8001`                           |

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
* **health card** — `/health`: status text, HTTP code, round-trip latency, last
  check, uptime since the first good poll, plus any other field the server sends.
* **models card** — `/v1/models`: count badge and a table of
  `id / object / owned_by / created` (human-readable date); an empty list says
  so explicitly.
* **cache card** — `/cache`: utilisation as a labelled progress bar with the raw
  numbers, then a key/value grid of everything the endpoint returned, then a raw
  JSON panel.
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

| route             | upstream                | notes                                    |
| ----------------- | ----------------------- | ---------------------------------------- |
| `GET /api/health` | `<upstream>/health`     | proxied                                  |
| `GET /api/models` | `<upstream>/v1/models`  | proxied                                  |
| `GET /api/cache`  | `<upstream>/cache`      | proxied                                  |
| `GET /api/config` | *(no upstream call)*    | active upstream + the endpoints to offer |

Any other `/api/...` path answers 404, a malformed `?url=` answers 400, and
everything that is not under `/api/` is served out of `static/` (directory
listings are disabled). Each proxy route takes an optional `?url=<http(s) base>`
that overrides the upstream for that single request.

## The envelope

All four `/api/*` routes answer with the same shape, and always with HTTP 200
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

`/api/config` is the odd one out: it never calls the upstream, and its `body` is
`{"upstream": ..., "endpoints": ["/health", "/v1/models", "/cache"],
"default_upstream": ...}`, with those three keys also copied onto the top level
of the envelope so `body.upstream` and `upstream` both work.

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
