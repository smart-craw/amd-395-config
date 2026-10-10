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
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
MONITOR_DIR = TESTS_DIR.parent
sys.path.insert(0, str(MONITOR_DIR))
sys.path.insert(0, str(TESTS_DIR))

import serve  # noqa: E402  (path juggling first)
from mock_upstream import MockUpstream  # noqa: E402

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

    def test_config_names_the_startup_upstream(self):
        env = self.api("config")
        self.assertTrue(env["ok"])
        self.assertEqual(env["body"]["upstream"], self.upstream.url)
        # Documented convenience: config keys are also top-level.
        self.assertEqual(env["upstream"], self.upstream.url)

    def test_every_route_is_gettable(self):
        for route in ("config", "health", "models", "cache"):
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
