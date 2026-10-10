#!/usr/bin/env python3
"""Backend for the LLM monitor dashboard.

Serves the static UI out of ``monitor/static/`` and, on the *same origin*,
proxies a handful of read-only GETs to the model server:

    GET /api/health  -> <upstream>/health
    GET /api/models  -> <upstream>/v1/models
    GET /api/cache   -> <upstream>/cache
    GET /api/config  -> active upstream + defaults (no upstream call)

The proxy exists because the model server (llama.cpp / vLLM style) does not send
CORS headers, so a page served from another origin could not poll it directly.
UI + API from one origin means no CORS problem at all.

Every proxy response uses the same envelope so the UI never has to guess:

    {"ok": bool, "url": "<absolute upstream url>", "status": int|null,
     "latency_ms": float, "content_type": str, "body": <json|raw text>,
     "error": str|null}

A dead, slow or angry upstream is still HTTP 200 from the proxy with ``ok:false``
and a populated ``error`` -- never a 500 that takes the dashboard down, and never
a hang: every upstream request is capped at 5 seconds wall-clock, whatever the
upstream is doing (stalled, half-open, or dribbling bytes out forever).

Each proxy route additionally accepts ``?url=<http(s) base>`` so the dashboard can
re-point the endpoint selector live; anything that is not an absolute http(s) URL
is refused with 400. That override lets the caller make the server fetch an
arbitrary URL, which is exactly why the default bind address is loopback -- only
pass ``--host 0.0.0.0`` on a network you trust the requesters on.

Standard library only. No build step, no ``pip install``, no ``npm install``.

Usage:
    python3 monitor/serve.py --url http://llm.home:8001 --port 8090
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

# --- configuration -----------------------------------------------------------

DEFAULT_UPSTREAM = "http://llm.home:8001"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090

#: Environment fallback, mirroring the env-configurable-upstream pattern used by
#: BACKEND_SERVICE in qwen3.8-flash/docker-compose.yml.
ENV_UPSTREAM = "LLM_MONITOR_UPSTREAM"

#: Hard cap per upstream request (connect + status line + body). The dashboard
#: must never sit waiting on a wedged model server.
UPSTREAM_TIMEOUT_SECONDS = 5.0

#: Chunk size used while draining an upstream body so the deadline above can be
#: re-checked between reads (a slow-drip server cannot hold a request open).
READ_CHUNK_BYTES = 64 * 1024

#: Refuse to buffer more than this per upstream response (the monitored endpoints
#: are tiny; this is only a memory-flood guard).
MAX_BODY_BYTES = 4 * 1024 * 1024

ALLOWED_SCHEMES = ("http", "https")

USER_AGENT = "llm-monitor/1.0 (monitor/serve.py)"

#: proxy route -> upstream path
PROXY_ROUTES = {
    "/api/health": "/health",
    "/api/models": "/v1/models",
    "/api/cache": "/cache",
}

#: The upstream-side endpoints the UI should offer in its selector.
PUBLIC_ENDPOINTS = ["/health", "/v1/models", "/cache"]

STATIC_DIR = Path(__file__).resolve().parent / "static"


class UpstreamError(ValueError):
    """Raised for a malformed / disallowed upstream URL."""


def normalize_upstream(raw: str) -> str:
    """Validate and canonicalize an upstream base URL.

    Only absolute http(s) URLs with a host are accepted; trailing slashes are
    removed so joining with an endpoint path cannot produce ``//``. Anything else
    (``file:///etc/passwd``, ``gopher://x``, a bare host, an empty string) raises
    :class:`UpstreamError`, which callers turn into a 400.
    """
    if raw is None:
        raise UpstreamError("empty upstream URL")
    candidate = raw.strip()
    if not candidate:
        raise UpstreamError("empty upstream URL")

    parts = urlparse.urlsplit(candidate)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UpstreamError(
            f"scheme {parts.scheme!r} is not allowed (use http:// or https://)"
        )
    if not parts.netloc:
        raise UpstreamError("URL has no host")

    return urlparse.urlunsplit((scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def target_url(upstream: str, endpoint_path: str) -> str:
    """Absolute upstream URL for one monitored endpoint."""
    return f"{normalize_upstream(upstream)}{endpoint_path}"


def describe_bytes(raw: bytes, content_type: str) -> object:
    """Parse the upstream body as JSON when possible, else return it as text."""
    text = raw.decode("utf-8", errors="replace")
    looks_json = "json" in content_type.lower() or text[:1] in ("{", "[")
    if not looks_json:
        # Plain-text /healthz, HTML error page, empty body, ...
        return text
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        # Claimed to be JSON but was not (truncated body, log line, ...).
        return text


def read_body(response, *, deadline: float, timeout: float) -> bytes:
    """Drain ``response`` honouring ``deadline`` and :data:`MAX_BODY_BYTES`.

    ``urlopen(timeout=...)`` bounds every *single* socket operation, not the
    request as a whole, so a server that trickles a byte every few seconds could
    keep a request open forever. Reading in chunks and re-checking the wall-clock
    deadline between reads keeps the per-request cap honest.
    """
    # read1() returns as soon as *any* data is available; read(n) happily blocks
    # until it has n bytes (or the stream ends), which for a chunked body that
    # never stops would never return.
    read = getattr(response, "read1", None) or response.read
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"body not received within {timeout:g}s")
        chunk = read(READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total >= MAX_BODY_BYTES:
            return b"".join(chunks)[:MAX_BODY_BYTES]


def envelope(
    *,
    ok: bool,
    url: str | None,
    status: int | None,
    latency_ms: float,
    content_type: str,
    body: object,
    error: str | None,
) -> dict:
    """Fixed-shape response wrapper. Key order is stable on purpose."""
    return {
        "ok": ok,
        "url": url,
        "status": status,
        "latency_ms": round(float(latency_ms), 2),
        "content_type": content_type,
        "body": body,
        "error": error,
    }


def fetch_envelope(
    url: str, *, deadline: float, timeout: float = UPSTREAM_TIMEOUT_SECONDS
) -> dict:
    """Actually perform the upstream GET and wrap the outcome in an envelope.

    Failures are reported *inside* the envelope (``ok:false`` + ``error``) so the
    caller can answer 200 and keep the dashboard alive.
    """
    started = time.monotonic()

    def elapsed_ms() -> float:
        return (time.monotonic() - started) * 1000.0

    req = urlrequest.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json, */*",
            "User-Agent": USER_AGENT,
            "Connection": "close",
        },
    )

    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            status = int(resp.status)
            content_type = resp.headers.get("Content-Type") or ""
            raw = read_body(resp, deadline=deadline, timeout=timeout)
        error = None if 200 <= status < 400 else f"upstream returned HTTP {status}"
        return envelope(
            ok=(200 <= status < 400),
            url=url,
            status=status,
            latency_ms=elapsed_ms(),
            content_type=content_type,
            body=describe_bytes(raw, content_type),
            error=error,
        )
    except urlerror.HTTPError as exc:  # subclass of URLError: must come first
        try:
            raw = read_body(exc, deadline=deadline, timeout=timeout)
        except Exception:  # noqa: BLE001 - body is best-effort
            raw = b""
        content_type = exc.headers.get("Content-Type") if exc.headers else ""
        return envelope(
            ok=False,
            url=url,
            status=exc.code,
            latency_ms=elapsed_ms(),
            content_type=content_type or "",
            body=describe_bytes(raw, content_type or ""),
            error=f"upstream returned HTTP {exc.code} {exc.reason}",
        )
    except (TimeoutError, urlerror.URLError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(exc, TimeoutError) or "timed out" in str(reason).lower():
            message = f"upstream timed out after {timeout:g}s"
        else:
            message = f"upstream unreachable: {reason}"
        return envelope(
            ok=False,
            url=url,
            status=None,
            latency_ms=elapsed_ms(),
            content_type="application/json",
            body=None,
            error=message,
        )
    except Exception as exc:  # noqa: BLE001 - the dashboard always survives
        return envelope(
            ok=False,
            url=url,
            status=None,
            latency_ms=elapsed_ms(),
            content_type="application/json",
            body=None,
            error=f"{type(exc).__name__}: {exc}",
        )


def proxy_get(url: str, *, timeout: float = UPSTREAM_TIMEOUT_SECONDS) -> dict:
    """GET ``url``, returning an envelope and never taking longer than ``timeout``.

    :func:`fetch_envelope` bounds connect, headers and body against a deadline,
    but a pathological upstream (one that dribbles out a byte every few seconds,
    say) can still keep a single blocking socket call busy. Running the fetch in
    a short-lived daemon thread and joining with a hard timeout guarantees the
    dashboard gets an answer -- always ``200`` with an ``ok:false`` envelope --
    within ``timeout`` seconds, whatever the upstream is doing. The orphaned
    thread is daemonised and its own socket timeouts make it die on its own.
    """
    started = time.monotonic()
    deadline = started + timeout
    outcome: dict[str, dict] = {}

    def worker() -> None:
        try:
            outcome["result"] = fetch_envelope(url, deadline=deadline, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - the dashboard always survives
            outcome["result"] = envelope(
                ok=False,
                url=url,
                status=None,
                latency_ms=(time.monotonic() - started) * 1000.0,
                content_type="application/json",
                body=None,
                error=f"{type(exc).__name__}: {exc}",
            )

    thread = threading.Thread(target=worker, name="llm-monitor-upstream", daemon=True)
    thread.start()
    thread.join(timeout)
    if "result" in outcome:
        return outcome["result"]

    return envelope(
        ok=False,
        url=url,
        status=None,
        latency_ms=(time.monotonic() - started) * 1000.0,
        content_type="application/json",
        body=None,
        error=f"upstream timed out after {timeout:g}s",
    )


def config_payload(upstream: str) -> dict:
    """Body of /api/config: lets the UI render the config field unhardcoded."""
    return {
        "upstream": upstream,
        "endpoints": list(PUBLIC_ENDPOINTS),
        "default_upstream": DEFAULT_UPSTREAM,
    }


class MonitorHandler(SimpleHTTPRequestHandler):
    """Static file host + same-origin JSON proxy."""

    # Keep-alive keeps browser polling cheap; every response sets Content-Length.
    protocol_version = "HTTP/1.1"
    server_version = "llm-monitor/1.0"

    # Whether a response line has been written for this request. Set by the
    # send_response_only() hook below; a fresh handler instance is created per
    # connection, so the class-level default is enough. (Do not use
    # ``self.headers_sent``: it does not exist on every supported CPython.)
    _response_started = False

    def send_response_only(self, code, message=None) -> None:  # noqa: ANN001, D102
        self._response_started = True
        super().send_response_only(code, message)

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("directory", str(STATIC_DIR))
        super().__init__(*args, **kwargs)

    # -- routing -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        self._route(include_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 (http.server API)
        self._route(include_body=False)

    def _route(self, *, include_body: bool) -> None:
        parts = urlparse.urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        try:
            if path == "/api/config":
                self._send_config(include_body)
            elif path in PROXY_ROUTES:
                self._send_proxy(path, parts.query, include_body)
            elif path.startswith("/api") or path.startswith("/api/"):
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    envelope(
                        ok=False,
                        url=None,
                        status=int(HTTPStatus.NOT_FOUND),
                        latency_ms=0.0,
                        content_type="application/json",
                        body=None,
                        error=f"unknown api route: {path}",
                    ),
                    include_body,
                )
            else:
                self._serve_static()
        except BrokenPipeError:
            pass  # client went away mid-response; nothing useful to do
        except Exception as exc:  # noqa: BLE001 - never kill the worker thread
            if not self._response_started:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    envelope(
                        ok=False,
                        url=None,
                        status=int(HTTPStatus.INTERNAL_SERVER_ERROR),
                        latency_ms=0.0,
                        content_type="application/json",
                        body=None,
                        error=f"{type(exc).__name__}: {exc}",
                    ),
                    include_body,
                )
            else:
                self.close_connection = True

    # -- api -----------------------------------------------------------------

    @property
    def upstream(self) -> str:
        return getattr(self.server, "monitor_upstream", DEFAULT_UPSTREAM)

    def _send_config(self, include_body: bool) -> None:
        config = config_payload(self.upstream)
        payload = envelope(
            ok=True,
            url=None,
            status=None,
            latency_ms=0.0,
            content_type="application/json",
            body=config,
            error=None,
        )
        # Convenience: the documented config keys are also top-level, so both
        # `body.upstream` and `upstream` work for the UI.
        payload.update(config)
        self._send_json(HTTPStatus.OK, payload, include_body)

    def _send_proxy(self, route: str, query: str, include_body: bool) -> None:
        params = urlparse.parse_qs(query, keep_blank_values=True)
        override = params.get("url", [""])[0]
        try:
            upstream = normalize_upstream(override) if override.strip() else self.upstream
        except UpstreamError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                envelope(
                    ok=False,
                    url=override,
                    status=int(HTTPStatus.BAD_REQUEST),
                    latency_ms=0.0,
                    content_type="application/json",
                    body=None,
                    error=f"invalid ?url= override: {exc}",
                ),
                include_body,
            )
            return

        result = proxy_get(target_url(upstream, PROXY_ROUTES[route]))
        # The proxy itself always answers 200: upstream trouble belongs in the
        # envelope (ok:false / status / error), not in the transport status.
        self._send_json(HTTPStatus.OK, result, include_body)

    def _send_json(self, status: HTTPStatus, payload: dict, include_body: bool) -> None:
        data = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if include_body:
            self.wfile.write(data)

    # -- static --------------------------------------------------------------

    def _serve_static(self) -> None:
        if not STATIC_DIR.is_dir():
            self.send_error(
                HTTPStatus.NOT_FOUND,
                "No static files (monitor/static/ does not exist yet)",
            )
            return
        # send_head() serves the file, sets Content-Type/Length, and returns
        # without a body for HEAD requests.
        fobj = self.send_head()
        if fobj is not None:
            fobj.close()

    def list_directory(self, path):  # noqa: ANN201 - directory listings off
        """Never expose a directory listing; 404 instead."""
        self.send_error(HTTPStatus.NOT_FOUND, "Directory listing is disabled")
        return None


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True  # a stuck request cannot block shutdown
    allow_reuse_address = True
    monitor_upstream = DEFAULT_UPSTREAM


def resolve_upstream(cli_url: str | None) -> tuple[str, str]:
    """CLI flag > env var > default. Returns (upstream, source_label)."""
    if cli_url:
        raw, source = cli_url, "--url"
    elif os.environ.get(ENV_UPSTREAM):
        raw, source = os.environ[ENV_UPSTREAM], f"env {ENV_UPSTREAM}"
    else:
        raw, source = DEFAULT_UPSTREAM, "default"
    try:
        return normalize_upstream(raw), source
    except UpstreamError as exc:
        raise SystemExit(f"error: invalid upstream from {source}: {exc}") from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="serve.py",
        description=(
            "Static file host + same-origin JSON proxy for the LLM monitor "
            "dashboard (stdlib only)."
        ),
        epilog=(
            "Proxy routes: /api/health -> <upstream>/health, "
            "/api/models -> <upstream>/v1/models,\n"
            "              /api/cache -> <upstream>/cache, "
            "/api/config -> active config (no upstream call).\n\n"
            "Each proxy route accepts ?url=<http(s) base> to override the\n"
            "upstream for that one request.\n\n"
            f"Upstream precedence: --url > ${ENV_UPSTREAM} > {DEFAULT_UPSTREAM}\n\n"
            "Example:\n"
            "  python3 monitor/serve.py --url http://llm.home:8001 --port 8090\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--url",
        default=None,
        help=f"upstream base URL (env ${ENV_UPSTREAM}, default {DEFAULT_UPSTREAM})",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"bind address (default {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"bind port (default {DEFAULT_PORT})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    upstream, source = resolve_upstream(args.url)

    server = MonitorServer((args.host, args.port), MonitorHandler)
    server.monitor_upstream = upstream

    print(
        f"llm-monitor listening on http://{args.host}:{args.port} "
        f"(upstream {upstream} via {source})",
        file=sys.stderr,
        flush=True,
    )
    if not STATIC_DIR.is_dir():
        print(
            f"note: {STATIC_DIR} is missing; static routes will 404 until the UI lands",
            file=sys.stderr,
            flush=True,
        )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("shutting down", file=sys.stderr, flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
