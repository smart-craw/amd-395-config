#!/usr/bin/env python3
"""A synthetic Prometheus exposition endpoint with a chosen series count.

    monitoring/flood_target.py --port 9199 --series 50000

Two jobs, both of which need a number nobody can guess from the config:

1. **How much memory does a Prometheus head cost per series?**  The budget in
   ADR 0001 C5 (`mem_limit: 512m`) was written before anything was scraped.
   Point a job at this file's endpoint at 1k / 10k / 100k series, watch
   `prometheus_*` head metrics and the process RSS, and the limit stops being a
   guess -- `monitoring/measure_footprint.sh` reads the RSS/PSS side of it.
2. **Push a container deliberately past its memory limit.**  On the Docker host,
   raising `--series` until `docker stats` shows the prometheus container pinned
   at its `mem_limit` is the overrun proof (`monitoring/verify_limits.sh
   --overrun` drives exactly this and reports the OOM kill).

It is deliberately a *server*, not a backfill: what is being measured is the cost
of a live scrape target, with labels and value movement that look like a real
llama.cpp-style endpoint (histogram buckets dominate real series counts, so
`--histograms` is the knob that models a chattier `/metrics`).

Stdlib only, no dependencies, binds loopback by default. Values move on every
scrape so nothing is optimised away as "unchanged".
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Bucket boundaries of a latency-style histogram, the shape llama.cpp/vLLM expose.
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

_lock = threading.Lock()
_started = time.time()
_scrapes = 0


def _labels(seed: int) -> str:
    """A stable, distinct label set -- hashed so a typo in `--series` cannot make
    two series collide and quietly understate the cardinality."""
    h = hashlib.sha1(str(seed).encode()).hexdigest()
    return 'job="flood", route="r%s", model="m-%s"' % (h[:4], h[4:10])


def render(series: int, histograms: int) -> bytes:
    """Exposition text with exactly `series` series (plus this endpoint's own
    housekeeping series, which are counted out of `series`)."""
    global _scrapes
    with _lock:
        _scrapes += 1
        n = _scrapes
    tick = time.time() - _started
    out = []
    out.append("# HELP flood_up synthetic target health")
    out.append("# TYPE flood_up gauge")
    out.append("flood_up 1")

    budget = max(series - 1, 0)
    emitted = 0
    # Histograms first: one histogram is len(BUCKETS) + 2 series, which is what
    # actually drives series counts on a real inference server.
    per_hist = len(BUCKETS) + 2
    for i in range(histograms):
        if emitted + per_hist > budget:
            break
        lab = _labels(i)
        base = 1000 + i
        out.append("# HELP flood_request_seconds synthetic histogram")
        out.append("# TYPE flood_request_seconds histogram")
        for j, b in enumerate(BUCKETS):
            out.append(
                'flood_request_seconds_bucket{le="%s",%s} %d' % (b, lab, base * (j + 1) + n)
            )
        out.append('flood_request_seconds_sum{%s} %d' % (lab, base * n))
        out.append('flood_request_seconds_count{%s} %d' % (lab, base + n))
        emitted += per_hist

    # Then plain counters/gauges to fill the requested total.
    kind = itertools.cycle(("counter", "gauge"))
    name = itertools.cycle(("flood_tokens_total", "flood_queue_depth"))
    for i in range(budget - emitted):
        k = next(kind)
        nm = next(name)
        if k == "counter":
            out.append("%s{%s} %d" % (nm, _labels(1000 + i), n * (i % 7 + 1)))
        else:
            out.append("%s{%s} %.3f" % (nm, _labels(1000 + i), (tick + i) % 100))
        emitted += 1
    return ("\n".join(out) + "\n").encode()


class Handler(BaseHTTPRequestHandler):
    series = 0
    histograms = 0
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        global _scrapes
        body = render(self.series, self.histograms)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # scrape noise, not signal
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=9199)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--series", type=int, default=1000,
                    help="total series to expose (default 1000)")
    ap.add_argument("--histograms", type=int, default=0,
                    help="how many of them are latency histograms")
    args = ap.parse_args(argv)

    Handler.series = args.series
    Handler.histograms = args.histograms
    preview = render(args.series, args.histograms).decode().splitlines()
    series_lines = [ln for ln in preview if ln and not ln.startswith("#")]
    print("flood_target: %d series on http://%s:%d/metrics (histograms=%d, body=%d KiB)"
          % (len(series_lines), args.host, args.port, args.histograms,
             sum(len(l) + 1 for l in preview) // 1024), flush=True)
    if len(series_lines) != args.series:
        print("note: emitted %d of %d requested series" % (len(series_lines), args.series),
              flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
