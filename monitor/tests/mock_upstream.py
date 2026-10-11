#!/usr/bin/env python3
"""A stand-in for a vLLM / llama.cpp style model server, for tests and demos.

The real box (``http://llm.home:8081``) is not part of this repo, so anything
that wants to exercise the dashboard end to end needs a local double that
answers the four monitored routes -- ``/health``, ``/v1/models``, ``/cache`` and
``/metrics`` -- in the shapes those servers are known to produce, plus a few
pathological shapes (plain text, HTTP 5xx, a wedged route, an unexpected
``/cache`` schema, a ``/metrics`` the server does not serve) so the "never renders
blank" rules can be checked.

Start one on its own::

    python3 monitor/tests/mock_upstream.py --port 8101 --mode healthy

Modes:

    healthy     OpenAI-style /v1/models, JSON /health, llama.cpp-shaped /cache,
                llama.cpp/halogen-shaped /metrics (Prometheus text)
    weird       /cache answers with keys the dashboard has never seen, /metrics
                with labels, buckets, NaN/Inf and one unparsable line
    free        /cache reports free space instead of used space
    nometrics   everything is healthy except /metrics, which 404s (a server that
                does not expose metrics at all)
    huge        /metrics answers thousands of series (to exercise the series and
                body-size caps of the scrape passthrough)
    unicode     /metrics as healthy, plus non-ASCII label values
    fanout      /metrics where each metric name carries two `model` label sets
    nan         the healthy metric names, with NaN / +Inf as their values
    reset       counters that reset on a schedule, so "rate() survives a reset" is
                something a test watches happen instead of something it asserts
    empty       every route answers 204 with no body
    plain       every route answers text/plain (a stock llama.cpp /health)
    broken      every route answers 500
    wedged      every route sleeps forever (to prove the proxy's timeout cap)

Used as a library by ``test_serve.py``::

    with MockUpstream(mode="weird") as upstream:
        ... upstream.url ...
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = {
    "object": "list",
    "data": [
        {
            "id": "qwen3-27b",
            "object": "model",
            "owned_by": "local",
            "created": 1730000000,
        },
        {
            "id": "gemma4-26b",
            "object": "model",
            "owned_by": "local",
            "created": 1720000000,
        },
    ],
}

#: One model only, and an id that needs percent-encoding in a URL (``/`` and ``:``).
#: The Grafana deep links pass `var-model` only when exactly one model is served, so
#: both halves of that rule need a server that serves exactly one.
MODELS_SINGLE = {
    "object": "list",
    "data": [
        {
            "id": "models/Qwen3-27B-Q4_K_M.gguf:latest",
            "object": "model",
            "owned_by": "local",
            "created": 1730000000,
        }
    ],
}

#: Shapes the dashboard must cope with, keyed by mode.
CACHE_BODIES = {
    "healthy": {
        "n_ctx": 8192,
        "tokens": 5123,
        "n_pred": 128,
        "hit_rate": 0.875,
    },
    # /cache is not a stock vLLM or llama.cpp route: nobody can promise this is
    # not what it answers with.
    "weird": {
        "usage_percent": 62.5,
        "slots": [{"id": 0, "busy": True}, {"id": 1, "busy": False}],
        "nested": {"deep": {"value": 7, "name": "hello"}},
        "note": None,
        "flag": True,
    },
    "free": {"n_ctx": 4096, "free_tokens": 1024},
}

#: /metrics is Prometheus text exposition format -- ``text/plain``, not JSON.
#: The healthy body mirrors what a llama.cpp + halogen front door actually
#: answers (same metric names, plausible numbers for CACHE_BODIES["healthy"]).
METRICS_BODIES = {
    "healthy": """# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed.
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total 779554
# HELP llamacpp:prompt_seconds_total Prompt process time.
# TYPE llamacpp:prompt_seconds_total counter
llamacpp:prompt_seconds_total 638.037
# HELP llamacpp:tokens_predicted_total Number of generation tokens processed.
# TYPE llamacpp:tokens_predicted_total counter
llamacpp:tokens_predicted_total 404669
# HELP llamacpp:tokens_predicted_seconds_total Predict process time.
# TYPE llamacpp:tokens_predicted_seconds_total counter
llamacpp:tokens_predicted_seconds_total 6607.03
# HELP llamacpp:prompt_tokens_seconds Average prompt throughput in tokens/s.
# TYPE llamacpp:prompt_tokens_seconds gauge
llamacpp:prompt_tokens_seconds 1415.51
# HELP llamacpp:predicted_tokens_seconds Average generation throughput in tokens/s.
# TYPE llamacpp:predicted_tokens_seconds gauge
llamacpp:predicted_tokens_seconds 72.9496
# HELP llamacpp:requests_processing Number of requests processing.
# TYPE llamacpp:requests_processing gauge
llamacpp:requests_processing 2
# HELP llamacpp:requests_deferred Number of requests deferred.
# TYPE llamacpp:requests_deferred gauge
llamacpp:requests_deferred 0
# HELP llamacpp:kv_cache_tokens KV-cache tokens.
# TYPE llamacpp:kv_cache_tokens gauge
llamacpp:kv_cache_tokens 5120
# HELP llamacpp:kv_cache_usage_ratio KV-cache usage. 1 means 100 percent usage.
# TYPE llamacpp:kv_cache_usage_ratio gauge
llamacpp:kv_cache_usage_ratio 0.625
# HELP halogen:requests_total Requests completed.
# TYPE halogen:requests_total counter
halogen:requests_total 522
# HELP halogen:structured_requests_total Requests decoded under a JSON schema.
# TYPE halogen:structured_requests_total counter
halogen:structured_requests_total 0
# HELP halogen:prompt_tokens_cached_total Prompt tokens the prompt cache covered.
# TYPE halogen:prompt_tokens_cached_total counter
halogen:prompt_tokens_cached_total 49838237
# HELP halogen:draft_tokens_accepted_total Of the drafted tokens, accepted.
# TYPE halogen:draft_tokens_accepted_total counter
halogen:draft_tokens_accepted_total 248795
# HELP halogen:draft_tokens_total Tokens proposed by the draft head.
# TYPE halogen:draft_tokens_total counter
halogen:draft_tokens_total 305696
# HELP halogen:kv_pool_positions The KV pool, in positions.
# TYPE halogen:kv_pool_positions gauge
halogen:kv_pool_positions 8192
# HELP halogen:kv_pool_reserved_tokens Positions the front-end slots asked for.
# TYPE halogen:kv_pool_reserved_tokens gauge
halogen:kv_pool_reserved_tokens 0
""",
    # Shapes the dashboard must survive: labels, buckets, non-finite values,
    # a metric with no HELP/TYPE, and a line that is not a sample at all.
    "weird": """# HELP some:histogram a histogram with labels
# TYPE some:histogram histogram
some:histogram_bucket{model="qwen3, 27b",le="0.5"} 12
some:histogram_bucket{model="qwen3, 27b",le="+Inf"} 20
some:histogram_count{model="qwen3, 27b"} 20
some:histogram_sum{model="qwen3, 27b"} 7.5
# a plain comment, and an OpenMetrics one
# UNIT some:other seconds
some:unknown_no_type 3
some:not_a_number nope
some:nan NaN
some:plus_inf +Inf
totally unparsable here
""",
    # One metric name, several label sets: two models in one server. This is the
    # shape that makes "sum the family" a decision rather than a no-op -- the page
    # adds the label sets together, a panel filtered by $model keeps them apart
    # (see monitoring/PARITY.md, delta class `label-fanout`).
    "fanout": """# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed.
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total{model="a"} 1000
llamacpp:prompt_tokens_total{model="b"} 3000
# HELP halogen:prompt_tokens_cached_total Prompt tokens the prompt cache covered.
# TYPE halogen:prompt_tokens_cached_total counter
halogen:prompt_tokens_cached_total{model="a"} 9000
halogen:prompt_tokens_cached_total{model="b"} 27000
# HELP halogen:draft_tokens_total Tokens proposed by the draft head.
# TYPE halogen:draft_tokens_total counter
halogen:draft_tokens_total{model="a"} 100
halogen:draft_tokens_total{model="b"} 300
# HELP halogen:draft_tokens_accepted_total Of the drafted tokens, accepted.
# TYPE halogen:draft_tokens_accepted_total counter
halogen:draft_tokens_accepted_total{model="a"} 60
halogen:draft_tokens_accepted_total{model="b"} 180
# HELP llamacpp:tokens_predicted_total Number of generation tokens processed.
# TYPE llamacpp:tokens_predicted_total counter
llamacpp:tokens_predicted_total{model="a"} 500
llamacpp:tokens_predicted_total{model="b"} 1500
# HELP llamacpp:tokens_predicted_seconds_total Predict process time.
# TYPE llamacpp:tokens_predicted_seconds_total counter
llamacpp:tokens_predicted_seconds_total{model="a"} 10
llamacpp:tokens_predicted_seconds_total{model="b"} 30
# HELP llamacpp:kv_cache_usage_ratio KV-cache usage.
# TYPE llamacpp:kv_cache_usage_ratio gauge
llamacpp:kv_cache_usage_ratio{model="a"} 0.2
llamacpp:kv_cache_usage_ratio{model="b"} 0.4
# HELP llamacpp:requests_processing Number of requests processing.
# TYPE llamacpp:requests_processing gauge
llamacpp:requests_processing{model="a"} 1
llamacpp:requests_processing{model="b"} 3
""",
}

def huge_metrics_body(series: int = 2600) -> str:
    """A /metrics body with more series than ``serve.MAX_SERIES`` and, at
    default sizes, more bytes than a test wants to patch down to."""
    lines = [
        '# HELP huge:generated_series_total A synthetic per-something counter.',
        '# TYPE huge:generated_series_total counter',
    ]
    lines += [
        'huge:generated_series_total{index="%d",model="qwen3-27b"} %d' % (i, i * 3)
        for i in range(series)
    ]
    return "\n".join(lines) + "\n"


#: Content-Type of a real /metrics response (nginx reports version 0.0.4).
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Same as "healthy" plus label values that are not ASCII. Legal exposition text
# (label *values* are free-form; metric names are not), and the case where a
# passthrough that decodes and re-encodes instead of copying bytes shows up:
# everything non-ASCII silently becomes U+FFFD.
#: Two more shapes, both scraped for real by the parity harness
#: (monitoring/parity) so the delta classes a healthy single-model server cannot
#: show are measured instead of asserted:
#:
#:   fanout  one metric name carrying two `model` label sets -- where "sum the
#:           family" is a decision rather than a no-op;
#:   nan     the healthy names with NaN / +Inf as their values -- legal in the
#:           exposition format, impossible in JSON, so the page gets
#:           `value: null` + `value_text` and renders an em-dash while PromQL keeps
#:           NaN and Grafana renders No data.
METRICS_BODIES["fanout"] = """# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed.
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total{model="a"} 1000
llamacpp:prompt_tokens_total{model="b"} 3000
# HELP halogen:prompt_tokens_cached_total Prompt tokens the prompt cache covered.
# TYPE halogen:prompt_tokens_cached_total counter
halogen:prompt_tokens_cached_total{model="a"} 9000
halogen:prompt_tokens_cached_total{model="b"} 27000
# HELP halogen:draft_tokens_total Tokens proposed by the draft head.
# TYPE halogen:draft_tokens_total counter
halogen:draft_tokens_total{model="a"} 100
halogen:draft_tokens_total{model="b"} 300
# HELP halogen:draft_tokens_accepted_total Of the drafted tokens, accepted.
# TYPE halogen:draft_tokens_accepted_total counter
halogen:draft_tokens_accepted_total{model="a"} 60
halogen:draft_tokens_accepted_total{model="b"} 180
# HELP llamacpp:tokens_predicted_total Number of generation tokens processed.
# TYPE llamacpp:tokens_predicted_total counter
llamacpp:tokens_predicted_total{model="a"} 500
llamacpp:tokens_predicted_total{model="b"} 1500
# HELP llamacpp:tokens_predicted_seconds_total Predict process time.
# TYPE llamacpp:tokens_predicted_seconds_total counter
llamacpp:tokens_predicted_seconds_total{model="a"} 10
llamacpp:tokens_predicted_seconds_total{model="b"} 30
# HELP llamacpp:kv_cache_usage_ratio KV-cache usage. 1 means 100 percent usage.
# TYPE llamacpp:kv_cache_usage_ratio gauge
llamacpp:kv_cache_usage_ratio{model="a"} 0.2
llamacpp:kv_cache_usage_ratio{model="b"} 0.4
# HELP llamacpp:requests_processing Number of requests processing.
# TYPE llamacpp:requests_processing gauge
llamacpp:requests_processing{model="a"} 1
llamacpp:requests_processing{model="b"} 3
# HELP llamacpp:predicted_tokens_seconds Average generation throughput in tokens/s.
# TYPE llamacpp:predicted_tokens_seconds gauge
llamacpp:predicted_tokens_seconds{model="a"} 40
llamacpp:predicted_tokens_seconds{model="b"} 60
# HELP llamacpp:prompt_tokens_seconds Average prompt throughput in tokens/s.
# TYPE llamacpp:prompt_tokens_seconds gauge
llamacpp:prompt_tokens_seconds{model="a"} 900
llamacpp:prompt_tokens_seconds{model="b"} 1100
# HELP halogen:requests_total Requests completed.
# TYPE halogen:requests_total counter
halogen:requests_total{model="a"} 100
halogen:requests_total{model="b"} 400
"""

RESET_EPOCH = time.monotonic()   # the fixture's own "since the process started"


def reset_counters_body(now: float) -> str:
    """A server whose counters *reset*, updated live: the counter-reset delta class.

    A lifetime division is only as good as the assumption that both counters have
    been counting since the same instant. `draft_tokens_accepted_total` here restarts
    every 45 s (what a drafter restart, a metrics-subsystem restart or a plain process
    restart does to one counter but not the other) while `draft_tokens_total` keeps
    climbing, so the truth -- 80% acceptance, and a prompt cache that covers 90% of
    what arrives -- is constant while the ratio-of-lifetimes figure decays toward
    zero and stays there.

    ``rate()`` puts the dropped back: over any window that contains a reset it counts
    the pre-reset tokens too, which is the whole reason it is the honest operator.
    Used by ``monitoring/parity`` (probe P2b); the values move with wall-clock time so
    a scrape interval is enough to see the sawtooth.
    """
    cycle = 45.0
    phase = now % cycle
    drafted = 100.0 * now
    accepted = 80.0 * phase
    processed = 10.0 * now
    cached = 90.0 * phase
    return f"""# HELP halogen:draft_tokens_total Tokens proposed by the draft head.
# TYPE halogen:draft_tokens_total counter
halogen:draft_tokens_total {drafted:.0f}
# HELP halogen:draft_tokens_accepted_total Of those, accepted.
# TYPE halogen:draft_tokens_accepted_total counter
halogen:draft_tokens_accepted_total {accepted:.0f}
# HELP llamacpp:prompt_tokens_total Prompt tokens that missed the cache.
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total {processed:.0f}
# HELP halogen:prompt_tokens_cached_total Prompt tokens the cache covered.
# TYPE halogen:prompt_tokens_cached_total counter
halogen:prompt_tokens_cached_total {cached:.0f}
# HELP llamacpp:tokens_predicted_total Generated tokens.
# TYPE llamacpp:tokens_predicted_total counter
llamacpp:tokens_predicted_total {50.0 * now:.0f}
# HELP llamacpp:tokens_predicted_seconds_total Predict process time.
# TYPE llamacpp:tokens_predicted_seconds_total counter
llamacpp:tokens_predicted_seconds_total {now:.1f}
# HELP llamacpp:predicted_tokens_seconds Average generation throughput in tokens/s.
# TYPE llamacpp:predicted_tokens_seconds gauge
llamacpp:predicted_tokens_seconds 50
# HELP llamacpp:requests_processing Number of requests processing.
# TYPE llamacpp:requests_processing gauge
llamacpp:requests_processing 1
# HELP llamacpp:kv_cache_usage_ratio KV-cache usage.
# TYPE llamacpp:kv_cache_usage_ratio gauge
llamacpp:kv_cache_usage_ratio 0.5
# HELP halogen:requests_total Requests completed.
# TYPE halogen:requests_total counter
halogen:requests_total {2.0 * now:.0f}
"""


METRICS_BODIES["nan"] = """# HELP llamacpp:predicted_tokens_seconds Average generation throughput in tokens/s.
# TYPE llamacpp:predicted_tokens_seconds gauge
llamacpp:predicted_tokens_seconds NaN
# HELP llamacpp:prompt_tokens_seconds Average prompt throughput in tokens/s.
# TYPE llamacpp:prompt_tokens_seconds gauge
llamacpp:prompt_tokens_seconds +Inf
# HELP llamacpp:requests_processing Number of requests processing.
# TYPE llamacpp:requests_processing gauge
llamacpp:requests_processing NaN
# HELP llamacpp:kv_cache_usage_ratio KV-cache usage. 1 means 100 percent usage.
# TYPE llamacpp:kv_cache_usage_ratio gauge
llamacpp:kv_cache_usage_ratio NaN
# HELP halogen:requests_total Requests completed.
# TYPE halogen:requests_total counter
halogen:requests_total NaN
# HELP halogen:draft_tokens_total Tokens proposed by the draft head.
# TYPE halogen:draft_tokens_total counter
halogen:draft_tokens_total 100
# HELP halogen:draft_tokens_accepted_total Of those, accepted.
# TYPE halogen:draft_tokens_accepted_total counter
halogen:draft_tokens_accepted_total NaN
# HELP llamacpp:tokens_predicted_total Number of generation tokens processed.
# TYPE llamacpp:tokens_predicted_total counter
llamacpp:tokens_predicted_total NaN
# HELP llamacpp:tokens_predicted_seconds_total Predict process time.
# TYPE llamacpp:tokens_predicted_seconds_total counter
llamacpp:tokens_predicted_seconds_total NaN
# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed.
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total 100
# HELP halogen:prompt_tokens_cached_total Prompt tokens the prompt cache covered.
# TYPE halogen:prompt_tokens_cached_total counter
halogen:prompt_tokens_cached_total NaN
"""

METRICS_BODIES["unicode"] = METRICS_BODIES["healthy"] + "\n".join(
    [
        "# HELP llamacpp:request_prompt_chars Prompt characters, by language.",
        "# TYPE llamacpp:request_prompt_chars counter",
        'llamacpp:request_prompt_chars{lang="日本語",note="naïve ✓"} 42',
        'llamacpp:request_prompt_chars{lang="español"} 7',
    ]
) + "\n"


class MockUpstreamHandler(BaseHTTPRequestHandler):
    """Answers the four monitored routes according to ``server.mode``."""

    protocol_version = "HTTP/1.1"
    server_version = "mock-llm/1.0"

    def log_message(self, fmt, *args):  # keep pytest output clean
        pass

    # -- helpers ------------------------------------------------------------

    def _reply(self, status: int, body, content_type: str = "application/json") -> None:
        if body is None:
            raw = b""
        elif isinstance(body, (bytes, bytearray)):
            raw = bytes(body)
        elif isinstance(body, str):
            raw = body.encode("utf-8")
        else:
            raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command == "GET":
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                # Expected in the "wedged" mode: the proxy gives up at its
                # timeout and closes, and this server answers 30 s later.
                self.close_connection = True

    @property
    def mode(self) -> str:
        return getattr(self.server, "mode", "healthy")

    # -- routes -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        mode = self.mode

        if mode == "wedged":
            time.sleep(getattr(self.server, "wedge_seconds", 30))
            self._reply(200, {"never": "meant to get here"})
            return

        if mode == "broken":
            self._reply(500, {"error": "model server on fire"})
            return

        if mode == "empty":
            self._reply(204, None)
            return

        if mode == "plain":
            self._reply(200, "keep-alive\n", "text/plain; charset=utf-8")
            return

        if path == "/health":
            self._reply(200, {"status": "ok", "load": 0.34, "load_avg": 0.21})
        elif path == "/v1/models":
            self._reply(200, MODELS_SINGLE if mode == "onemodel" else MODELS)
        elif path == "/cache":
            self._reply(200, CACHE_BODIES.get(mode, CACHE_BODIES["healthy"]))
        elif path == "/metrics":
            if mode == "nometrics":
                # A server that simply does not have the route (vLLM, old llama.cpp).
                self._reply(404, {"error": "this server does not expose /metrics"})
                return
            if mode == "reset":
                # Time-varying so a parity run can watch a counter reset happen.
                # Ageing from when the mock started, not from boot: the point of the
                # fixture is a *short* lifetime whose ratio has decayed.
                self._reply(200, reset_counters_body(time.monotonic() - RESET_EPOCH),
                            METRICS_CONTENT_TYPE)
                return
            if mode == "huge":
                self._reply(
                    200,
                    huge_metrics_body(getattr(self.server, "huge_series", 2600)),
                    METRICS_CONTENT_TYPE,
                )
                return
            self._reply(
                200,
                METRICS_BODIES.get(mode, METRICS_BODIES["healthy"]),
                METRICS_CONTENT_TYPE,
            )
        elif path == "/slow":
            time.sleep(float((self.path.split("=", 1) + ["5"])[1]))
            self._reply(200, {"slept": True})
        else:
            self._reply(404, {"error": f"mock has no route {path}"})


class MockUpstream(ThreadingHTTPServer):
    """A mock model server bound to an ephemeral loopback port, in a thread."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, mode: str = "healthy", host: str = "127.0.0.1", port: int = 0):
        super().__init__((host, port), MockUpstreamHandler)
        self.mode = mode
        self.wedge_seconds = 30
        self.thread = threading.Thread(
            target=self.serve_forever, name=f"mock-llm-{mode}", daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        return f"http://{host}:{port}"

    def __enter__(self) -> "MockUpstream":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()
        self.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument(
        "--mode",
        default="healthy",
        choices=sorted(
            set(list(CACHE_BODIES) + list(METRICS_BODIES))
            | {"broken", "plain", "empty", "wedged", "nometrics", "huge", "onemodel",
               "reset"}
        ),
    )
    args = parser.parse_args()
    server = MockUpstream(mode=args.mode, port=args.port)
    print(f"mock llm server ({args.mode}) on {server.url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
