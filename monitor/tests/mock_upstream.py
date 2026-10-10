#!/usr/bin/env python3
"""A stand-in for a vLLM / llama.cpp style model server, for tests and demos.

The real box (``http://llm.home:8001``) is not part of this repo, so anything
that wants to exercise the dashboard end to end needs a local double that
answers the three monitored routes -- ``/health``, ``/v1/models`` and ``/cache``
-- in the shapes those servers are known to produce, plus a few pathological
shapes (plain text, HTTP 5xx, a wedged route, an unexpected ``/cache`` schema)
so the "never renders blank" rules can be checked.

Start one on its own::

    python3 monitor/tests/mock_upstream.py --port 8101 --mode healthy

Modes:

    healthy     OpenAI-style /v1/models, JSON /health, llama.cpp-shaped /cache
    weird       /cache answers with keys the dashboard has never seen
    free        /cache reports free space instead of used space
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


class MockUpstreamHandler(BaseHTTPRequestHandler):
    """Answers the three monitored routes according to ``server.mode``."""

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
        choices=sorted(set(list(CACHE_BODIES) + ["broken", "plain", "empty", "wedged"])),
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
