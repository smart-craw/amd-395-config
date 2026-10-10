#!/usr/bin/env python3
"""Backend for the LLM monitor dashboard.

Serves the static UI out of ``monitor/static/`` and, on the *same origin*,
proxies a handful of read-only GETs to the model server:

    GET /api/health        -> <upstream>/health
    GET /api/models        -> <upstream>/v1/models
    GET /api/cache         -> <upstream>/cache
    GET /api/metrics       -> <upstream>/metrics  (+ parsed Prometheus series)
    GET /api/config        -> active upstream + defaults (no upstream call)
    GET /api/scrape-health -> <prometheus>/api/v1/targets (monitor the monitor)
    GET /metrics           -> <upstream>/metrics, verbatim + llm_monitor_* self-metrics

``GET /metrics`` is the plain-text scrape passthrough, the one route that is not a
JSON envelope: it answers the exposition body the model server sent, byte for
byte, with ``Content-Type: text/plain; version=0.0.4; charset=utf-8`` and the
proxy's own ``llm_monitor_*`` series appended, so a real Prometheus can pull
through here when it cannot reach the model server directly. The two-layer
architecture (Prometheus/Grafana owns history; this page is the "right now" view)
is decided in ``docs/adr/0001-prometheus-grafana-owns-metrics.md``, and the scrape
job itself lives under ``monitoring/``. A body over ``MAX_BODY_BYTES`` is
capped like every other upstream response and the truncation is said out loud in a
comment line rather than hidden.

The proxy exists because the model server (llama.cpp / vLLM style) does not send
CORS headers, so a page served from another origin could not poll it directly.
UI + API from one origin means no CORS problem at all.

The ``/api/metrics`` route is the JSON-shaped twin of the passthrough: same
upstream call, but the raw text is left in ``body`` exactly as the upstream said
it and the parsed form is added as an extra ``metrics`` key so the dashboard does
not have to ship a second parser::

    {"format": "prometheus", "count": 19, "truncated": false, "errors": [],
     "families": [{"name": "llamacpp:requests_processing", "type": "gauge",
                   "help": "Number of requests processing."}],
     "series": [{"name": "llamacpp:requests_processing", "labels": {},
                 "value": 0.0, "value_text": "0"}]}

Every proxy response uses the same envelope so the UI never has to guess:

    {"ok": bool, "url": "<absolute upstream url>", "status": int|null,
     "latency_ms": float, "content_type": str, "body": <json|raw text>,
     "error": str|null}

A dead, slow or angry upstream is still HTTP 200 from the proxy with ``ok:false``
and a populated ``error`` -- never a 500 that takes the dashboard down, and never
a hang: every upstream request is capped at 5 seconds wall-clock, whatever the
upstream is doing (stalled, half-open, or dribbling bytes out forever).

Every proxy route (including the ``/metrics`` passthrough) additionally accepts
``?url=<http(s) base>`` so the dashboard can re-point the endpoint selector live
and one proxy can be scraped as several distinct targets; anything that is not an
absolute http(s) URL is refused with 400. That override lets the caller make the
server fetch an arbitrary URL -- and ``GET /metrics`` additionally leaks the whole
exposition (model ids, traffic shape, prompt-cache behaviour) -- which is exactly
why the default bind address is loopback. Only pass ``--host 0.0.0.0`` on a
network you trust the requesters on.

Standard library only. No build step, no ``pip install``, no ``npm install``.

Usage:
    python3 monitor/serve.py --url http://llm.home:8001 --port 8090
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
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

DEFAULT_UPSTREAM = "http://llm.home:8080"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090

#: Reported in llm_monitor_build_info so a broken monitor is at least a
#: *versioned* broken monitor.
VERSION = "1.1.0"

#: Environment fallback, mirroring the env-configurable-upstream pattern used by
#: BACKEND_SERVICE in qwen3.8-flash/docker-compose.yml.
ENV_UPSTREAM = "LLM_MONITOR_UPSTREAM"

#: Where the historical view lives. Neither is used for anything but links and
#: the scrape-health read-out; both are opt-in (unset = "no Grafana / Prometheus
#: on this box", which is a supported configuration, not a broken one).
ENV_GRAFANA_URL = "LLM_MONITOR_GRAFANA_URL"
ENV_PROMETHEUS_URL = "LLM_MONITOR_PROMETHEUS_URL"

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
    "/api/metrics": "/metrics",
}

#: The raw scrape passthrough (proxy path -> upstream path). Deliberately *not*
#: part of PROXY_ROUTES: it answers plain text, not the JSON envelope, and it is
#: the path Prometheus scrapes, so it has to stay out of the static-file tree.
PASSTHROUGH_ROUTE = "/metrics"
PASSTHROUGH_UPSTREAM_PATH = "/metrics"

#: Exact response Content-Type of the passthrough (the exposition format's own
#: spelling; Prometheus parses the body either way but the header is the
#: contract the scrape job documents).
PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: The upstream-side endpoints the UI should offer in its selector.
PUBLIC_ENDPOINTS = ["/health", "/v1/models", "/cache", "/metrics"]

#: The one /api route whose upstream payload is not JSON.
METRICS_ROUTE = "/api/metrics"

#: Prefix for this proxy's own series. Underscores (ours); the upstream's native
#: colon-names are passed through untouched (ADR workspace-szw.1).
SELF_METRIC_PREFIX = "llm_monitor_"
#: Series name -> (type, help) for the self-metrics appended to /metrics.
SELF_METRICS = (
    ("llm_monitor_scrape_success_total", "counter",
     "Passthrough scrapes of this upstream that returned exposition text."),
    ("llm_monitor_scrape_ok", "gauge",
     "1 when the most recent passthrough scrape of this upstream succeeded, else 0."),
    ("llm_monitor_scrape_latency_ms", "gauge",
     "Round-trip time of the most recent passthrough scrape, in milliseconds."),
    ("llm_monitor_scrape_upstream_status", "gauge",
     "Upstream HTTP status of the most recent passthrough scrape (0 = no answer)."),
    ("llm_monitor_parse_errors_total", "counter",
     "Exposition lines this proxy could not parse since it started."),
    ("llm_monitor_parse_truncated", "gauge",
     "1 when the last scrape hit the series cap and was cut short."),
    ("llm_monitor_series_count", "gauge",
     "Series kept from the last successful scrape of this upstream."),
    ("llm_monitor_build_info", "gauge",
     "Constant 1; labels carry the monitor version answering this route."),
)

# --- Prometheus text exposition format -------------------------------------
#
# /metrics answers lines like:
#
#     # HELP llamacpp:requests_processing Number of requests processing.
#     # TYPE llamacpp:requests_processing gauge
#     llamacpp:requests_processing 0
#     llama:histogram_bucket{model="qwen",le="0.5"} 12 1600000000000
#
# Names may contain ':' (llama.cpp uses ``llamacpp:prompt_tokens_total``), label
# values may contain escaped quotes and commas, and the value may be NaN/Inf --
# which JSON cannot spell, so such a sample carries ``value: null`` plus the
# original token in ``value_text``.

_METRIC_NAME = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
_RE_COMMENT = re.compile(r"^#\s*(?P<kind>[A-Za-z]+)\b(?P<rest>.*)$")
_RE_SAMPLE = re.compile(r"^(?P<name>" + _METRIC_NAME + r")(?P<rest>.*)$")
_RE_LABEL = re.compile(r'(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:\\.|[^"\\])*)"')

#: Series kept per scrape, so a chatty server cannot balloon the response.
MAX_SERIES = 2000
#: Distinct bad lines reported back before the list is closed.
MAX_PARSE_ERRORS = 8

_UNESCAPE = {"n": "\n", '"': '"', "\\": "\\", "t": "\t"}


def _unescape(text: str) -> str:
    """Undo the exposition format's label-value escapes."""
    if "\\" not in text:
        return text
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            out.append(_UNESCAPE.get(nxt, nxt))
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def sample_value(token: str) -> tuple[float | None, str, str | None]:
    """Parse one sample value into ``(number|None, text, problem|None)``.

    ``NaN`` / ``+Inf`` / ``-Inf`` are legal in the exposition format and illegal
    in JSON, so they come back as ``None`` with their original spelling in the
    second slot and no complaint. A token that is not a number at all (a torn
    line, an HTML error page) does report a problem, which the caller lists in
    ``errors`` instead of dropping the line silently.
    """
    try:
        value = float(token)
    except (TypeError, ValueError):
        return None, token, f"value {token!r} is not a number"
    if not math.isfinite(value):
        return None, token, None  # NaN / +-Inf: valid here, unspellable in JSON
    return value, token, None


def parse_labels(text: str) -> tuple[dict[str, str], str | None]:
    """``a="b",c="d\"e"`` -> ``({"a": "b", "c": 'd"e'}, None)``.

    The second slot carries a human-readable complaint instead of raising: one
    odd label must not lose the rest of the scrape.
    """
    labels: dict[str, str] = {}
    if not text.strip():
        return labels, None
    pos = 0
    while pos < len(text):
        while pos < len(text) and text[pos] in " \t,":
            pos += 1
        if pos >= len(text):
            break
        match = _RE_LABEL.match(text, pos)
        if not match:
            return labels, f"unparsable label list: {text[:60]!r}"
        labels[match.group("key")] = _unescape(match.group("value"))
        pos = match.end()
    return labels, None


def _split_labels(rest: str) -> tuple[dict[str, str], str, str | None]:
    """Cut a ``{...}`` label block off the front of ``rest``.

    Brace hunting has to be quote-aware: a label value may itself contain ``}``.
    """
    if not rest.startswith("{"):
        return {}, rest, None
    depth = 0
    in_quotes = False
    escaped = False
    for index, char in enumerate(rest):
        if in_quotes:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_quotes = False
            continue
        if char == '"':
            in_quotes = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                labels, problem = parse_labels(rest[1:index])
                return labels, rest[index + 1 :], problem
    return {}, "", "unterminated label block"


def parse_prometheus(text: str) -> dict:
    """Parse Prometheus/OpenMetrics text into JSON-friendly data.

    Never raises: anything that is not a sample line is skipped and named in
    ``errors``, so a half-broken scrape still renders every good series.
    """
    families: dict[str, dict] = {}
    series: list[dict] = []
    errors: list[str] = []
    truncated = False

    def complain(message: str) -> None:
        if len(errors) < MAX_PARSE_ERRORS:
            errors.append(message)

    def family(name: str) -> dict:
        return families.setdefault(name, {"name": name, "type": None, "help": None})

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#"):
            comment = _RE_COMMENT.match(line)
            kind = comment.group("kind").upper() if comment else ""
            if kind not in ("HELP", "TYPE") or not comment:
                continue  # a plain comment, '# UNIT', '# EOF', ...
            name, _, detail = comment.group("rest").strip().partition(" ")
            if not name:
                continue
            if kind == "HELP":
                family(name)["help"] = detail.strip() or None
            else:
                family(name)["type"] = detail.strip() or None
            continue

        sample = _RE_SAMPLE.match(line)
        if not sample:
            complain(f"not a sample line: {line[:60]!r}")
            continue

        name = sample.group("name")
        labels, rest, problem = _split_labels(sample.group("rest").strip())
        if problem:
            complain(f"{name}: {problem}")
            continue
        fields = rest.split()
        if len(fields) < 1:
            complain(f"{name}: no value after the name")
            continue
        if len(fields) > 2:
            complain(f"{name}: too many fields: {line[:60]!r}")
            continue
        value, as_text, problem = sample_value(fields[0])
        if problem:
            # Not a sample after all (a torn line, an HTML error page): report it
            # rather than file it away as a series with a null value.
            complain(f"{name}: {problem}")
            continue
        if len(series) >= MAX_SERIES:
            truncated = True
            continue
        family(name)
        series.append(
            {"name": name, "labels": labels, "value": value, "value_text": as_text}
        )

    if truncated:
        complain(f"only the first {MAX_SERIES} series were kept")

    return {
        "format": "prometheus",
        "count": len(series),
        "truncated": truncated,
        "errors": errors,
        "families": list(families.values()),
        "series": series,
    }


def attach_metrics(result: dict) -> dict:
    """Add a ``metrics`` key to the /api/metrics envelope, in place.

    ``body`` keeps the upstream's own text (the raw panel shows it, and it is the
    fallback whenever the parse finds nothing). ``metrics`` is ``null`` when
    there is no text to parse -- no body, or a server that answered /metrics with
    real JSON, which is a shape the dashboard then just shows as-is.
    """
    body = result.get("body")
    if isinstance(body, str) and body.strip():
        result["metrics"] = parse_prometheus(body)
    else:
        result["metrics"] = None
    return result


def escape_label_value(value: str) -> str:
    """Escape a label value the way the exposition format demands."""
    return (
        str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )


class ScrapeStats:
    """The proxy's own series, so scrape health is visible as PromQL.

    Keyed by the upstream base URL, because one proxy can front several servers
    (``/metrics?url=...``) and each is scraped as its own target. Counters are
    process-lifetime (they reset when the monitor restarts, which is exactly what
    a counter reset means to PromQL); gauges describe the most recent scrape.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_upstream: dict[str, dict] = {}

    def observe(
        self,
        upstream: str,
        *,
        ok: bool,
        status: int | None,
        latency_ms: float,
        series_count: int | None = None,
        parse_errors: int | None = None,
        truncated: bool | None = None,
    ) -> None:
        with self._lock:
            state = self._by_upstream.setdefault(
                upstream,
                {
                    "success_total": 0,
                    "parse_errors_total": 0,
                },
            )
            state["ok"] = 1 if ok else 0
            state["latency_ms"] = round(float(latency_ms), 3)
            state["status"] = int(status) if status is not None else 0
            if ok:
                state["success_total"] += 1
            if parse_errors:
                state["parse_errors_total"] += int(parse_errors)
            if series_count is not None:
                state["series_count"] = int(series_count)
            if truncated is not None:
                state["truncated"] = 1 if truncated else 0

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._by_upstream.items()}

    def render(self, only: str | None = None) -> str:
        """Render the self-metrics as exposition text (never empty).

        ``only`` limits the output to one upstream: a Prometheus scrape of
        ``/metrics?url=A`` should not carry the numbers of servers B and C, or
        every ``sum(llm_monitor_*)`` would count them once per target.
        """
        snapshot = self.snapshot()
        if only is not None:
            snapshot = {k: v for k, v in snapshot.items() if k == only}
        lines: list[str] = []
        for name, kind, help_text in SELF_METRICS:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            if name == "llm_monitor_build_info":
                lines.append(
                    'llm_monitor_build_info{version="%s"} 1' % escape_label_value(VERSION)
                )
                continue
            for upstream, state in sorted(snapshot.items()):
                label = 'upstream="%s"' % escape_label_value(upstream)
                if name == "llm_monitor_scrape_success_total":
                    value = state.get("success_total", 0)
                elif name == "llm_monitor_scrape_ok":
                    value = state.get("ok", 0)
                elif name == "llm_monitor_scrape_latency_ms":
                    value = state.get("latency_ms", 0)
                elif name == "llm_monitor_scrape_upstream_status":
                    value = state.get("status", 0)
                elif name == "llm_monitor_parse_errors_total":
                    value = state.get("parse_errors_total", 0)
                elif name == "llm_monitor_parse_truncated":
                    value = state.get("truncated", 0)
                else:  # llm_monitor_series_count
                    value = state.get("series_count", 0)
                lines.append(f"{name}{{{label}}} {value}")
        lines.append("")
        return "\n".join(lines)


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


def read_body(
    response, *, deadline: float, timeout: float, cut: list | None = None
) -> bytes:
    """Drain ``response`` honouring ``deadline`` and :data:`MAX_BODY_BYTES`.

    ``urlopen(timeout=...)`` bounds every *single* socket operation, not the
    request as a whole, so a server that trickles a byte every few seconds could
    keep a request open forever. Reading in chunks and re-checking the wall-clock
    deadline between reads keeps the per-request cap honest.

    ``cut`` (optional) is a list the size cap appends ``True`` to, so a caller can
    tell "the whole body" from "the first :data:`MAX_BODY_BYTES` of it" -- the
    passthrough uses that to say so out loud instead of handing out a torn body
    that looks complete.
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
            if cut is not None:
                cut.append(True)
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


def fetch_raw(url: str, *, deadline: float, timeout: float = UPSTREAM_TIMEOUT_SECONDS) -> dict:
    """GET ``url`` and hand back the *bytes* (no JSON envelope, no decoding).

    The passthrough has to be byte-identical to what the model server answered --
    ``describe_bytes`` would turn the exposition text into a Python str and the
    envelope would wrap it in JSON, and Prometheus could eat neither.
    """
    started = time.monotonic()
    cut: list[bool] = []

    def elapsed_ms() -> float:
        return (time.monotonic() - started) * 1000.0

    req = urlrequest.Request(
        url,
        method="GET",
        headers={
            "Accept": "text/plain, */*",
            "User-Agent": USER_AGENT,
            "Connection": "close",
        },
    )

    def outcome(*, ok, status, raw, error, content_type=""):
        return {
            "ok": bool(ok),
            "url": url,
            "status": int(status) if status is not None else None,
            "latency_ms": elapsed_ms(),
            "content_type": content_type,
            "body": raw or b"",
            "error": error,
            "truncated": bool(cut),
        }

    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            status = int(resp.status)
            content_type = resp.headers.get("Content-Type") or ""
            raw = read_body(resp, deadline=deadline, timeout=timeout, cut=cut)
        if 200 <= status < 400:
            return outcome(
                ok=True, status=status, raw=raw, error=None, content_type=content_type
            )
        return outcome(
            ok=False,
            status=status,
            raw=b"",
            error=f"HTTP {status}",
            content_type=content_type,
        )
    except urlerror.HTTPError as exc:  # subclass of URLError: must come first
        return outcome(
            ok=False,
            status=exc.code,
            raw=b"",
            error=f"HTTP {exc.code} {exc.reason}",
            content_type=(exc.headers.get("Content-Type") if exc.headers else "") or "",
        )
    except (TimeoutError, urlerror.URLError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(exc, TimeoutError) or "timed out" in str(reason).lower():
            message = f"timed out after {timeout:g}s"
        else:
            message = f"unreachable: {reason}"
        return outcome(ok=False, status=None, raw=b"", error=message)
    except Exception as exc:  # noqa: BLE001 - the scrape fails, the proxy does not
        return outcome(
            ok=False, status=None, raw=b"", error=f"{type(exc).__name__}: {exc}"
        )


def proxy_raw(url: str, *, timeout: float = UPSTREAM_TIMEOUT_SECONDS) -> dict:
    """Raw-bytes twin of :func:`proxy_get`, with the same hard cap.

    Same thread-and-join trick, so a wedged upstream cannot hold a scrape open
    for longer than ``timeout`` seconds; the caller turns a failure into an
    HTTP 502, which is what makes Prometheus record a scrape failure instead of
    parsing garbage as a sample set.
    """
    started = time.monotonic()
    deadline = started + timeout
    outcome: dict[str, dict] = {}

    def worker() -> None:
        try:
            outcome["result"] = fetch_raw(url, deadline=deadline, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            outcome["result"] = {
                "ok": False,
                "url": url,
                "status": None,
                "latency_ms": (time.monotonic() - started) * 1000.0,
                "content_type": "",
                "body": b"",
                "error": f"{type(exc).__name__}: {exc}",
                "truncated": False,
            }

    thread = threading.Thread(target=worker, name="llm-monitor-scrape", daemon=True)
    thread.start()
    thread.join(timeout)
    if "result" in outcome:
        return outcome["result"]

    return {
        "ok": False,
        "url": url,
        "status": None,
        "latency_ms": (time.monotonic() - started) * 1000.0,
        "content_type": "",
        "body": b"",
        "error": f"timed out after {timeout:g}s",
        "truncated": False,
    }


def render_passthrough(result: dict, stats: "ScrapeStats", upstream: str) -> bytes:
    """Upstream exposition bytes + the proxy's own series, as one body.

    The upstream text is copied verbatim (only a missing final newline is
    repaired, which the format requires anyway); the ``llm_monitor_*`` block is
    appended after it. Nothing is renamed or relabelled: the monitor's own
    numbers are additional series, never edits of the server's.

    The ``upstream`` label is the base URL the operator addressed (``--url`` or
    ``?url=``), not the full ``.../metrics`` URL, so it matches how the same
    server is named everywhere else in the tooling.
    """
    raw = result.get("body") or b""
    if not isinstance(raw, bytes):
        raw = str(raw).encode("utf-8", errors="replace")
    text = raw.decode("utf-8", errors="replace")
    parsed = parse_prometheus(text) if text.strip() else None
    stats.observe(
        upstream,
        ok=True,
        status=result.get("status"),
        latency_ms=result.get("latency_ms") or 0.0,
        series_count=None if parsed is None else parsed["count"],
        parse_errors=None if parsed is None else len(parsed["errors"]),
        truncated=None if parsed is None else parsed["truncated"],
    )
    if raw and not raw.endswith(b"\n"):
        raw += b"\n"
    if result.get("truncated"):
        # A body cut off at the cap is not the exposition: say so in a comment
        # line (which every Prometheus parser ignores) rather than let a torn
        # scrape look complete.
        raw += (
            b"# llm-monitor: upstream body truncated at "
            + str(MAX_BODY_BYTES).encode("ascii")
            + b" bytes; this scrape is incomplete\n"
        )
    marker = (
        b"# llm-monitor "
        + VERSION.encode("ascii")
        + b" self-metrics (appended by monitor/serve.py, not served by the upstream)\n"
    )
    return raw + marker + stats.render(only=upstream).encode("utf-8")


def config_payload(upstream: str, *, grafana_url: str = "", prometheus_url: str = "") -> dict:
    """Body of /api/config: lets the UI render the config field unhardcoded."""
    return {
        "upstream": upstream,
        "endpoints": list(PUBLIC_ENDPOINTS),
        "default_upstream": DEFAULT_UPSTREAM,
        # Additive keys (the three above are unchanged for back-compat): where
        # the historical view lives, and which version of the monitor this is.
        "grafana_url": grafana_url,
        "prometheus_url": prometheus_url,
        "version": VERSION,
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
            elif path == "/api/scrape-health":
                self._send_scrape_health(include_body)
            elif path in PROXY_ROUTES:
                self._send_proxy(path, parts.query, include_body)
            elif path == PASSTHROUGH_ROUTE:
                self._send_passthrough(parts.query, include_body)
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
                self._serve_static(include_body)
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

    @property
    def grafana_url(self) -> str:
        return getattr(self.server, "monitor_grafana_url", "") or ""

    @property
    def prometheus_url(self) -> str:
        return getattr(self.server, "monitor_prometheus_url", "") or ""

    @property
    def scrape_stats(self) -> ScrapeStats:
        stats = getattr(self.server, "monitor_scrape_stats", None)
        if stats is None:  # a bare ThreadingHTTPServer (tests) still answers
            stats = ScrapeStats()
            self.server.monitor_scrape_stats = stats
        return stats

    def _send_text(
        self, status: HTTPStatus, text: str, *, content_type: str, include_body: bool
    ) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if include_body:
            self.wfile.write(data)

    def _resolved_upstream(self, query: str) -> tuple[str | None, str | None]:
        """``?url=`` override + the server default, with validation.

        Returns ``(upstream, error)``; exactly one of the two is non-None.
        """
        params = urlparse.parse_qs(query, keep_blank_values=True)
        override = params.get("url", [""])[0]
        try:
            return (
                normalize_upstream(override) if override.strip() else self.upstream,
                None,
            )
        except UpstreamError as exc:
            return None, f"invalid ?url= override: {exc}"

    def _send_scrape_health(self, include_body: bool) -> None:
        """Thin proxy of ``<prometheus>/api/v1/targets``: monitor the monitor.

        The browser must not talk to Prometheus directly (that reintroduces the
        CORS problem this proxy exists to solve), so the page asks *this* server
        and gets the envelope plus a ``scrape_health`` digest. Prometheus being
        down is an ``ok:false`` envelope, never a 500.
        """
        base = self.prometheus_url
        digest = {
            "configured": bool(base),
            "reachable": False,
            "prometheus_url": base or None,
            "targets_total": 0,
            "targets_up": 0,
            "jobs": [],
        }
        if not base:
            payload = envelope(
                ok=True,
                url=None,
                status=None,
                latency_ms=0.0,
                content_type="application/json",
                body=dict(digest, error=None),
                error=None,
            )
            payload["scrape_health"] = payload["body"]
            self._send_json(HTTPStatus.OK, payload, include_body)
            return

        try:
            url = target_url(base, "/api/v1/targets")
        except UpstreamError as exc:
            payload = envelope(
                ok=False,
                url=base,
                status=int(HTTPStatus.BAD_REQUEST),
                latency_ms=0.0,
                content_type="application/json",
                body=dict(digest, error=str(exc)),
                error=f"invalid prometheus URL: {exc}",
            )
            payload["scrape_health"] = payload["body"]
            self._send_json(HTTPStatus.OK, payload, include_body)
            return

        result = proxy_get(url)
        body = result.get("body")
        targets = []
        if isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, dict) and isinstance(data.get("activeTargets"), list):
                targets = [t for t in data["activeTargets"] if isinstance(t, dict)]
        jobs = []
        for target in targets:
            labels = target.get("labels") if isinstance(target.get("labels"), dict) else {}
            jobs.append(
                {
                    "job": str(labels.get("job", "")),
                    "instance": str(labels.get("instance", "")),
                    "health": str(target.get("health") or "unknown"),
                    "last_error": str(target.get("lastError") or ""),
                }
            )
        digest["targets_total"] = len(jobs)
        digest["targets_up"] = sum(1 for j in jobs if j["health"] == "up")
        digest["reachable"] = bool(result.get("ok"))
        digest["error"] = result.get("error")
        payload = envelope(
            ok=bool(result.get("ok")),
            url=result.get("url"),
            status=result.get("status"),
            latency_ms=result.get("latency_ms") or 0.0,
            content_type=result.get("content_type") or "application/json",
            body=digest,
            error=result.get("error"),
        )
        payload["scrape_health"] = digest
        self._send_json(HTTPStatus.OK, payload, include_body)

    def _send_passthrough(self, query: str, include_body: bool) -> None:
        """``GET /metrics``: the raw scrape passthrough, plain text end to end.

        Prometheus scrapes this URL, so it must never see the JSON envelope, an
        HTML error page or a 200-with-garbage: a failed scrape answers 502 with
        a one-line reason and Prometheus records a scrape failure.
        """
        upstream, error = self._resolved_upstream(query)
        if upstream is None:
            self.scrape_stats.observe(
                query[:200] or "(invalid)",
                ok=False,
                status=int(HTTPStatus.BAD_REQUEST),
                latency_ms=0.0,
            )
            self._send_text(
                HTTPStatus.BAD_REQUEST,
                f"llm-monitor passthrough: {error}\n",
                content_type="text/plain; charset=utf-8",
                include_body=include_body,
            )
            return

        url = target_url(upstream, PASSTHROUGH_UPSTREAM_PATH)
        result = proxy_raw(url)
        if not result["ok"]:
            self.scrape_stats.observe(
                upstream,
                ok=False,
                status=result.get("status"),
                latency_ms=result.get("latency_ms") or 0.0,
            )
            reason = f"llm-monitor passthrough: GET {url} failed -> {result.get('error')}\n"
            self._send_text(
                HTTPStatus.BAD_GATEWAY,
                reason,
                content_type="text/plain; charset=utf-8",
                include_body=include_body,
            )
            return

        body = render_passthrough(result, self.scrape_stats, upstream)
        self._send_text(
            HTTPStatus.OK,
            body.decode("utf-8", errors="replace"),
            content_type=PROMETHEUS_CONTENT_TYPE,
            include_body=include_body,
        )

    def _send_config(self, include_body: bool) -> None:
        config = config_payload(
            self.upstream,
            grafana_url=self.grafana_url,
            prometheus_url=self.prometheus_url,
        )
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
            upstream = (
                normalize_upstream(override) if override.strip() else self.upstream
            )
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
        if route == METRICS_ROUTE:
            # The only non-JSON route: hand the UI a parsed view alongside the text.
            attach_metrics(result)
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

    def _serve_static(self, include_body: bool) -> None:
        if not STATIC_DIR.is_dir():
            self.send_error(
                HTTPStatus.NOT_FOUND,
                "No static files (monitor/static/ does not exist yet)",
            )
            return
        # send_head() only writes the status line + headers and hands back the
        # open file; the body has to be copied by the caller (that is what
        # SimpleHTTPRequestHandler.do_GET does). Skipping that step answers 200
        # with a Content-Length but zero bytes, and the client then hangs until
        # it times out. HEAD gets the headers only, which is correct.
        fobj = self.send_head()
        if fobj is not None:
            try:
                if include_body:
                    self.copyfile(fobj, self.wfile)
            finally:
                fobj.close()

    def list_directory(self, path):  # noqa: ANN201 - directory listings off
        """Never expose a directory listing; 404 instead."""
        self.send_error(HTTPStatus.NOT_FOUND, "Directory listing is disabled")
        return None


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True  # a stuck request cannot block shutdown
    allow_reuse_address = True
    monitor_upstream = DEFAULT_UPSTREAM
    monitor_grafana_url = ""
    monitor_prometheus_url = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Shared across handler threads: one row of self-metrics per upstream.
        self.monitor_scrape_stats = ScrapeStats()


def resolve_optional_url(cli_value: str | None, env_name: str) -> str:
    """Validate an opt-in URL (Grafana / Prometheus); "" means "not configured".

    Same validation as the model-server upstream (absolute http(s), host
    required), so a typo fails loudly at startup instead of producing dead links
    in the page. CLI flag beats env, mirroring :func:`resolve_upstream`.
    """
    for raw, source in ((cli_value, "flag"), (os.environ.get(env_name), f"env {env_name}")):
        if raw and raw.strip():
            try:
                return normalize_upstream(raw)
            except UpstreamError as exc:
                raise SystemExit(f"error: invalid URL from {source}: {exc}") from exc
    return ""


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
            "Proxy routes (JSON envelope): /api/health -> <upstream>/health, "
            "/api/models\n"
            "  -> <upstream>/v1/models, /api/cache -> <upstream>/cache, "
            "/api/metrics\n"
            "  -> <upstream>/metrics (Prometheus text, also returned parsed as\n"
            "     JSON), /api/config -> active config (no upstream call),\n"
            "  /api/scrape-health -> <prometheus>/api/v1/targets digest.\n\n"
            "Scrape passthrough (plain text, NOT an envelope):\n"
            "  GET /metrics -> <upstream>/metrics verbatim + llm_monitor_*\n"
            "     self-metrics; a non-2xx upstream answers 502 with a one-line\n"
            "     reason so Prometheus records a scrape failure.\n"
            "     Appended: llm_monitor_scrape_success_total, llm_monitor_scrape_ok,\n"
            "     llm_monitor_scrape_latency_ms,\n"
            "     llm_monitor_scrape_upstream_status,\n"
            "     llm_monitor_parse_errors_total, llm_monitor_parse_truncated,\n"
            "     llm_monitor_series_count, llm_monitor_build_info{version=\"...\"}.\n\n"
            "Each proxy route accepts ?url=<http(s) base> to override the\n"
            "upstream for that one request (so /metrics?url=A and\n"
            "/metrics?url=B can be scraped as separate targets).\n\n"
            f"Upstream precedence: --url > ${ENV_UPSTREAM} > {DEFAULT_UPSTREAM}\n\n"
            "INFORMATION DISCLOSURE: ?url= lets a caller make this server fetch\n"
            "an arbitrary URL, and GET /metrics hands out the whole exposition\n"
            "(model ids, traffic shape, prompt-cache behaviour). That is why the\n"
            "bind address defaults to loopback; --host 0.0.0.0 is a deliberate\n"
            "choice about who on your network may read this data.\n\n"
            "Example:\n"
            "  python3 monitor/serve.py --url http://llm.home:8001 --port 8090 \\\n"
            "      --grafana-url http://localhost:3000 --prometheus-url http://localhost:9090\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--url",
        default=None,
        help=f"upstream base URL (env ${ENV_UPSTREAM}, default {DEFAULT_UPSTREAM})",
    )
    parser.add_argument(
        "--grafana-url",
        default=None,
        help=(
            "base URL of the Grafana that owns the historical view; enables the "
            "dashboard deep links on the metrics card "
            f"(env ${ENV_GRAFANA_URL}, default: unset = no links)"
        ),
    )
    parser.add_argument(
        "--prometheus-url",
        default=None,
        help=(
            "base URL of the Prometheus scraping this box; enables the "
            "scrape-health read-out, proxied so the page never calls "
            f"Prometheus directly (env ${ENV_PROMETHEUS_URL}, default: unset)"
        ),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=(
            f"bind address (default {DEFAULT_HOST}, loopback; --host 0.0.0.0 is an "
            "information-disclosure decision -- see INFORMATION DISCLOSURE below)"
        ),
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
    grafana_url = resolve_optional_url(args.grafana_url, ENV_GRAFANA_URL)
    prometheus_url = resolve_optional_url(args.prometheus_url, ENV_PROMETHEUS_URL)

    server = MonitorServer((args.host, args.port), MonitorHandler)
    server.monitor_upstream = upstream
    server.monitor_grafana_url = grafana_url
    server.monitor_prometheus_url = prometheus_url

    extras = []
    if grafana_url:
        extras.append(f"grafana {grafana_url}")
    if prometheus_url:
        extras.append(f"prometheus {prometheus_url}")
    print(
        f"llm-monitor listening on http://{args.host}:{args.port} "
        f"(upstream {upstream} via {source})"
        + ("" if not extras else " [" + ", ".join(extras) + "]"),
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
