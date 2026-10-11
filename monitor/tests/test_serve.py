"""Tests for monitor/serve.py -- the same-origin proxy behind the dashboard.

Standard library only (``unittest``), like the code under test::

    python3 -m unittest discover -s monitor/tests -p "test_*.py"
    python3 monitor/tests/test_serve.py -v

What is pinned here is the *contract* the dashboard's front end is written
against: one JSON envelope shape on every API route, the proxy always answering
200 (upstream trouble lives inside the envelope, never in the transport), the
``?url=`` override, the 5 s cap per upstream request, and the static hosting.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
MONITOR_DIR = TESTS_DIR.parent
sys.path.insert(0, str(MONITOR_DIR))
sys.path.insert(0, str(TESTS_DIR))

import serve  # noqa: E402  (path juggling first)
from mock_upstream import MockUpstream  # noqa: E402
import mock_upstream  # noqa: E402

# The proxy logs every request the way http.server does; hundreds of those lines
# would bury the test report. Test-only silencing -- the shipped server keeps
# its access log.
serve.MonitorHandler.log_message = lambda *args, **kwargs: None

ENVELOPE_KEYS = ["ok", "url", "status", "latency_ms", "content_type", "body", "error"]


def get(url: str, timeout: float = 15.0):
    """GET ``url``; return (status, headers, body bytes). Non-2xx included.

    ``headers`` is the case-insensitive ``email.message.Message`` from urllib --
    ``http.server`` spells some of these ``Content-type`` and some
    ``Content-Type``, and the dashboard does not care which.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        with exc:  # an HTTPError is also the response; leave no open sockets
            return exc.code, exc.headers, exc.read()


def get_json(url: str, **kw) -> dict:
    status, _, raw = get(url, **kw)
    return {"_status": status, **json.loads(raw.decode("utf-8"))}


class ProxyFixture(unittest.TestCase):
    """ Boots one mock model server + one monitor server for the class. """

    mode = "healthy"

    @classmethod
    def setUpClass(cls):
        cls.upstream = MockUpstream(mode=cls.mode)
        cls.upstream.__enter__()
        cls.monitor = ThreadingHTTPServer(("127.0.0.1", 0), serve.MonitorHandler)
        cls.monitor.monitor_upstream = cls.upstream.url
        cls.monitor_thread = threading.Thread(
            target=cls.monitor.serve_forever, name="monitor-under-test", daemon=True
        )
        cls.monitor_thread.start()
        cls.base = f"http://127.0.0.1:{cls.monitor.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.monitor.shutdown()
        cls.monitor.server_close()
        cls.upstream.__exit__(None, None, None)

    # -- helpers ------------------------------------------------------------

    def api(self, route: str, url: str | None = None) -> dict:
        target = f"{self.base}/api/{route}"
        if url is not None:
            target += "?url=" + urllib.parse.quote(url, safe="")
        return get_json(target)


# --- upstream URL validation -------------------------------------------------


class NormalizeUpstreamTests(unittest.TestCase):
    def test_accepts_absolute_http_urls(self):
        self.assertEqual(serve.normalize_upstream("http://llm.home:8001"), "http://llm.home:8001")
        self.assertEqual(serve.normalize_upstream("https://a.b"), "https://a.b")

    def test_strips_trailing_slashes_and_whitespace(self):
        self.assertEqual(serve.normalize_upstream("  http://a.b:8000/// "), "http://a.b:8000")

    def test_drops_query_and_fragment(self):
        self.assertEqual(
            serve.normalize_upstream("http://a.b:1/x?y=2#z"), "http://a.b:1/x"
        )

    def test_rejects_non_http_schemes(self):
        for bad in ("file:///etc/passwd", "gopher://x:1/", "ftp://a.b", "javascript:alert(1)"):
            with self.subTest(bad=bad):
                with self.assertRaises(serve.UpstreamError):
                    serve.normalize_upstream(bad)

    def test_rejects_hostless_and_empty(self):
        for bad in ("", "   ", None, "localhost:8000", "/relative", "http://"):
            with self.subTest(bad=bad):
                with self.assertRaises(serve.UpstreamError):
                    serve.normalize_upstream(bad)

    def test_target_url_never_doubles_the_slash(self):
        self.assertEqual(
            serve.target_url("http://a.b:8001///", serve.PROXY_ROUTES["/api/health"]),
            "http://a.b:8001/health",
        )


class ResolveUpstreamTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop(serve.ENV_UPSTREAM, None)

    def tearDown(self):
        if self._saved is not None:
            os.environ[serve.ENV_UPSTREAM] = self._saved
        else:
            os.environ.pop(serve.ENV_UPSTREAM, None)

    def test_cli_beats_env_beats_default(self):
        os.environ[serve.ENV_UPSTREAM] = "http://from-env:1"
        self.assertEqual(serve.resolve_upstream("http://from-cli:2")[0], "http://from-cli:2")
        self.assertEqual(serve.resolve_upstream(None)[0], "http://from-env:1")
        del os.environ[serve.ENV_UPSTREAM]
        self.assertEqual(serve.resolve_upstream(None), (serve.DEFAULT_UPSTREAM, "default"))

    def test_bad_value_exits_with_a_message(self):
        with self.assertRaises(SystemExit) as ctx:
            serve.resolve_upstream("not-a-url")
        self.assertIn("invalid upstream", str(ctx.exception))


# --- envelope / body helpers -------------------------------------------------


class EnvelopeTests(unittest.TestCase):
    def test_key_order_is_stable(self):
        env = serve.envelope(
            ok=True, url="u", status=200, latency_ms=1.234,
            content_type="application/json", body=None, error=None,
        )
        self.assertEqual(list(env.keys()), ENVELOPE_KEYS)
        self.assertEqual(env["latency_ms"], 1.23)  # rounded for humans

    def test_describe_bytes_parses_json_and_leaves_text(self):
        self.assertEqual(
            serve.describe_bytes(b'{"a": 1}', "application/json"), {"a": 1}
        )
        self.assertEqual(serve.describe_bytes(b"ok\n", "text/plain"), "ok\n")
        # Claimed JSON but not parseable (truncated body) must not explode.
        self.assertEqual(serve.describe_bytes(b'{"a":', "application/json"), '{"a":')
        # No content type but obviously JSON-shaped.
        self.assertEqual(serve.describe_bytes(b"[1,2]", ""), [1, 2])

    def test_read_body_honours_the_deadline(self):
        class Drip:
            def read1(self, _n):
                return b"x" * 1024

        with self.assertRaises(TimeoutError):
            serve.read_body(Drip(), deadline=0.0, timeout=0.0)

    def test_read_body_caps_oversized_payloads(self):
        class Big:
            def read1(self, n):
                return b"x" * n

        raw = serve.read_body(Big(), deadline=__import__("time").monotonic() + 5, timeout=5)
        self.assertLessEqual(len(raw), serve.MAX_BODY_BYTES)


class ProxyRouteMappingTests(unittest.TestCase):
    def test_config_advertises_exactly_the_proxied_endpoints(self):
        self.assertEqual(
            set(serve.PUBLIC_ENDPOINTS),
            set(serve.PROXY_ROUTES.values()),
            "/api/config tells the UI which endpoints exist; it must not lie",
        )
        self.assertNotIn("/api/config", serve.PROXY_ROUTES)

    def test_config_payload_shape(self):
        body = serve.config_payload("http://a.b:1")
        self.assertEqual(body["upstream"], "http://a.b:1")
        self.assertEqual(body["default_upstream"], serve.DEFAULT_UPSTREAM)


# --- the server, end to end ---------------------------------------------------


class HealthyUpstreamTests(ProxyFixture):
    mode = "healthy"

    def test_health_is_proxied_into_an_envelope(self):
        env = self.api("health")
        self.assertEqual(env["_status"], 200)
        self.assertEqual(env["ok"], True)
        self.assertEqual(env["status"], 200)
        self.assertEqual(env["url"], self.upstream.url + "/health")
        self.assertEqual(env["error"], None)
        self.assertEqual(env["body"]["status"], "ok")
        self.assertGreaterEqual(env["latency_ms"], 0)
        self.assertEqual(
            [k for k in env if k != "_status"], ENVELOPE_KEYS
        )

    def test_models_are_proxied(self):
        env = self.api("models")
        self.assertTrue(env["ok"])
        ids = [m["id"] for m in env["body"]["data"]]
        self.assertEqual(ids, ["qwen3-27b", "gemma4-26b"])

    def test_cache_is_proxied(self):
        env = self.api("cache")
        self.assertTrue(env["ok"])
        self.assertEqual(env["body"]["n_ctx"], 8192)

    def test_metrics_text_is_proxied_verbatim_and_parsed(self):
        env = self.api("metrics")
        self.assertTrue(env["ok"])
        # body stays the upstream's own text: /metrics is not JSON, and the raw
        # panel in the dashboard shows exactly what the server said.
        self.assertEqual(env["content_type"].split(";")[0], "text/plain")
        self.assertIn("llamacpp:prompt_tokens_total 779554", env["body"])
        # ...and the parsed view rides alongside it, as an extra key.
        self.assertEqual([k for k in env if k != "_status"], ENVELOPE_KEYS + ["metrics"])
        metrics = env["metrics"]
        self.assertEqual(metrics["format"], "prometheus")
        self.assertEqual(metrics["count"], 17)
        self.assertEqual(metrics["errors"], [])
        names = {s["name"] for s in metrics["series"]}
        self.assertIn("llamacpp:predicted_tokens_seconds", names)
        self.assertIn("halogen:requests_total", names)
        by_name = {f["name"]: f for f in metrics["families"]}
        self.assertEqual(by_name["halogen:requests_total"]["type"], "counter")
        self.assertEqual(
            by_name["llamacpp:requests_processing"]["help"],
            "Number of requests processing.",
        )
        value = [s for s in metrics["series"] if s["name"] == "llamacpp:requests_processing"][0]
        self.assertEqual(value["value"], 2.0)
        self.assertEqual(value["labels"], {})

    def test_config_names_the_startup_upstream(self):
        env = self.api("config")
        self.assertTrue(env["ok"])
        self.assertEqual(env["body"]["upstream"], self.upstream.url)
        # Documented convenience: config keys are also top-level.
        self.assertEqual(env["upstream"], self.upstream.url)

    def test_every_route_is_gettable(self):
        for route in ("config", "health", "models", "cache", "metrics"):
            with self.subTest(route=route):
                status, headers, _ = get(f"{self.base}/api/{route}")
                self.assertEqual(status, 200)
                self.assertIn("application/json", headers["Content-Type"])
                self.assertEqual(headers["Cache-Control"], "no-store")


class UrlOverrideTests(ProxyFixture):
    mode = "healthy"

    def test_url_override_retargets_a_single_request(self):
        with MockUpstream(mode="weird") as other:
            env = self.api("cache", url=other.url)
            self.assertTrue(env["ok"])
            self.assertEqual(env["url"], other.url + "/cache")
            self.assertIn("usage_percent", env["body"])
            # ...and the server's own default is untouched.
            self.assertEqual(self.api("config")["body"]["upstream"], self.upstream.url)

    def test_blank_url_falls_back_to_the_default(self):
        env = self.api("health", url="")
        self.assertEqual(env["url"], self.upstream.url + "/health")

    def test_bad_url_override_is_400_and_explains_itself(self):
        for bad in ("file:///etc/passwd", "localhost:8000", "javascript:alert(1)"):
            with self.subTest(bad=bad):
                env = self.api("health", url=bad)
                self.assertEqual(env["_status"], 400)
                self.assertFalse(env["ok"])
                self.assertIn("invalid ?url=", env["error"])

    def test_override_picks_a_base_not_a_path(self):
        # ?url= chooses the upstream *base*; the monitored path (/health) is
        # always appended by the proxy, so a caller cannot aim at an arbitrary
        # upstream path -- only at a different host:port this box may reach.
        env = self.api("health", url=self.upstream.url + "/deep")
        self.assertTrue(env["url"].endswith("/deep/health"))
        self.assertFalse(env["ok"])  # the mock 404s that route, and says so
        self.assertIn("404", env["error"])


class DeadUpstreamTests(ProxyFixture):
    mode = "healthy"

    def test_unreachable_upstream_is_still_a_200_envelope(self):
        # Port 9 (discard) on loopback: nothing listening.
        env = self.api("health", url="http://127.0.0.1:9")
        self.assertEqual(env["_status"], 200)
        self.assertFalse(env["ok"])
        self.assertIsNone(env["status"])
        self.assertIsNone(env["body"])
        self.assertIn("unreachable", env["error"])

    def test_five_hundred_is_reported_not_raised(self):
        with MockUpstream(mode="broken") as broken:
            env = self.api("health", url=broken.url)
            self.assertEqual(env["_status"], 200)
            self.assertFalse(env["ok"])
            self.assertEqual(env["status"], 500)
            self.assertIn("HTTP 500", env["error"])
            self.assertEqual(env["body"]["error"], "model server on fire")

    def test_unexpected_cache_shape_still_returns_a_body(self):
        with MockUpstream(mode="weird") as weird:
            env = self.api("cache", url=weird.url)
            self.assertTrue(env["ok"])
            self.assertIsInstance(env["body"], dict)
            self.assertIn("nested", env["body"])

    def test_non_json_upstream_body_degrades_to_text(self):
        with MockUpstream(mode="plain") as plain:
            env = self.api("health", url=plain.url)
            self.assertTrue(env["ok"])
            self.assertEqual(env["body"], "keep-alive\n")

    def test_empty_body_is_representable(self):
        with MockUpstream(mode="empty") as empty:
            env = self.api("health", url=empty.url)
            self.assertTrue(env["ok"])
            self.assertEqual(env["body"], "")


class TimeoutTests(ProxyFixture):
    mode = "healthy"

    def test_wedged_upstream_is_cut_off_at_the_timeout(self):
        import time

        with MockUpstream(mode="wedged") as wedged:
            wedged.wedge_seconds = 3
            started = time.monotonic()
            env = serve.proxy_get(wedged.url + "/health", timeout=0.5)
            elapsed = time.monotonic() - started
        self.assertFalse(env["ok"])
        self.assertIn("timed out", env["error"])
        self.assertLess(elapsed, 2.0, "proxy must not outlive its timeout")

    def test_one_wedged_route_does_not_block_the_others(self):
        """The dashboard polls three routes at once; a stall must not wedge the server."""
        import time

        with MockUpstream(mode="wedged") as wedged:
            wedged.wedge_seconds = 5
            stalled = f"{self.base}/api/health?url=" + urllib.parse.quote(
                wedged.url, safe=""
            )
            stuck = threading.Thread(target=lambda: get(stalled), daemon=True)
            stuck.start()
            time.sleep(0.3)  # let the stalled request get going
            started = time.monotonic()
            env = self.api("models")
            elapsed = time.monotonic() - started
        self.assertTrue(env["ok"])
        self.assertLess(elapsed, 2.0)


class MetricsRouteTests(ProxyFixture):
    """/api/metrics on servers that answer it badly, or not at all."""

    mode = "healthy"

    def test_odd_metrics_still_produce_every_good_series(self):
        with MockUpstream(mode="weird") as weird:
            env = self.api("metrics", url=weird.url)
            status, _, raw = get(f"{self.base}/api/metrics?url=" + urllib.parse.quote(weird.url, safe=""))
        self.assertTrue(env["ok"])
        metrics = env["metrics"]
        names = {s["name"] for s in metrics["series"]}
        self.assertIn("some:histogram_bucket", names)
        self.assertNotIn("totally", names)  # the unparsable line is not a series
        self.assertEqual(metrics["count"], 7)
        bucket = [s for s in metrics["series"] if s["name"] == "some:histogram_bucket"][0]
        # label values keep their commas
        self.assertEqual(bucket["labels"]["model"], "qwen3, 27b")
        # ...and a non-finite value survives as text, never as invalid JSON
        nan = [s for s in metrics["series"] if s["name"] == "some:nan"][0]
        self.assertIsNone(nan["value"])
        self.assertEqual(nan["value_text"], "NaN")
        self.assertTrue(any("not a number" in e for e in metrics["errors"]))
        # the wire bytes must be strict JSON: no bare NaN / Infinity literals
        self.assertIsNone(
            re.search(rb":\s*(NaN|\+?-Infinity)\b", raw),
            "non-finite values must travel as text, never as JSON literals",
        )
        self.assertIn(b'"value_text": "NaN"', raw)
        self.assertEqual(json.loads(raw)["metrics"]["count"], 7)

    def test_a_server_without_metrics_is_reported_not_raised(self):
        with MockUpstream(mode="nometrics") as quiet:
            env = self.api("metrics", url=quiet.url)
            health = self.api("health", url=quiet.url)
        self.assertEqual(env["_status"], 200)
        self.assertFalse(env["ok"])
        self.assertEqual(env["status"], 404)
        self.assertIsNone(env["metrics"])  # nothing text-shaped to parse
        # ...while the rest of that server is fine (this must not read as "down")
        self.assertTrue(health["ok"])

    def test_non_prometheus_text_parses_to_nothing_but_says_so(self):
        with MockUpstream(mode="plain") as plain:
            env = self.api("metrics", url=plain.url)
        self.assertTrue(env["ok"])
        self.assertEqual(env["body"], "keep-alive\n")
        # "keep-alive" looks like a name with a non-numeric value: zero series.
        self.assertEqual(env["metrics"]["count"], 0)
        self.assertTrue(env["metrics"]["errors"])


class PrometheusParserTests(unittest.TestCase):
    """Unit tests for the exposition-format reader (no sockets involved)."""

    def test_counts_and_groups(self):
        parsed = serve.parse_prometheus(
            "# HELP a Total a.\n# TYPE a counter\na 1\n# TYPE b gauge\nb 2.5\n"
        )
        self.assertEqual(parsed["count"], 2)
        self.assertEqual(parsed["truncated"], False)
        self.assertEqual(
            [(f["name"], f["type"]) for f in parsed["families"]], [("a", "counter"), ("b", "gauge")]
        )
        self.assertEqual(parsed["series"][1]["value"], 2.5)

    def test_help_may_come_after_type_and_names_may_contain_colons(self):
        parsed = serve.parse_prometheus("# TYPE x:y gauge\n# HELP x:y Some help.\nx:y 1\n")
        self.assertEqual(parsed["families"][0], {"name": "x:y", "type": "gauge", "help": "Some help."})
        self.assertEqual(parsed["series"][0]["name"], "x:y")

    def test_labels_including_escapes_commas_and_braces(self):
        parsed = serve.parse_prometheus('m{a="1",b="say \\"hi\\"",c="x, } y"} 4\n')
        self.assertEqual(
            parsed["series"][0]["labels"], {"a": "1", "b": 'say "hi"', "c": "x, } y"}
        )
        self.assertEqual(parsed["errors"], [])

    def test_timestamps_and_exponential_notation(self):
        parsed = serve.parse_prometheus("m 1.5e3 1600000000000\n")
        self.assertEqual(parsed["series"][0]["value"], 1500.0)

    def test_non_finite_values_are_json_safe(self):
        parsed = serve.parse_prometheus("a NaN\nb +Inf\nc -Inf\n")
        self.assertEqual([s["value"] for s in parsed["series"]], [None, None, None])
        self.assertEqual([s["value_text"] for s in parsed["series"]], ["NaN", "+Inf", "-Inf"])
        self.assertEqual(parsed["errors"], [], "NaN/Inf are legal, not errors")

    def test_garbage_is_skipped_and_reported(self):
        parsed = serve.parse_prometheus(
            "# just a comment\n# EOF\ntotally unparsable here\nbroken{ 7\n\n   \ngood 1\n"
        )
        self.assertEqual([s["name"] for s in parsed["series"]], ["good"])
        self.assertTrue(any("unterminated label" in e for e in parsed["errors"]))
        self.assertEqual(len(parsed["errors"]), len(parsed["errors"][: serve.MAX_PARSE_ERRORS]))

    def test_empty_input_is_harmless(self):
        for blank in ("", "   \n\n", None):
            with self.subTest(blank=blank):
                parsed = serve.parse_prometheus(blank)
                self.assertEqual(parsed["count"], 0)
                self.assertEqual(parsed["series"], [])

    def test_series_are_capped(self):
        body = "\n".join(f"m{i} {i}" for i in range(serve.MAX_SERIES + 50))
        parsed = serve.parse_prometheus(body)
        self.assertEqual(parsed["count"], serve.MAX_SERIES)
        self.assertTrue(parsed["truncated"])

    def test_attach_metrics_keeps_body_and_adds_one_key(self):
        result = serve.envelope(
            ok=True, url="u", status=200, latency_ms=1, content_type="text/plain",
            body="x 1\n", error=None,
        )
        serve.attach_metrics(result)
        self.assertEqual(list(result.keys()), ENVELOPE_KEYS + ["metrics"])
        self.assertEqual(result["body"], "x 1\n")
        self.assertEqual(result["metrics"]["count"], 1)

    def test_attach_metrics_on_a_body_there_is_not(self):
        for body in (None, "", "   ", {"json": True}, [1]):
            with self.subTest(body=body):
                result = serve.attach_metrics({"body": body})
                self.assertIsNone(result["metrics"])


class UnknownApiTests(ProxyFixture):
    mode = "healthy"

    def test_unknown_api_route_is_404_in_the_same_shape(self):
        env = self.api("nope")
        self.assertEqual(env["_status"], 404)
        self.assertFalse(env["ok"])
        self.assertEqual(env["status"], 404)
        self.assertIn("unknown api route", env["error"])

    def test_api_prefix_without_a_trailing_slash(self):
        self.assertEqual(get(f"{self.base}/api")[0], 404)

    def test_head_works_without_a_body(self):
        status, headers, raw = get(f"{self.base}/api/config")
        self.assertEqual(status, 200)
        request = urllib.request.Request(f"{self.base}/api/config", method="HEAD")
        with urllib.request.urlopen(request, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")
            self.assertGreater(int(resp.headers["Content-Length"]), 0)
        del raw


class StaticHostingTests(ProxyFixture):
    mode = "healthy"

    def test_root_serves_the_dashboard(self):
        status, headers, raw = get(self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"llm monitor", raw[:2000])

    def test_assets_are_served_with_bodies(self):
        # A real bug this guards against: send_head() alone sets Content-Length
        # and then forgets to copy the bytes, so the client hangs on an empty 200.
        for name, needle in (
            ("app.js", b"function"),
            ("styles.css", b"--"),
            ("index.html", b"<html"),
        ):
            with self.subTest(name=name):
                status, headers, raw = get(f"{self.base}/{name}")
                self.assertEqual(status, 200)
                self.assertGreater(len(raw), 100)
                self.assertEqual(len(raw), int(headers["Content-Length"]))
                self.assertIn(needle, raw[:4000])

    def test_missing_file_is_404_not_hung(self):
        self.assertEqual(get(self.base + "/nope.js")[0], 404)

    def test_directory_listing_is_disabled(self):
        self.assertEqual(get(self.base + "/static/")[0], 404)

    def test_traversal_cannot_escape_the_static_dir(self):
        for target in ("/../../etc/passwd", "/%2e%2e%2f%2e%2e%2fetc%2fpasswd", "/..%2f..%2fetc"):
            with self.subTest(target=target):
                self.assertIn(get(self.base + target)[0], (400, 404))


# --- scrape passthrough (GET /metrics) --------------------------------------

#: Every self-metric the proxy owns; the ADR (workspace-szw.1, C2) is the source
#: of the names, the code is the source of the list.
SELF_SERIES_NAMES = {name for name, _kind, _help in serve.SELF_METRICS}


def parse_body(raw: bytes) -> dict:
    """Parse a passthrough body with the proxy's own parser (the acceptance
    criterion is "parseable by parse_prometheus", so the tests use exactly that
    parser rather than a private one)."""
    return serve.parse_prometheus(raw.decode("utf-8", errors="replace"))


def self_block(raw: bytes) -> bytes:
    """The appended ``llm_monitor_*`` block on its own.

    Parsing the *whole* body would hit the proxy's own series cap when the
    upstream is huge, and the self-metrics are the last lines in it.
    """
    marker = raw.index(b"# llm-monitor ")
    return raw[marker:]


def self_series(raw: bytes, name: str) -> list[dict]:
    return [s for s in parse_body(self_block(raw))["series"] if s["name"] == name]


def self_value(raw: bytes, name: str) -> float:
    found = self_series(raw, name)
    if not found:
        raise AssertionError(f"{name} is missing from the self-metrics block")
    return found[0]["value"]


class PassthroughMixin:
    """``scrape()`` for a :class:`ProxyFixture` subclass."""

    def scrape(self, url: str | None = None, path: str | None = None, **kw):
        target = self.base + (path or serve.PASSTHROUGH_ROUTE)
        if url is not None:
            target += "?url=" + urllib.parse.quote(url, safe="")
        return get(target, **kw)


class PassthroughTests(PassthroughMixin, ProxyFixture):
    """``GET /metrics``: the route a real Prometheus scrapes. Plain text, not the
    envelope; the upstream bytes verbatim; the proxy's own series appended."""

    mode = "healthy"

    def upstream_body(self) -> bytes:
        with urllib.request.urlopen(self.upstream.url + "/metrics", timeout=15) as resp:
            return resp.read()

    def test_upstream_exposition_comes_back_verbatim(self):
        native = self.upstream_body()
        status, headers, raw = self.scrape()
        self.assertEqual(status, 200)
        self.assertTrue(
            raw.startswith(native),
            "the upstream bytes must be copied byte for byte, in order",
        )
        self.assertEqual(len(raw), int(headers["Content-Length"]))

    def test_utf8_bytes_survive_the_round_trip(self):
        # Non-ASCII label values are legal exposition text, and decode/re-encode
        # is where a "verbatim" passthrough quietly mangles them.
        with MockUpstream(mode="unicode") as uni:
            with urllib.request.urlopen(uni.url + "/metrics", timeout=15) as resp:
                native = resp.read()
            status, _, raw = self.scrape(url=uni.url)
        self.assertEqual(status, 200)
        self.assertEqual(raw[: len(native)], native)
        self.assertIn("日本語".encode("utf-8"), raw)
        self.assertNotIn(b"\xef\xbf\xbd", raw, "no U+FFFD: the bytes were copied, not re-encoded")
        series = [
            s for s in parse_body(self_block(raw))["series"]
            if s["name"] == "llm_monitor_scrape_upstream_status"
        ]
        self.assertEqual(series[0]["labels"]["upstream"], uni.url)
        self.assertEqual(parse_body(raw)["errors"], [])

    def test_content_type_is_the_exposition_contract(self):
        status, headers, _ = self.scrape()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], serve.PROMETHEUS_CONTENT_TYPE)
        self.assertEqual(
            serve.PROMETHEUS_CONTENT_TYPE, "text/plain; version=0.0.4; charset=utf-8"
        )

    def test_no_json_envelope_and_no_markup_on_this_path(self):
        _, _, raw = self.scrape()
        self.assertFalse(raw.lstrip().startswith((b"{", b"[")), "must not be the envelope")
        for key in (b'"latency_ms"', b'"content_type"', b'"ok"', b'"error"'):
            self.assertNotIn(key, raw)
        self.assertNotIn(b"<html", raw.lower())
        self.assertNotIn(b"<!doctype", raw.lower())

    def test_native_metric_names_are_not_renamed_or_relabeled(self):
        native = self.upstream_body()
        _, _, raw = self.scrape()
        self.assertTrue(raw.startswith(native), "verbatim, in order")
        for name, value in (
            (b"llamacpp:prompt_tokens_total", b"779554"),
            (b"halogen:prompt_tokens_cached_total", b"49838237"),
            (b"llamacpp:kv_cache_usage_ratio", b"0.625"),
        ):
            with self.subTest(name=name):
                self.assertIn(name + b" " + value, raw)
                # the colon spelling is the contract; an underscore rewrite here
                # would silently break every recording rule written against it
                self.assertNotIn(name.replace(b":", b"_"), raw)
        self.assertIn(b"# TYPE llamacpp:requests_processing gauge", raw)

    def test_self_metrics_are_appended_and_parse_cleanly(self):
        _, _, raw = self.scrape()
        parsed = parse_body(raw)
        self.assertEqual(parsed["errors"], [], "the whole body must be valid exposition")
        self.assertFalse(parsed["truncated"])
        self.assertEqual(
            {s["name"] for s in parsed["series"] if s["name"].startswith(serve.SELF_METRIC_PREFIX)},
            SELF_SERIES_NAMES,
        )

    def test_build_info_is_one_and_carries_the_version(self):
        _, _, raw = self.scrape()
        info = self_series(raw, "llm_monitor_build_info")
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0]["value"], 1)
        self.assertEqual(info[0]["labels"], {"version": serve.VERSION})

    def test_counters_and_gauges_describe_this_scrape(self):
        native_count = parse_body(self.upstream_body())["count"]
        _, _, first = self.scrape()
        _, _, second = self.scrape()
        self.assertEqual(
            self_value(second, "llm_monitor_scrape_success_total"),
            self_value(first, "llm_monitor_scrape_success_total") + 1,
            "a counter, incremented once per successful scrape",
        )
        self.assertEqual(self_value(second, "llm_monitor_scrape_ok"), 1)
        self.assertEqual(self_value(second, "llm_monitor_scrape_upstream_status"), 200)
        self.assertEqual(
            self_value(second, "llm_monitor_series_count"), native_count
        )
        self.assertEqual(self_value(second, "llm_monitor_parse_truncated"), 0)
        self.assertGreaterEqual(self_value(second, "llm_monitor_scrape_latency_ms"), 0)

    def test_only_the_scraped_upstream_is_rendered(self):
        """``sum(llm_monitor_*)`` across targets must not double-count (ADR C2)."""
        with MockUpstream(mode="weird") as other:
            _, _, raw = self.scrape(url=other.url)
            self.assertIn(other.url.encode(), raw)
            self.assertNotIn(
                self.upstream.url.encode(),
                raw[raw.index(b"# llm-monitor"):],
                "the self-metrics block must carry only the scraped upstream's row",
            )
            rows = [s for s in self_series(raw, "llm_monitor_scrape_ok")]
            self.assertEqual([s["labels"]["upstream"] for s in rows], [other.url])

    def test_parse_errors_in_the_upstream_body_are_counted(self):
        with MockUpstream(mode="weird") as weird:
            _, _, raw = self.scrape(url=weird.url)
            with urllib.request.urlopen(weird.url + "/metrics", timeout=15) as resp:
                native = resp.read()
        self.assertGreaterEqual(
            self_value(raw, "llm_monitor_parse_errors_total"), 1,
            "'some:not_a_number nope' and 'totally unparsable here' are errors",
        )
        self.assertEqual(self_value(raw, "llm_monitor_parse_truncated"), 0)
        # ...and the odd body still came through verbatim, errors and all
        self.assertTrue(raw.startswith(native))

    def test_head_works_without_a_body(self):
        request = urllib.request.Request(self.base + serve.PASSTHROUGH_ROUTE, method="HEAD")
        with urllib.request.urlopen(request, timeout=15) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["Content-Type"], serve.PROMETHEUS_CONTENT_TYPE)
            self.assertGreater(int(resp.headers["Content-Length"]), 0)
            self.assertEqual(resp.read(), b"")

    def test_trailing_slash_is_the_same_route(self):
        status, headers, raw = self.scrape(path=serve.PASSTHROUGH_ROUTE + "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], serve.PROMETHEUS_CONTENT_TYPE)
        self.assertIn(b"llm_monitor_build_info", raw)

    def test_the_rest_of_the_surface_is_untouched(self):
        """Claiming /metrics must not disturb /api/* or the static hosting."""
        env = self.api("metrics")
        self.assertEqual([k for k in env if k != "_status"], ENVELOPE_KEYS + ["metrics"])
        self.assertEqual(
            env["metrics"]["count"], parse_body(self.upstream_body())["count"]
        )
        status, headers, raw = get(self.base + "/index.html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"<", raw[:200])


class PassthroughFailureTests(PassthroughMixin, ProxyFixture):
    """A failed scrape is a scrape failure: 502 + one line of plain text."""

    mode = "healthy"

    def assert_scrape_failure(self, status: int, headers, raw: bytes, needle: str) -> None:
        self.assertEqual(status, 502)
        self.assertTrue(headers["Content-Type"].startswith("text/plain"), headers["Content-Type"])
        self.assertEqual(raw.count(b"\n"), 1, "exactly one line, terminated")
        self.assertIn(needle, raw.decode())
        self.assertNotIn(b"<html", raw.lower())
        self.assertNotIn(b"<!doctype", raw.lower())
        self.assertFalse(raw.lstrip().startswith((b"{", b"[")), "never a 200-shaped envelope")
        self.assertNotIn(b"llm_monitor_", raw, "no sample set to speak of")

    def test_upstream_500_is_a_502_with_a_reason(self):
        with MockUpstream(mode="broken") as broken:
            status, headers, raw = self.scrape(url=broken.url)
        self.assert_scrape_failure(status, headers, raw, "HTTP 500")

    def test_upstream_without_the_route_is_a_502(self):
        with MockUpstream(mode="nometrics") as quiet:
            status, headers, raw = self.scrape(url=quiet.url)
        self.assert_scrape_failure(status, headers, raw, "HTTP 404")

    def test_unreachable_upstream_is_a_502(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        status, headers, raw = self.scrape(url=f"http://127.0.0.1:{dead_port}")
        self.assert_scrape_failure(status, headers, raw, "unreachable")

    def test_wedged_upstream_hits_the_five_second_cap(self):
        import time

        with MockUpstream(mode="wedged") as wedged:
            wedged.wedge_seconds = serve.UPSTREAM_TIMEOUT_SECONDS + 3
            started = time.monotonic()
            status, headers, raw = self.scrape(url=wedged.url, timeout=20)
            elapsed = time.monotonic() - started
        self.assert_scrape_failure(status, headers, raw, "timed out")
        self.assertLess(
            elapsed, serve.UPSTREAM_TIMEOUT_SECONDS + 3.0,
            "the scrape must not outlive the per-request cap",
        )

    def test_a_wedged_scrape_does_not_block_the_ui_polls(self):
        """The passthrough runs on its own thread; the dashboard must stay live."""
        import time

        with MockUpstream(mode="wedged") as wedged:
            wedged.wedge_seconds = 5
            stuck = threading.Thread(
                target=lambda: self.scrape(url=wedged.url, timeout=20), daemon=True
            )
            stuck.start()
            time.sleep(0.3)  # let the stalled scrape get going
            started = time.monotonic()
            env = self.api("models")
            elapsed = time.monotonic() - started
        self.assertTrue(env["ok"])
        self.assertLess(elapsed, 2.0, "/api/models waited on a stalled scrape")

    def test_bad_url_overrides_are_400_in_plain_text(self):
        for bad in (
            "file:///etc/passwd",
            "localhost:8000",
            "gopher://llm:1",
            "ftp://a.b",
            "/relative",
            "http://",
        ):
            with self.subTest(bad=bad):
                status, headers, raw = self.scrape(url=bad)
                self.assertEqual(status, 400)
                self.assertTrue(headers["Content-Type"].startswith("text/plain"))
                self.assertIn("invalid ?url=", raw.decode())
                self.assertNotIn(b"<html", raw.lower())
                self.assertNotIn(b"\n", raw.strip(b"\n"), "one line only")
                self.assertNotIn(b"etc/passwd", raw, "do not echo the rejected target back")

    def test_blank_url_falls_back_to_the_default_upstream(self):
        status, _, raw = self.scrape(url="")
        self.assertEqual(status, 200)
        self.assertIn(self.upstream.url.encode(), raw)

    def test_a_bad_url_does_not_poison_the_self_metrics(self):
        _, _, raw = self.scrape(url="file:///etc/passwd")
        self.assertEqual(raw.count(b"llm_monitor_"), 0)
        _, _, ok = self.scrape()
        self.assertEqual(self_value(ok, "llm_monitor_scrape_ok"), 1)


class PassthroughClaimsMetricsTests(unittest.TestCase):
    """``/metrics`` is claimed ahead of ``static/`` so a static asset can never
    shadow the route Prometheus is configured to scrape."""

    def setUp(self):
        self.shadow = Path(tempfile.mkdtemp(prefix="llm-monitor-shadow-"))
        # A file that *would* be served if the passthrough did not claim the path.
        (self.shadow / "metrics").write_text("STATIC-ASSET-NAMED-METRICS", encoding="utf-8")
        outer = self

        class ShadowHandler(serve.MonitorHandler):
            def __init__(self, *args, **kwargs):
                kwargs["directory"] = str(outer.shadow)
                super().__init__(*args, **kwargs)

        self.upstream = MockUpstream(mode="healthy")
        self.upstream.__enter__()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ShadowHandler)
        self.server.monitor_upstream = self.upstream.url
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.upstream.__exit__(None, None, None)
        shutil.rmtree(self.shadow, ignore_errors=True)

    def test_static_asset_cannot_shadow_the_scrape_route(self):
        status, headers, raw = get(self.base + serve.PASSTHROUGH_ROUTE)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], serve.PROMETHEUS_CONTENT_TYPE)
        self.assertNotIn(b"STATIC-ASSET-NAMED-METRICS", raw)
        self.assertIn(b"llm_monitor_build_info", raw)

    def test_other_paths_still_come_from_the_static_dir(self):
        # The shadow dir proves the static handler is really the one being used,
        # i.e. the passthrough claims exactly one path and nothing else.
        (self.shadow / "page.html").write_text("<html>page</html>", encoding="utf-8")
        status, headers, raw = get(self.base + "/page.html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"page", raw)
        self.assertEqual(get(self.base + "/index.html")[0], 404)


class PassthroughBoundsTests(PassthroughMixin, ProxyFixture):
    """A chatty server must not make the scrape unbounded."""

    mode = "healthy"

    def test_series_cap_marks_itself_in_the_self_metrics(self):
        with MockUpstream(mode="huge") as huge:
            status, _, raw = self.scrape(url=huge.url)
            with urllib.request.urlopen(huge.url + "/metrics", timeout=30) as resp:
                native = resp.read()
        self.assertEqual(status, 200)
        self.assertEqual(self_value(raw, "llm_monitor_parse_truncated"), 1)
        self.assertEqual(self_value(raw, "llm_monitor_series_count"), serve.MAX_SERIES)
        # the body itself is still verbatim: every series the server sent
        self.assertTrue(raw.startswith(native))
        self.assertGreater(native.count(b"\n"), serve.MAX_SERIES)

    def test_oversized_body_is_capped_and_says_so(self):
        with MockUpstream(mode="huge") as huge:
            with mock.patch.object(serve, "MAX_BODY_BYTES", 4096):
                status, headers, raw = self.scrape(url=huge.url)
        self.assertEqual(status, 200)
        self.assertIn(b"truncated at 4096 bytes", raw)
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        # cap respected: the upstream body plus a marker and the self-metrics block
        self.assertLess(len(raw), 4096 + 4096)
        self.assertIn(b"llm_monitor_build_info", raw)


class ResetFixtureTests(unittest.TestCase):
    """The double `monitoring/parity` uses to make a counter reset observable.

    A live server is not obliged to restart inside a measurement window, so the
    parity run's counter-reset probe drives this instead of waiting for luck. Its
    contract is the thing worth testing: the truth is constant, the numerator is not
    monotonic, and the ratio-of-lifetimes figure the bespoke page used collapses —
    if any of that stops being true the probe proves nothing.
    """

    TRUTH_PCT = 80.0          # 80 accepted per 100 drafted, every cycle
    CYCLE_SECONDS = 45.0      # when the numerator restarts

    def values(self, age: float) -> dict:
        parsed = serve.parse_prometheus(mock_upstream.reset_counters_body(age))
        self.assertEqual(parsed["errors"], [], "the reset body must stay parseable")
        return {s["name"]: s["value"] for s in parsed["series"]}

    def test_the_construction_is_what_the_probe_thinks_it_is(self):
        for age in (1.0, 20.0, 44.0, 60.0, 300.0):
            v = self.values(age)
            drafted = v["halogen:draft_tokens_total"]
            accepted = v["halogen:draft_tokens_accepted_total"]
            self.assertGreater(drafted, 0)
            # rate() over any window containing the reset sees the same 80/100,
            # because the drafted counter never stops and the drops are counted back.
            self.assertAlmostEqual(100.0 * accepted / drafted if drafted else 0,
                                   self.ratio_at(age), places=6)

    def ratio_at(self, age: float) -> float:
        """The ratio-of-lifetimes a reader would compute at this age."""
        return 100.0 * (80.0 * (age % self.CYCLE_SECONDS)) / (100.0 * age)

    def test_the_numerator_restarts_and_the_lifetimes_ratio_decays(self):
        before = self.values(self.CYCLE_SECONDS - 1.0)
        after = self.values(self.CYCLE_SECONDS + 1.0)
        self.assertAlmostEqual(self.TRUTH_PCT,
                               100.0 * before["halogen:draft_tokens_accepted_total"]
                               / before["halogen:draft_tokens_total"], places=6)
        self.assertLess(after["halogen:draft_tokens_accepted_total"],
                        before["halogen:draft_tokens_accepted_total"],
                        "the numerator must go backwards, or there is no reset")
        self.assertGreater(after["halogen:draft_tokens_total"],
                           before["halogen:draft_tokens_total"],
                           "the denominator must keep climbing, or the ratio is not a lifetime")
        old = self.values(1800.0)   # half an hour in
        self.assertLess(100.0 * old["halogen:draft_tokens_accepted_total"]
                        / old["halogen:draft_tokens_total"], 3.0,
                        "the lifetime ratio should have decayed towards zero by now")

    def test_the_prompt_cache_pair_resets_the_same_way(self):
        v = self.values(1800.0)
        hit = 100.0 * v["halogen:prompt_tokens_cached_total"] / (
            v["halogen:prompt_tokens_cached_total"] + v["llamacpp:prompt_tokens_total"])
        self.assertLess(hit, 5.0, "same mechanism, 90% by construction, reading ~0")

    def test_the_mode_serves_it_over_http(self):
        with MockUpstream(mode="reset") as fixture:
            with urllib.request.urlopen(fixture.url + "/metrics", timeout=5) as resp:
                body = resp.read().decode()
        self.assertIn("halogen:draft_tokens_accepted_total", body)
        self.assertEqual(serve.parse_prometheus(body)["errors"], [])


class ScrapeStatsTests(unittest.TestCase):
    """The ``llm_monitor_*`` series themselves (no sockets involved)."""

    def test_render_is_never_empty_and_always_names_every_series(self):
        rendered = serve.ScrapeStats().render()
        self.assertIn("llm_monitor_build_info", rendered)
        for name, kind, _help in serve.SELF_METRICS:
            with self.subTest(name=name):
                self.assertIn(f"# TYPE {name} {kind}", rendered)
        parsed = serve.parse_prometheus(rendered)
        self.assertEqual(parsed["errors"], [])

    def test_failure_is_recorded_as_ok_zero_with_the_upstream_status(self):
        stats = serve.ScrapeStats()
        stats.observe("http://llm:8001", ok=False, status=500, latency_ms=12.5)
        parsed = serve.parse_prometheus(stats.render())
        by_name = {s["name"]: s for s in parsed["series"]}
        self.assertEqual(by_name["llm_monitor_scrape_ok"]["value"], 0)
        self.assertEqual(by_name["llm_monitor_scrape_upstream_status"]["value"], 500)
        self.assertEqual(by_name["llm_monitor_scrape_success_total"]["value"], 0)
        self.assertEqual(by_name["llm_monitor_scrape_latency_ms"]["value"], 12.5)

    def test_no_answer_from_the_upstream_reports_status_zero(self):
        stats = serve.ScrapeStats()
        stats.observe("http://llm:8001", ok=False, status=None, latency_ms=5000.0)
        rendered = stats.render()
        self.assertIn('llm_monitor_scrape_upstream_status{upstream="http://llm:8001"} 0', rendered)
        self.assertIn('llm_monitor_scrape_ok{upstream="http://llm:8001"} 0', rendered)

    def test_counters_accumulate_and_gauges_track_the_latest_scrape(self):
        stats = serve.ScrapeStats()
        for latency in (10.0, 20.0, 30.0):
            stats.observe(
                "http://llm:8001", ok=True, status=200, latency_ms=latency,
                series_count=5, parse_errors=1, truncated=False,
            )
        parsed = serve.parse_prometheus(stats.render())
        by_name = {s["name"]: s for s in parsed["series"]}
        self.assertEqual(by_name["llm_monitor_scrape_success_total"]["value"], 3)
        self.assertEqual(by_name["llm_monitor_parse_errors_total"]["value"], 3)
        self.assertEqual(by_name["llm_monitor_scrape_latency_ms"]["value"], 30.0)
        self.assertEqual(by_name["llm_monitor_series_count"]["value"], 5)

    def test_one_row_per_upstream_and_only_filtering(self):
        stats = serve.ScrapeStats()
        stats.observe("http://a:1", ok=True, status=200, latency_ms=1)
        stats.observe("http://b:2", ok=True, status=200, latency_ms=2)
        both = stats.render()
        self.assertEqual(both.count('upstream="http://a:1"'), len(SELF_SERIES_NAMES) - 1)
        only_b = serve.parse_prometheus(stats.render(only="http://b:2"))
        upstreams = {s["labels"]["upstream"] for s in only_b["series"] if "upstream" in s["labels"]}
        self.assertEqual(upstreams, {"http://b:2"})
        self.assertIn("http://a:1", both)

    def test_upstream_label_values_are_escaped(self):
        stats = serve.ScrapeStats()
        stats.observe('http://a:1/x"y', ok=True, status=200, latency_ms=1)
        parsed = serve.parse_prometheus(stats.render())
        upstreams = {s["labels"]["upstream"] for s in parsed["series"] if "upstream" in s["labels"]}
        self.assertEqual(upstreams, {'http://a:1/x"y'})

    def test_self_metric_names_use_underscores_and_are_prefixed(self):
        for name, _kind, _help in serve.SELF_METRICS:
            self.assertTrue(name.startswith(serve.SELF_METRIC_PREFIX), name)
            self.assertNotIn(":", name)
            self.assertNotIn("-", name)
        # build_info is a gauge carrying a constant 1, per the ADR
        kinds = {name: kind for name, kind, _help in serve.SELF_METRICS}
        self.assertEqual(kinds["llm_monitor_build_info"], "gauge")
        self.assertEqual(kinds["llm_monitor_scrape_success_total"], "counter")


class RenderPassthroughTests(unittest.TestCase):
    """Unit level: how the body is assembled."""

    def render(self, body: bytes, **extra) -> bytes:
        stats = serve.ScrapeStats()
        result = dict({"body": body, "status": 200, "latency_ms": 1.0, "ok": True}, **extra)
        return serve.render_passthrough(result, stats, "http://llm:8001")

    def test_missing_final_newline_is_repaired(self):
        out = self.render(b"llamacpp:requests_processing 2")
        self.assertTrue(out.startswith(b"llamacpp:requests_processing 2\n"))

    def test_existing_newline_is_not_doubled(self):
        out = self.render(b"llamacpp:requests_processing 2\n")
        self.assertFalse(out.startswith(b"llamacpp:requests_processing 2\n\n"))

    def test_empty_upstream_body_still_answers_as_exposition(self):
        out = self.render(b"")
        self.assertEqual(out.count(b"\n\n"), 0)
        parsed = serve.parse_prometheus(out.decode())
        self.assertEqual(parsed["errors"], [])
        self.assertIn("llm_monitor_build_info", out.decode())

    def test_appended_block_is_marked_as_the_proxies_own(self):
        out = self.render(b"a 1\n")
        self.assertIn(b"# llm-monitor", out)
        self.assertIn(serve.VERSION.encode(), out)

    def test_truncated_result_is_visible_in_the_body(self):
        out = self.render(b"a 1\n", truncated=True)
        self.assertIn(b"truncated at", out)
        self.assertEqual(serve.parse_prometheus(out.decode())["errors"], [])


class ArgParsingTests(unittest.TestCase):
    def test_defaults(self):
        args = serve.parse_args([])
        self.assertEqual(args.host, serve.DEFAULT_HOST)
        self.assertEqual(args.port, serve.DEFAULT_PORT)
        self.assertIsNone(args.url)

    def test_flags(self):
        args = serve.parse_args(["--url", "http://x:1", "--host", "0.0.0.0", "--port", "9"])
        self.assertEqual((args.url, args.host, args.port), ("http://x:1", "0.0.0.0", 9))


if __name__ == "__main__":
    unittest.main(verbosity=2)
