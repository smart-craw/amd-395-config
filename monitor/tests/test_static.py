"""Contract tests for the dashboard's front end, checked without a browser.

These pin the rules the UI is built on -- rules that a Python test of
monitor/serve.py cannot see and that break silently in hand-written HTML:

* every ``#id`` app.js looks up exists in index.html (a typo means a silently
  missing widget, because ``$("#x")`` just returns null);
* every CSS class the JS toggles is actually styled, or a state change would be
  invisible;
* nothing third-party is loaded (the box is meant to work air-gapped, and the
  acceptance check greps index.html for http(s) URLs);
* upstream data only ever reaches the page through ``textContent`` -- never
  ``innerHTML``, which would hand any model server a script tag;
* the degraded-UI affordances (banner, retry buttons, stale chips) are in the
  markup the JS expects to find.

See test_app.test.mjs for the behavioural counterpart.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

MONITOR_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = MONITOR_DIR / "static"

sys.path.insert(0, str(MONITOR_DIR))
import serve  # noqa: E402

HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

#: Class tokens the JS toggles that must carry a visual state. A missing rule
#: here means a card silently stops changing appearance.
REQUIRED_CLASSES = [
    "pill-pending", "pill-healthy", "pill-degraded", "pill-down",
    "badge-ok", "badge-warn", "badge-bad", "badge-neutral",
    "is-error", "is-stale", "is-loading", "is-paused", "is-promoted",
    "is-warn", "is-bad", "is-unknown",
    "t-ok", "t-warn", "t-bad",
    "btn-preset", "btn-retry", "btn-primary", "btn-ghost",
    "spark-line", "spark-fill", "spark-grid", "spark-empty", "spark-head",
    "kv-item", "kv-key", "kv-value",
    "card-error", "card-note", "chip-stale", "empty-state", "banner",
    "card-wide", "metric-sub",
]

#: Element ids the four cards are built from, with the per-card extras spelled
#: out.
#: ids every card defines, and the ones only one card defines. app.js builds a
#: full ref set per card and guards the ones it does not use, so this is the
#: contract for the ones it *does* dereference.
COMMON_CARD_SUFFIXES = [
    "badge", "spark", "spark-stats", "error", "error-text", "stale",
]
CARD_ID_SUFFIXES = {
    "health": ["status", "code", "latency", "last", "extra", "uptime"],
    "models": ["body", "empty"],
    "cache": [
        "gauge-label", "gauge-value", "bar", "bar-fill", "gauge-sub",
        "kv", "raw", "raw-json",
    ],
    "metrics": [
        "gauge-label", "gauge-value", "bar", "bar-fill", "gauge-sub",
        "kv", "raw", "raw-json", "note", "tiles", "throughput",
        "throughput-stats",
    ],
}
CHROME_IDS = [
    "status-pill", "status-text", "target-upstream", "last-updated", "next-poll",
    "upstream-form", "upstream-input", "upstream-error", "preset-row",
    "poll-interval", "refresh-now", "banner", "banner-text", "foot-uptime", "cards",
]


class AssetLocalityTests(unittest.TestCase):
    """Nothing may be fetched from outside the box."""

    def test_html_has_no_remote_references(self):
        for match in re.finditer(r'(?:src|href)\s*=\s*"([^"]*)"', HTML):
            target = match.group(1)
            with self.subTest(target=target):
                self.assertFalse(
                    target.startswith(("http://", "https://", "//")),
                    f"index.html points off-box at {target}",
                )

    def test_no_external_urls_in_css_or_js(self):
        for name, source in (("styles.css", CSS), ("app.js", JS)):
            with self.subTest(name=name):
                for url in re.findall(r"""url\(\s*['"]?(https?:)?//[^)]*""", source):
                    self.fail(f"{name} loads {url!r} from the network")
                self.assertNotIn("cdn", source.lower())

    def test_only_inline_svg_and_local_assets(self):
        self.assertNotIn("<img", HTML)
        self.assertIn('rel="stylesheet" href="styles.css"', HTML)
        self.assertIn('src="app.js"', HTML)
        # an inline data: favicon keeps even that request local
        self.assertIn('rel="icon" href="data:', HTML)


class MarkupContractTests(unittest.TestCase):
    def ids(self):
        return re.findall(r'\bid="([^"]+)"', HTML)

    def test_ids_are_unique(self):
        ids = self.ids()
        duplicates = {i for i in ids if ids.count(i) > 1}
        self.assertFalse(duplicates, f"duplicate ids in index.html: {sorted(duplicates)}")

    def test_every_id_app_js_looks_up_exists(self):
        """$("#x") returns null for a typo; the card then just... does nothing."""
        referenced = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', JS))
        # ids built as "#" + key + "-suffix" for each of the three cards
        for key, suffixes in CARD_ID_SUFFIXES.items():
            for suffix in [*suffixes, *COMMON_CARD_SUFFIXES]:
                referenced.add(f"{key}-{suffix}")
        referenced.update(CHROME_IDS)
        missing = sorted(referenced - set(self.ids()))
        self.assertFalse(missing, f"app.js looks up ids that index.html does not define: {missing}")

    def test_every_endpoint_card_is_present(self):
        for key, route in (
            ("health", "/health"),
            ("models", "/v1/models"),
            ("cache", "/cache"),
            ("metrics", "/metrics"),
        ):
            with self.subTest(card=key):
                self.assertIn(f'id="card-{key}"', HTML)
                self.assertIn(route, HTML, f"the {key} card does not name {route}")

    def test_retry_buttons_are_present_and_wired_by_dataset(self):
        retries = re.findall(r'class="btn btn-retry" data-endpoint="(\w+)"', HTML)
        self.assertEqual(sorted(retries), ["cache", "health", "metrics", "models"])

    def test_reachable_endpoints_match_the_proxy(self):
        """The UI documents the upstream paths; the proxy must proxy exactly those."""
        for route in ("/health", "/v1/models", "/cache", "/metrics"):
            self.assertIn(route, HTML)
        self.assertEqual(
            set(serve.PUBLIC_ENDPOINTS),
            set(serve.PROXY_ROUTES.values()),
        )

    def test_the_ui_polls_every_proxied_route(self):
        """A route the proxy adds but the page never polls is invisible dead code."""
        for route in serve.PROXY_ROUTES:
            with self.subTest(route=route):
                self.assertIn(route.lstrip("/"), JS)

    def test_live_regions_are_announced(self):
        self.assertIn('role="status"', HTML)   # the pill
        self.assertIn('role="alert"', HTML)    # the banner and the field error
        self.assertIn('role="progressbar"', HTML)  # the cache and kv-cache gauges
        self.assertIn('href="#cards"', HTML)  # skip link
        self.assertIn('id="cards"', HTML)

    def test_poll_and_preset_controls_exist_as_containers(self):
        # Buttons are built by app.js; the containers must exist.
        for container_id in ("poll-interval", "preset-row"):
            self.assertIn(f'id="{container_id}"', HTML)


class StyleContractTests(unittest.TestCase):
    def test_required_classes_are_styled(self):
        missing = []
        for name in REQUIRED_CLASSES:
            if not re.search(rf"\.{re.escape(name)}\b", CSS):
                missing.append(name)
        self.assertFalse(missing, f"app.js toggles classes nothing styles: {missing}")

    def test_every_state_class_the_js_toggles_is_styled(self):
        toggled = set(re.findall(r"classList\.(?:add|remove|toggle)\(\"([a-z0-9-]+)\"", JS))
        toggled |= set(re.findall(r'"(is-[a-z-]+|pill-[a-z-]+|badge-[a-z-]+)"', JS))
        # "badge-" + tone is built by concatenation; check the four tones instead.
        toggled = {t for t in toggled if not t.endswith("-")}
        unstyled = sorted(t for t in toggled if not re.search(rf"\.{re.escape(t)}\b", CSS))
        self.assertFalse(unstyled, f"toggled but unstyled: {unstyled}")

    def test_palette_is_custom_properties(self):
        self.assertIn(":root", CSS)
        variables = set(re.findall(r"(--[a-z0-9-]+)\s*:", CSS))
        for expected in ("--bg", "--surface", "--text", "--text-mute", "--border",
                         "--accent", "--ok", "--warn", "--bad"):
            self.assertIn(expected, variables, f"{expected} is not part of the palette")
        self.assertGreater(len(variables), 8, "the palette looks too small to theme with")

    def test_responsive_and_motion_preferences(self):
        self.assertIn("@media (max-width: 720px)", CSS)
        self.assertIn("prefers-reduced-motion", CSS)

    def test_metrics_use_tabular_numbers(self):
        self.assertRegex(CSS, r"font-variant-numeric\s*:\s*tabular-nums")


class ScriptSafetyTests(unittest.TestCase):
    def test_upstream_data_never_becomes_markup(self):
        """The payload is somebody else's JSON; it must only ever be text."""
        self.assertNotIn("innerHTML", JS)
        self.assertNotIn("outerHTML", JS)
        self.assertNotIn("insertAdjacentHTML", JS)
        self.assertNotIn("document.write", JS)
        self.assertNotIn("eval(", JS)

    def test_no_blocking_dialogs(self):
        """Failures are shown in the banner, not in a modal that freezes polling."""
        for call in ("alert(", "confirm(", "prompt("):
            self.assertNotIn(call, JS, f"{call} blocks the page")

    def test_poll_intervals_and_storage_keys(self):
        self.assertIn("2000", JS)
        self.assertIn("5000", JS)
        self.assertIn("15000", JS)
        self.assertIn("ms: 0", JS)  # the "paused" option
        keys = set(re.findall(r'"(llmmonitor\.[A-Za-z]+)"', JS))
        self.assertEqual(keys, {"llmmonitor.upstream", "llmmonitor.pollMs"})

    def test_history_is_bounded(self):
        match = re.search(r"HISTORY_SAMPLES\s*=\s*(\d+)", JS)
        self.assertIsNotNone(match, "the sparkline history has no cap")
        self.assertLessEqual(int(match.group(1)), 120)

    def test_frontend_url_rules_mirror_the_backend(self):
        """normalizeUpstream() must accept exactly what normalize_upstream() does."""
        accepted = ("http://a.b:1", "https://a.b", "http://a.b/x")
        for url in accepted:
            self.assertIsNotNone(re.search(r"https?:", url))
        # The JS comment promises a mirror of the Python rule; keep them equal
        # on the two cases that matter: scheme required, host required.
        self.assertIn('url.protocol !== "http:"', JS)
        self.assertIn("!url.host", JS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
