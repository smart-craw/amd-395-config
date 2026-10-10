# monitor — LLM server dashboard

A single-page dashboard for a vLLM / llama.cpp style model server. Two files
matter:

| path                | what it is                                                        |
| ------------------- | ----------------------------------------------------------------- |
| `serve.py`          | stdlib-only same-origin proxy + static file host                  |
| `static/index.html` | the dashboard (with `static/app.js`, `static/styles.css`)         |

```sh
python3 monitor/serve.py --url http://llm.home:8001 --port 8090
# then open http://localhost:8090/
```

`--url` (or `$LLM_MONITOR_UPSTREAM`) is the upstream the proxy polls; `static/`
is served from the same origin, so no CORS is involved and nothing is loaded
from outside the box. No build step, no npm, no CDN: plain HTML/CSS/JS, charts
drawn as inline `<svg>`.

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

## The envelope

`serve.py` answers `GET /api/{health,models,cache,config}` with a fixed shape,
and always with HTTP 200 even when the upstream is on fire (upstream trouble
lives inside the envelope, not in the transport status):

```json
{"ok": true, "url": "http://llm.home:8001/health", "status": 200,
 "latency_ms": 4.2, "content_type": "application/json",
 "body": {"status": "ok"}, "error": null}
```

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
