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
}

#: Content-Type of a real /metrics response (nginx reports version 0.0.4).
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


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
            self._reply(200, MODELS)
        elif path == "/cache":
            self._reply(200, CACHE_BODIES.get(mode, CACHE_BODIES["healthy"]))
        elif path == "/metrics":
            if mode == "nometrics":
                # A server that simply does not have the route (vLLM, old llama.cpp).
                self._reply(404, {"error": "this server does not expose /metrics"})
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
            | {"broken", "plain", "empty", "wedged", "nometrics"}
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
