#!/usr/bin/env python3
"""Parity harness: what the bespoke page shows vs what Grafana's PromQL says.

This is the machine behind `monitoring/PARITY.md` (workspace-szw.11). It exists
because "the dashboard and the page agree" is not a thing anyone can assert from
memory: the two sides read the same exposition text through different math, and
every difference between them has to be *named* before the page's arithmetic can
be deleted.

Three modes:

    # 1. record paired samples while both views are live (the parallel run)
    python3 monitoring/parity/parity_check.py record \
        --upstream http://llm.home:8080 --prometheus http://127.0.0.1:9090 \
        --interval 15 --duration 7d --out monitoring/parity/runs/live

    # 2. probe the delta classes that a single quiet window cannot show
    python3 monitoring/parity/parity_check.py probe \
        --upstream http://llm.home:8080 --prometheus http://127.0.0.1:9090 \
        --fixture-fanout http://127.0.0.1:8111 --fixture-nan http://127.0.0.1:8112

    # 3. selftest: the page-side model, offline, no Prometheus and no server
    python3 monitoring/parity/parity_check.py selftest

How a "delta" is decided, and why that is a fair test
----------------------------------------------------
For every figure the harness computes three numbers per sample:

* ``page``   — what the bespoke page renders, produced from the same parser the
               proxy hands the browser (``monitor/serve.py:parse_prometheus``)
               and the same lookup-and-sum rules as ``app.js``.
* ``same``   — Grafana's expression that reads the *identical raw series* the tile
               reads (panels 1/2 plot the server's average gauge beside the rate;
               panels 20/21 are the same gauges). This is the arithmetic-parity
               check: if `page` and `same` differ, one of the two sides has a bug,
               and nothing may be deleted.
* ``panel``  — Grafana's canonical expression for that figure (windowed ``rate()``
               over ``$__rate_interval`` and the recording rules' 5m). This is
               allowed to differ, and every difference must fall into one of the
               named delta classes (window choice, counter reset, lifetime-vs-window,
               label fan-out, NaN handling, scrape skew).
* ``as_promql`` — the page's own arithmetic, written in PromQL with no window
               (``sum(a)/sum(b)`` of the instantaneous counters). If `page` ==
               `as_promql` then any `page` vs `panel` delta is *provably* the
               windowing and not the division, which is what licenses deleting
               the division from the browser.

Every class a sample falls into is counted, and the run fails (`--strict`) if a
delta is left unclassified: an unexplained delta is the ticket's stop condition.

Stdlib only, like the rest of this repo.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
sys.path.insert(0, str(REPO_ROOT / "monitor"))

import serve  # noqa: E402  (the parser the page itself uses — not a second parser)

DASHBOARD = REPO_ROOT / "monitoring" / "grafana" / "dashboards" / "llm-server.json"
DEFAULT_WINDOWS = ("1m", "5m", "15m")

# A sample counts as "identical" when it is within this much of the other side,
# relative. The scrapes are not taken at the same instant (the page and Prometheus
# each poll on their own 15 s tick), so an exact-equality test would report noise as
# a bug. 1% is far below any difference a human would notice on these tiles and far
# above float noise.
REL_TOLERANCE = 0.01
ABS_TOLERANCE = 0.01


# --------------------------------------------------------------------------- #
# the figures both views have                                                           #
# --------------------------------------------------------------------------- #

#: Metric names the page looks up by exact name (pre-deletion set, including the
#: counters only the deleted ratio math used). Names come from what the box
#: actually serves; see monitor/README.md "Why /metrics is parsed server-side".
NAMES = {
    "decode_gauge": "llamacpp:predicted_tokens_seconds",
    "prompt_gauge": "llamacpp:prompt_tokens_seconds",
    "processing": "llamacpp:requests_processing",
    "deferred": "llamacpp:requests_deferred",
    "requests": "halogen:requests_total",
    "prompt_tokens": "llamacpp:prompt_tokens_total",
    "gen_tokens": "llamacpp:tokens_predicted_total",
    "gen_seconds": "llamacpp:tokens_predicted_seconds_total",
    "cached_prompt": "halogen:prompt_tokens_cached_total",
    "draft_total": "halogen:draft_tokens_total",
    "draft_accepted": "halogen:draft_tokens_accepted_total",
    "kv_ratio": "llamacpp:kv_cache_usage_ratio",
    "kv_tokens": "llamacpp:kv_cache_tokens",
    "kv_pool": "halogen:kv_pool_positions",
}

#: Grafana expression selectors for the same figures, taken verbatim from the
#: dashboard's own union selectors (vendor drift lives there, not here).
FAMILIES = {
    "decode_tokens": "llamacpp:tokens_predicted_total|llamacpp:predicted_tokens_total|vllm:generation_tokens_total",
    "prompt_tokens": "llamacpp:prompt_tokens_total|vllm:prompt_tokens_total",
    "requests": "halogen:requests_total|llamacpp:requests_total|vllm:request_success_total",
    "cached_prompt": "halogen:prompt_tokens_cached_total|llamacpp:prompt_cache_tokens_cached_total",
    "draft_total": "halogen:draft_tokens_total|llamacpp:draft_tokens_total",
    "draft_accepted": "halogen:draft_tokens_accepted_total|llamacpp:draft_tokens_accepted_total",
    "kv_ratio": "llamacpp:kv_cache_usage_ratio|llamacpp:kv_cache_usage",
    "processing": "llamacpp:requests_processing|halogen:requests_running|vllm:num_requests_running",
}

#: Every figure the two views share, with how the page derives it and which panel
#: is the Grafana answer. ``class`` is the delta class a difference is expected to
#: fall into; ``delete`` marks the arithmetic this ticket removes from app.js.
FIGURES = (
    {
        "key": "decode_tok_s",
        "title": "decode tok/s",
        "page": "the server's own avg-since-last-scrape gauge, read as-is",
        "kind": "server-avg gauge",
        "expect": "window",
        "panel": 22,
        "panel_legend": "decode tok/s",
        "same_panel": 1,
        "same_legend": "server gauge",
        "delete": None,
    },
    {
        "key": "prompt_tok_s",
        "title": "prompt tok/s",
        "page": "the server's own avg-since-last-scrape gauge, read as-is",
        "kind": "server-avg gauge",
        "expect": "window",
        "panel": 2,
        "panel_legend": "prompt tok/s",
        "same_panel": 2,
        "same_legend": "server gauge",
        "delete": None,
    },
    {
        "key": "requests_in_flight",
        "title": "requests in flight",
        "page": "instant gauge, summed over label sets",
        "kind": "instant gauge",
        "expect": "exact",
        "panel": 20,
        "panel_legend": "processing",
        "same_panel": 20,
        "same_legend": "processing",
        "delete": None,
    },
    {
        "key": "kv_usage_pct",
        "title": "KV cache usage",
        "page": "instant ratio gauge (or tokens/pool when no ratio is published)",
        "kind": "instant gauge",
        "expect": "exact",
        "panel": 21,
        "panel_legend": "usage",
        "same_panel": 21,
        "same_legend": "usage",
        # the panel plots a 0..1 ratio, the tile renders a percentage: the harness
        # compares numbers, so it converts.
        "scale": 100.0,
        "delete": None,
    },
    {
        "key": "draft_accept_pct",
        "title": "draft acceptance",
        "page": "accepted/drafted counters since server start",
        "kind": "lifetime ratio of two counters",
        "expect": "lifetime-vs-window",
        "panel": 24,
        "panel_legend": "accept ratio",
        "as_promql": (
            "100 * (sum(@FAM.draft_accepted@) / sum(@FAM.draft_total@))"
        ),
        "scale": 100.0,
        "delete": "acceptPct",
    },
    {
        "key": "cache_hit_pct",
        "title": "prompt-cache hit ratio",
        "page": "cached/(processed+cached) counters since server start",
        "kind": "lifetime ratio of two counters",
        "expect": "lifetime-vs-window",
        "panel": 23,
        "panel_legend": "cache hit ratio",
        "as_promql": (
            "100 * (sum(@FAM.cached_prompt@) / "
            "(sum(@FAM.prompt_tokens@) + sum(@FAM.cached_prompt@)))"
        ),
        "scale": 100.0,
        "delete": "cacheHitPct",
    },
    {
        "key": "lifetime_decode_tok_s",
        "title": "lifetime decode tok/s",
        "page": "tokens/seconds counters since server start",
        "kind": "lifetime ratio of two counters",
        "expect": "lifetime-vs-window",
        "panel": 29,
        "panel_legend": "windowed rate for comparison",
        "same_panel": 29,
        "same_legend": "tokens/s since server start",
        "delete": "genLifetime",
    },
    {
        "key": "requests_total",
        "title": "requests completed",
        "page": "counter value, read as-is",
        "kind": "counter (not a rate)",
        "expect": "unit-change",
        "panel": 26,
        "panel_legend": "requests/s",
        # the counter total is what the tile shows and what Prometheus stores; the
        # panel that reads it shows a rate, so this row is a unit change, not a bug.
        "as_promql": "sum(@FAM.requests@)",
        "delete": None,
    },
)

FIGURE_BY_KEY = {f["key"]: f for f in FIGURES}

#: The figures the page renders after workspace-szw.11: everything with `delete`
#: unset. `page_figures` still computes the deleted ones, because the run that
#: justifies the deletion has to be able to show them; this is the set that says
#: which ones the page is actually responsible for now.
SURVIVING_FIGURES = frozenset(f["key"] for f in FIGURES if not f["delete"])

#: Which Grafana window(s) each figure is compared over. The panels use
#: $__rate_interval (Grafana resolves it to ~4x the scrape interval, i.e. ~60 s at
#: this stack's 15 s scrape) and the recording rules use a fixed 5m; measuring
#: several windows keeps the conclusion from depending on which one a given panel
#: resolves to.
COMPARE_WINDOWS = DEFAULT_WINDOWS


# --------------------------------------------------------------------------- #
# the page side                                                                        #
# --------------------------------------------------------------------------- #

def scrape(upstream: str, timeout: float = 8.0) -> dict:
    """Fetch `<upstream>/metrics` and parse it exactly like the proxy does."""
    url = upstream.rstrip("/") + "/metrics"
    with urllib.request.urlopen(url, timeout=timeout) as res:
        text = res.read().decode("utf-8", "replace")
    parsed = serve.parse_prometheus(text)
    by_name: dict[str, list] = {}
    for s in parsed["series"]:
        by_name.setdefault(s["name"], []).append(s)
    return {"text": text, "series": by_name, "count": parsed["count"],
            "errors": parsed["errors"], "truncated": parsed["truncated"]}


def sum_name(series_by_name: dict, name: str):
    """app.js signalValue(): sum a family across label sets, skipping non-finite.

    NaN/+Inf arrive as value=null + value_text from serve.parse_prometheus, and
    `signalValue` only adds entries whose value is a number — so a NaN series
    contributes nothing here, which is itself one of the delta classes.
    """
    hits = series_by_name.get(name)
    if not hits:
        return None
    total, counted = 0.0, 0
    for sample in hits:
        value = sample.get("value")
        if isinstance(value, (int, float)) and math.isfinite(value):
            total += value
            counted += 1
    return total if counted else None


def ratio(numerator, denominator):
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def page_figures(scraped: dict) -> dict:
    """Every figure the bespoke page shows, computed the way app.js shows it."""
    s = scraped["series"]
    get = lambda key: sum_name(s, NAMES[key])  # noqa: E731

    out = {
        "decode_tok_s": get("decode_gauge"),
        "prompt_tok_s": get("prompt_gauge"),
        "requests_in_flight": get("processing"),
        "requests_total": get("requests"),
        "kv_usage_pct": None,
        "draft_accept_pct": None,
        "cache_hit_pct": None,
        "lifetime_decode_tok_s": None,
    }

    # metricsGauge(): the reported ratio if there is one, else used/pool.
    kv_ratio = get("kv_ratio")
    kv_tokens, kv_pool = get("kv_tokens"), get("kv_pool")
    if kv_ratio is not None:
        out["kv_usage_pct"] = kv_ratio * 100 if kv_ratio <= 1 else kv_ratio
    elif kv_tokens is not None and kv_pool:
        out["kv_usage_pct"] = (kv_tokens / kv_pool) * 100

    # The three derived tiles (deleted by this ticket; kept here so the run
    # documents what was removed and how far it drifted from Grafana).
    accepted, drafted = get("draft_accepted"), get("draft_total")
    if accepted is not None and drafted:
        out["draft_accept_pct"] = min(max((accepted / drafted) * 100, 0), 999)
    seen, cached = get("prompt_tokens"), get("cached_prompt")
    if seen is not None and cached is not None and (seen + cached) > 0:
        out["cache_hit_pct"] = min(max((cached / (seen + cached)) * 100, 0), 999)
    gen_tokens, gen_seconds = get("gen_tokens"), get("gen_seconds")
    if gen_tokens is not None and gen_seconds:
        out["lifetime_decode_tok_s"] = gen_tokens / gen_seconds
    return out


# --------------------------------------------------------------------------- #
# the Grafana side                                                                     #
# --------------------------------------------------------------------------- #

def load_panel_targets(dashboard_path: Path = DASHBOARD) -> dict:
    """(panel id, legend substring) -> expression, straight from the dashboard."""
    doc = json.loads(dashboard_path.read_text(encoding="utf-8"))
    out = {}
    for panel in doc.get("panels", []):
        for target in panel.get("targets", []) or []:
            expr = target.get("expr")
            if not expr:
                continue
            legend = (target.get("legendFormat") or "").lower()
            out[(panel["id"], legend)] = expr
    return out


def find_target(targets: dict, panel_id: int, legend: str | None):
    """Pick a panel's target by legend text; the panel may hold several."""
    if legend is None:
        return None
    legend = legend.lower()
    for (pid, leg), expr in targets.items():
        if pid == panel_id and legend in leg:
            return expr
    return None


def instantiate(expr: str, *, instance: str, job: str = ".*", model: str = ".*",
                window: str) -> str:
    """Substitute the dashboard's template variables the way Grafana would.

    `job` and `model` are ".*", i.e. the dashboard's own default (All): the harness
    compares one server across all its models, exactly as the page does. A fan-out
    difference is a separate probe (P4), not something to hide behind a variable.
    """
    return (
        expr.replace("$__rate_interval", window)
        .replace('$job', job)
        .replace('$instance', prom_regex(instance))
        .replace('$model', model)
    )


def prom_range(prometheus: str, query: str, start: float, end: float, step: float,
               timeout: float = 15.0):
    """Range query -> the list of values that series took between start and end.

    Used for the skew test: "is the number on the page a value this series actually
    has in this time neighbourhood?" Two scrapers on the same target cannot read the
    same instant, so a gauge that swings between scrapes will not match value-for-
    value — and a comparison that demands it would report noise as a bug and make the
    whole run worthless.
    """
    params = {"query": query, "start": f"{start:.3f}", "end": f"{end:.3f}",
              "step": f"{max(step, 1.0):.0f}"}
    url = prometheus.rstrip("/") + "/api/v1/query_range?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            payload = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"prometheus rejected the range query ({err.code}): {detail}") from None
    if payload.get("status") != "success":
        raise RuntimeError(f"prometheus error: {payload.get('error') or payload}")
    values = []
    for series in payload["data"].get("result") or []:
        for _, raw in series.get("values") or []:
            value = float(raw)
            if math.isfinite(value):
                values.append(value)
    return values


def prom_regex(value: str) -> str:
    """Make a literal value safe as a PromQL regex, without invalid escapes.

    `llm.home:8080` is a label value, not a pattern: the dots must be literal. In a
    PromQL *string* a backslash-dot is a parse error ("unknown escape sequence"), so
    each metacharacter is put in a bracketed class instead — `[.]` means the same
    thing and survives the string lexer.
    """
    out = []
    for ch in str(value):
        out.append("[" + ch + "]" if ch in r"\.+*?()|[]{}^$" else ch)
    return "".join(out)


def prom_query(prometheus: str, query: str, when: float | None = None,
               timeout: float = 10.0):
    """One instant query -> (value, query). Value is None for "no datapoint".

    NaN and +Inf also come back as None on purpose: the page renders such a figure as
    an em-dash and Grafana renders it as No data, and the harness must not read NaN
    as the number zero (that conflation is delta class `nan-handling`).
    """
    params = {"query": query}
    if when is not None:
        params["time"] = f"{when:.6f}"
    url = prometheus.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            payload = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"prometheus rejected the query ({err.code}): {detail} "
                           f"--- query: {query}") from None
    if payload.get("status") != "success":
        raise RuntimeError(f"prometheus error: {payload.get('error') or payload}")
    result = payload["data"].get("result") or []
    if not result:
        return None, query
    value = float(result[0]["value"][1])
    return (None if not math.isfinite(value) else value), query


def panel_value(prometheus: str, figure: dict, targets: dict, *, instance: str,
                when: float, window: str):
    expr = find_target(targets, figure["panel"], figure.get("panel_legend"))
    if expr is None:
        return None, "no-panel", None
    rendered = instantiate(expr, instance=instance, window=window)
    value, kind = prom_query(prometheus, rendered, when)
    return scale_to(value, figure), kind, rendered


def scale_to(value, figure):
    """A panel that plots a fraction and a tile that prints a percentage are the same
    number; compare numbers, not spellings."""
    scale = figure.get("scale")
    if value is None or not scale or scale == 1:
        return value
    return value * scale


def same_source_value(prometheus: str, figure: dict, targets: dict, *, instance: str,
                      when: float, window: str):
    panel = figure.get("same_panel")
    if panel is None:
        return None, "no-panel", None
    expr = find_target(targets, panel, figure.get("same_legend"))
    if expr is None:
        return None, "no-panel", None
    rendered = instantiate(expr, instance=instance, window=window)
    value, kind = prom_query(prometheus, rendered, when)
    return scale_to(value, figure), kind, rendered


def render_family_template(template: str, *, instance: str) -> str:
    """Fill a `@FAM.name@` template with the family's union selector, filtered to
    one instance — the page's arithmetic written in PromQL, no window anywhere.

    Written with tokens rather than str.format() because PromQL is full of braces.
    """
    def one(token):
        family = FAMILIES[token]
        return '{__name__=~"%s",instance=~"%s"}' % (family, prom_regex(instance))

    return re.sub(r"@FAM\.([a-z_]+)@", lambda m: one(m.group(1)), template)


def as_promql_value(prometheus: str, figure: dict, *, instance: str, when: float):
    """The page's arithmetic written in PromQL, with no window at all."""
    template = figure.get("as_promql")
    if not template:
        return None, "no-template", None
    rendered = render_family_template(template, instance=instance)
    value, kind = prom_query(prometheus, rendered, when)
    return value, kind, rendered


# --------------------------------------------------------------------------- #
# one paired sample                                                                    #
# --------------------------------------------------------------------------- #

def take_sample(upstream: str, prometheus: str, targets: dict, *,
                instance: str | None = None, windows=COMPARE_WINDOWS,
                settle: float = 0.5, skew_span: float = 45.0,
                figures=None) -> dict:
    """Read the page side and the panel side of every figure, as close to the same
    instant as two independent scrape targets can be managed."""
    instance = instance or urllib.parse.urlparse(upstream).netloc
    scraped = scrape(upstream)
    page = page_figures(scraped)
    # Prometheus' instant query reads its *last* sample, which can be one scrape
    # old; querying at "now" (rather than at the scrape's own timestamp) is what a
    # human does when they look at the two screens side by side, so that skew is
    # part of what is being measured, not an artefact to remove.
    when = time.time()
    if settle:
        time.sleep(settle)

    row = {
        "t": when,
        "upstream": upstream,
        "instance": instance,
        "series": scraped["count"],
        "page": page,
        "prom": {},
        "exprs": {},
    }
    wanted = [FIGURE_BY_KEY[key] for key in figures] if figures else list(FIGURES)
    for figure in wanted:
        key = figure["key"]
        windows_out = {}
        kinds = {}
        for window in windows:
            value, kind, expr = panel_value(prometheus, figure, targets,
                                            instance=instance, when=when, window=window)
            windows_out[window] = value
            kinds[window] = kind
            if expr:
                row["exprs"][f"{key}:{window}"] = expr
        same, same_kind, same_expr = same_source_value(prometheus, figure, targets,
                                                       instance=instance, when=when,
                                                       window=windows[0])
        as_promql, as_promql_kind, as_promql_expr = as_promql_value(
            prometheus, figure, instance=instance, when=when)
        # The time neighbourhood of the series the tile reads: what Prometheus itself
        # recorded around now, which is how a skew gets told apart from a bug.
        neighbours = []
        if same_expr:
            neighbours = prom_range(prometheus, same_expr, when - skew_span, when,
                                    max(skew_span / 8, 1.0))
        row["prom"][key] = {
            "panel": windows_out,
            "kinds": kinds,
            "same": same,
            "same_kind": same_kind,
            "as_promql": as_promql,
            "as_promql_kind": as_promql_kind,
            "neighbours": neighbours,
        }
        if same_expr:
            row["exprs"][f"{key}:same"] = same_expr
        if as_promql_expr:
            row["exprs"][f"{key}:as_promql"] = as_promql_expr
    row["raw"] = {name: samples for name, samples in scraped["series"].items()}
    return row


# --------------------------------------------------------------------------- #
# delta classes                                                                        #
# --------------------------------------------------------------------------- #

#: Every way a page/panel difference is allowed to happen. A sample whose delta
#: does not fall into one of these is *unexplained*, and the run says so loudly.
CLASSES = {
    "exact": "same series, same math: any real delta is a bug on one side",
    "exact-with-skew": "same series, but the two scrapes were taken apart in time",
    "window": "the page reads the server's own average-since-scrape gauge, the panel "
              "computes rate() over a window: different quantities, both correct",
    "lifetime-vs-window": "the page divides counters accumulated since server start, "
                          "the panel divides rate() over a window: the page is a "
                          "running average that an idle minute cannot move",
    "unit-change": "the page shows a counter total, the panel shows a per-second rate: "
                   "different units, no agreement expected",
    "idle-zero": "the server reports 0 for its average gauge when nothing is running, "
                 "while rate() still spreads the last burst across the window",
    "counter-reset": "a counter that restarted: rate()/increase() adds the reset back, "
                     "the page's lifetime division silently treats it as a smaller "
                     "lifetime total",
    "label-fanout": "one metric name carrying several label sets: the page sums them "
                    "into one number, a panel filtered by $model/$job keeps them apart",
    "nan-handling": "NaN/+Inf: the parser gives the page value=null (rendered em-dash, "
                    "excluded from sums), PromQL keeps NaN (rendered No data)",
    "no-data": "one side has no datapoint at all",
    "clamp": "the panel's clamp_min() turns a 0/0 into 0 where the page renders nothing",
    "sum-vs-max": "both sides read the same family but aggregate differently: the page "
                  "sums every label set, the KV panel takes max()",
    "reader-reset": "llamacpp's `*_tokens_seconds` gauges average over the interval "
                    "since *whoever last read /metrics*, so the page's own poll "
                    "changes what Prometheus sees seconds later: the same quantity "
                    "differs between the two views by construction. Probe P7 measures "
                    "the spread on this box.",
    "blip-not-scraped": "a gauge excursion shorter than the scrape interval: the page, "
                        "polling faster than Prometheus, saw it; the TSDB never did. "
                        "The one direction the page can know more than Grafana.",
}


def relative_delta(a, b):
    if a is None or b is None:
        return None
    base = max(abs(a), abs(b))
    if base == 0:
        return 0.0
    return (b - a) / base


def within(a, b, tolerance=REL_TOLERANCE):
    """None when either side is missing, else True/False."""
    delta = relative_delta(a, b)
    if delta is None:
        return None
    return abs(delta) <= tolerance or abs(b - a) <= ABS_TOLERANCE


def plausible_for_series(value, neighbours, tolerance=REL_TOLERANCE):
    """Is `value` a number this series actually takes in the surrounding window?

    This is how a skew is told apart from a bug. The page and Prometheus cannot read
    the same target at the same instant, so a gauge that moves between scrapes must
    be compared against the range it occupies, not against one of its samples.
    True says "the difference is a time difference, not an arithmetic difference";
    False says the page is showing a number the series never was, which is exactly
    the bug this check exists to catch.
    """
    if not neighbours or value is None:
        return None
    low, high = min(neighbours), max(neighbours)
    pad = max(abs(high), abs(low), 1.0) * tolerance
    return (low - pad) <= value <= (high + pad)


#: The same-source difference a pair of poll intervals can explain: a value inside
#: the neighbourhood's own range is a plain skew, a value just outside it can be an
#: excursion the slower scraper missed, and a *reader-reset* gauge may legitimately
#: sit anywhere, because its value is a function of who read last (probe P7).
def gap_class(page, neighbours, figure):
    """Explain a same-series difference by *when* each side looked, if that is enough.

    Returns a class name, or None when the timing cannot explain it — which is how a
    real arithmetic difference still lands in the report as unexplained instead of
    being waved through by a generous tolerance.
    """
    if not neighbours or page is None:
        return None
    low, high = min(neighbours), max(neighbours)
    span = max(abs(high), abs(low), 1.0)
    pad = span * REL_TOLERANCE
    if (low - pad) <= page <= (high + pad):
        return "skew"
    nearest = min(neighbours, key=lambda value: abs(value - page))
    if abs(page - nearest) <= max(1.0, 0.05 * span):
        return "blip-not-scraped"
    if figure["kind"].startswith("server-avg"):
        return "reader-reset"
    return None


def classify(figure: dict, sample: dict, window: str, *, fanout_expected=False) -> dict:
    """Decide, for one figure/window pair, what kind of delta this is.

    Two validity checks run first, because a delta that is "explained" by a
    difference in definition only means something if neither side is simply wrong:

    `arith_check`  the page's own arithmetic, written as PromQL with no window, must
                   reproduce the page. If it does not, the harness' model of the page
                   is wrong (or the page has a real bug) and nothing gets deleted.
    `same_check`   the number on the page must be a number Grafana's own series has
                   around the same time — "equal", or "skew" (a time difference), or
                   "fanout" (both sides summed/maxed a multi-label family
                   differently), or MISMATCH.
    """
    prom = sample["prom"][figure["key"]]
    page = sample["page"].get(figure["key"])
    panel = prom["panel"].get(window)
    kind = prom.get("kinds", {}).get(window, "value")
    same = prom.get("same")
    same_kind = prom.get("same_kind", "value")
    as_promql = prom.get("as_promql")
    # Scaled like the panel value, in classify() rather than at record time: a gauge
    # the page renders as a percentage has to be compared against its neighbourhood in
    # percent, or every 0..1 ratio series reads as a mismatch. Every recorded KV row
    # of the first long run failed `--strict` for exactly that reason, and doing it
    # here is what lets `report --reclassify` revise that judgement afterwards.
    neighbours = [scale_to(value, figure) for value in (prom.get("neighbours") or [])]

    out = {
        "figure": figure["key"], "window": window,
        "page": page, "panel": panel, "same": same, "as_promql": as_promql,
        "delta": None, "delta_pct": None, "class": None,
        "arith_check": None, "same_check": None, "panel_kind": kind,
    }

    if page is not None and as_promql is not None:
        out["arith_check"] = within(page, as_promql)
    if page is not None and same is not None:
        if within(page, same):
            out["same_check"] = "equal"
        else:
            out["same_check"] = gap_class(page, neighbours, figure) or (
                "fanout" if fanout_expected else "MISMATCH")
    elif same_kind == "nan":
        out["same_check"] = "nan"

    if out["arith_check"] is False:
        out["class"] = "UNEXPLAINED: the page disagrees with its own arithmetic in PromQL"
        return out
    if out["same_check"] == "MISMATCH":
        out["class"] = "UNEXPLAINED: the page shows a number Grafana's series never had"
        return out
    if out["same_check"] == "fanout":
        out["class"] = "sum-vs-max"
        return out
    if out["same_check"] in ("reader-reset", "blip-not-scraped"):
        # The tile and the panel read one series and got different numbers for a
        # timing reason. That *is* the finding for this sample; the page-vs-panel
        # delta is not worth a separate class when the two are the same quantity.
        out["delta"] = None
        out["class"] = out["same_check"]
        return out

    if figure["expect"] == "unit-change":
        if page is not None and panel is not None:
            out["delta"] = panel - page
            out["delta_pct"] = relative_delta(page, panel)
        out["class"] = "unit-change"
        return out

    if page is None or panel is None:
        out["class"] = "nan-handling" if (kind == "nan" or same_kind == "nan") else "no-data"
        return out

    out["delta"] = panel - page
    out["delta_pct"] = relative_delta(page, panel)

    if within(page, panel):
        out["class"] = "exact"
        return out
    if figure["expect"] == "exact":
        out["class"] = "exact-with-skew" if plausible_for_series(page, neighbours) \
            else "UNEXPLAINED: two views of one gauge disagreed with no skew to explain it"
        return out
    if page == 0 and figure["kind"].startswith("server-avg"):
        out["class"] = "idle-zero"
        return out
    if figure["expect"] == "lifetime-vs-window":
        out["class"] = "clamp" if (panel == 0 and page != 0) else "lifetime-vs-window"
        return out
    if figure["expect"] == "window":
        out["class"] = "window"
        return out
    out["class"] = "UNEXPLAINED: no class for this delta"
    return out


# --------------------------------------------------------------------------- #
# modes                                                                                #
# --------------------------------------------------------------------------- #

def parse_duration(text: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r}")
    value = float(match.group(1))
    return value * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]


def cmd_record(args) -> int:
    targets = load_panel_targets()
    stem = Path(args.out)
    stem.parent.mkdir(parents=True, exist_ok=True)
    samples_path = stem.with_name(stem.name + ".jsonl")
    meta_path = stem.with_name(stem.name + ".meta.json")
    window_s = parse_duration(args.duration)
    meta = {
        "started": time.time(),
        "upstream": args.upstream,
        "prometheus": args.prometheus,
        "interval_s": args.interval,
        "planned_duration_s": window_s,
        "windows": list(args.windows),
        # the dashboard's (panel id, legend) -> expr map, keyed as a string so it
        # survives JSON: this is the evidence of *which* panel query each figure was
        # compared against.
        "expressions": {f"panel {pid} :: {legend}": expr
                        for (pid, legend), expr in targets.items() if legend},
        "note": "scrape_interval on the Prometheus side is what makes the page's "
                "instant-vs-window deltas; recorded here so the run can be re-read "
                "against the config that produced it.",
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    deadline = time.time() + window_s
    count = 0
    problems = []
    raw_path = stem.with_name(stem.name + ".raw.jsonl")
    with samples_path.open("a", encoding="utf-8") as sink, raw_path.open("a", encoding="utf-8") as raw_sink:
        while time.time() < deadline:
            started = time.time()
            try:
                sample = take_sample(args.upstream, args.prometheus, targets,
                                     windows=args.windows,
                                     skew_span=3 * args.scrape_interval)
            except (urllib.error.URLError, ConnectionError, RuntimeError, OSError) as err:
                problems.append(f"{time.strftime('%F %T')} read failed: {err}")
                print(f"[record] read failed: {err}", file=sys.stderr)
                time.sleep(args.interval)
                continue
            # The scrape itself goes to its own file: one row per sample rather than
            # one per figure/window pair, so the classified file stays a table a
            # human can read and the raw evidence stays available offline.
            raw_sink.write(json.dumps({"t": sample["t"], "raw": sample["raw"],
                                       "prom": sample["prom"], "page": sample["page"],
                                       "exprs": sample["exprs"]}, sort_keys=True) + "\n")
            raw_sink.flush()
            for figure in FIGURES:
                for window in args.windows:
                    row = classify(figure, sample, window)
                    row["t"] = sample["t"]
                    sink.write(json.dumps(row, sort_keys=True) + "\n")
                    if row["class"].startswith("UNEXPLAINED"):
                        problems.append(f"{row['figure']}/{window}: {row['class']} "
                                        f"page={row['page']} panel={row['panel']}")
            sink.flush()
            count += 1
            if args.max_samples and count >= args.max_samples:
                break
            elapsed = time.time() - started
            if args.interval - elapsed > 0:
                time.sleep(args.interval - elapsed)
    meta.update({"finished": time.time(), "samples": count, "problems": problems})
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"[record] {count} paired samples -> {samples_path}")
    for problem in problems[:20]:
        print(f"[record] UNEXPLAINED: {problem}", file=sys.stderr)
    return 1 if (problems and args.strict) else 0


def read_samples(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def reclassify_rows(samples_raw: Path) -> list[dict]:
    """Re-run classify() over a recorded run, with the classifier as it is now.

    Classification is decided at record time, so a classifier fix would otherwise
    throw a long run away. `samples.raw.jsonl` keeps the page numbers, the panel
    numbers and the neighbourhood, which is everything classify() reads — so the
    judgement can be revised without repeating the week, and the raw evidence stays
    the raw evidence.
    """
    targets = load_panel_targets()
    del targets  # only the stored per-figure expressions matter here
    rows = []
    with samples_raw.open(encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line:
                continue
            sample = json.loads(line)
            for figure in FIGURES:
                for window in (sample["prom"].get(figure["key"], {})
                               .get("panel") or {}):
                    row = classify(figure, sample, window)
                    row["t"] = sample["t"]
                    rows.append(row)
    return rows


def cmd_report(args) -> int:
    if args.reclassify:
        raw = Path(args.samples).with_name(Path(args.samples).stem + ".raw.jsonl")
        if not raw.exists():
            print(f"--reclassify needs {raw}", file=sys.stderr)
            return 2
        rows = reclassify_rows(raw)
    else:
        rows = read_samples(Path(args.samples))
    if not rows:
        print("no samples", file=sys.stderr)
        return 1
    per: dict[tuple, list[dict]] = {}
    for row in rows:
        per.setdefault((row["figure"], row["window"]), []).append(row)

    started = min(r["t"] for r in rows)
    finished = max(r["t"] for r in rows)
    if not getattr(args, "json", False):
        print(f"# paired samples: {len(rows)} rows over {(finished - started)/3600:.2f} h "
              f"({time.strftime('%F %T', time.localtime(started))} -> "
              f"{time.strftime('%F %T', time.localtime(finished))})\n")

    if args.csv:
        with Path(args.csv).open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=["figure", "window", "page", "panel", "same",
                                "as_promql", "delta", "delta_pct", "class", "t"],
                extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        if not getattr(args, "json", False):
            print(f"wrote {args.csv}\n")

    summary = []
    for (figure, window), group in sorted(per.items()):
        spec = FIGURE_BY_KEY[figure]
        deltas = [r["delta_pct"] for r in group if r["delta_pct"] is not None]
        finite_pages = [r["page"] for r in group if r["page"] is not None]
        finite_panels = [r["panel"] for r in group if r["panel"] is not None]
        summary.append({
            "title": f"{spec['title']}{' [deleted]' if spec['delete'] else ''}",
            "figure": f"{spec['title']}{' [deleted]' if spec['delete'] else ''}",
            "key": figure,
            "window": window,
            "n": len(group),
            "classes": tally_by(group, "class"),
            "page": round(mean(finite_pages), 3),
            "panel": round(mean(finite_panels), 3),
            "delta_pct_mean": round(100 * mean(deltas), 3),
            "delta_pct_worst": round(100 * max((abs(d) for d in deltas), default=0), 3),
            "same_check": tally_by(group, "same_check"),
            "arith_check": tally_by(group, "arith_check"),
        })

    unexplained = sorted({r["class"] for r in rows
                          if r["class"].startswith("UNEXPLAINED")})
    if args.json:
        # Machine-readable, for monitoring/parity/fill_parity_doc.py: the write-up
        # quotes these numbers, so it must not be reading them off a text table.
        print(json.dumps({
            "rows": len(rows),
            "started": started,
            "finished": finished,
            "summary": summary,
            "classes": dict(sorted(CLASSES.items())),
            "unexplained": unexplained,
            "verdict": ("UNEXPLAINED deltas: " + ", ".join(unexplained))
                       if unexplained else
                       "No unexplained deltas: every paired difference fell into a "
                       "named class.",
        }, indent=2, sort_keys=True))
        return 1 if unexplained else 0

    header = ("figure", "window", "n", "class tally", "page (mean)", "panel (mean)",
              "delta % (mean / max)", "same-source check", "arith check")
    lines = [header]
    for row in summary:
        lines.append((
            row["figure"], row["window"], str(row["n"]), joined_tally(row["classes"]),
            row["page"], row["panel"],
            f"{row['delta_pct_mean']:+.2f}% / {row['delta_pct_worst']:.2f}%",
            joined_tally(row["same_check"]), joined_tally(row["arith_check"]),
        ))
    widths = [max(len(str(line[i])) for line in lines) for i in range(len(header))]
    for line in lines:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(line)))

    print()
    for line in sorted(CLASSES.items()):
        print(f"- **{line[0]}**: {line[1]}")
    if unexplained:
        print("\nUNEXPLAINED (the ticket's stop condition):")
        for cls in unexplained:
            print(f"  - {cls}")
        return 1
    print("\nNo unexplained deltas: every paired difference fell into a named class.")
    return 0


def tally_by(rows, key):
    out: dict = {}
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        label = str(value)
        out[label] = out.get(label, 0) + 1
    return out


def joined_tally(counts):
    return ", ".join(f"{k}×{v}"
                     for k, v in sorted(counts.items(), key=lambda kv: -kv[1])) or "\u2014"


def mean(values):
    return sum(values) / len(values) if values else 0.0


def fmt(value, unit=""):
    if value is None:
        return "no datapoint"
    return f"{value:.3f}{unit}"


def cmd_probe(args) -> int:
    """The delta classes a single quiet window cannot show."""
    targets = load_panel_targets()
    report: dict[str, dict] = {}

    # -- P1 window choice: the same counter, four windows, under load -----------
    windows = ["1m", "5m", "15m", "1h"]
    sample = take_sample(args.upstream, args.prometheus, targets, windows=windows)
    report["window_choice"] = {
        "what": "one counter, four rate windows, compared with the server's own "
                "average gauge (the tile) and with the recording rule's fixed 5m",
        "page_gauge": sample["page"]["decode_tok_s"],
        "panel": sample["prom"]["decode_tok_s"]["panel"],
        "rule_5m_is_the_panel_5m": sample["prom"]["decode_tok_s"]["panel"]["5m"],
        "class": "window",
    }

    # -- P2 lifetime vs window: the deleted ratio math -------------------------
    for key in ("draft_accept_pct", "cache_hit_pct"):
        figure = FIGURE_BY_KEY[key]
        row = take_sample(args.upstream, args.prometheus, targets,
                          windows=("1m", "5m", "1h"))
        report[key] = {
            "page_lifetime": row["page"][key],
            "same_math_as_promql": row["prom"][key]["as_promql"],
            "panel_windowed": row["prom"][key]["panel"],
            "class": "lifetime-vs-window",
        }

    # -- P3 counter reset: rate() recovers, a lifetime division does not --------
    report["counter_reset"] = counter_reset_probe(args, targets)

    # -- P4 label fan-out (needs a server that fans out; probe mode optional) ---
    if args.fixture_fanout:
        fan = urllib.parse.urlparse(args.fixture_fanout).netloc
        sample = take_sample(args.fixture_fanout, args.prometheus, targets,
                             instance=fan, windows=("5m",))
        s = scrape(args.fixture_fanout)["series"]
        per_label = {}
        for name, hits in s.items():
            if len(hits) > 1:
                per_label[name] = [
                    {"labels": h["labels"], "value": h["value"], "text": h["value_text"]}
                    for h in hits
                ]
        report["label_fanout"] = {
            "what": "one metric name, several label sets: the page's signalValue() "
                    "sums them into one figure; panels aggregate differently (the KV "
                    "panel takes max, the throughput panels sum) and $model can split "
                    "them further",
            "instance": fan,
            "families_with_several_label_sets": per_label,
            "page_figures": sample["page"],
            "panel_figures": {k: v["panel"] for k, v in sample["prom"].items()},
            "classes": [classify(f, sample, "5m", fanout_expected=True)
                        for f in FIGURES],
            "class": "label-fanout",
        }

    # -- P5 NaN / +Inf ----------------------------------------------------------
    if args.fixture_nan:
        nan = urllib.parse.urlparse(args.fixture_nan).netloc
        sample = take_sample(args.fixture_nan, args.prometheus, targets,
                             instance=nan, windows=("5m",))
        report["nan_handling"] = {
            "what": "NaN and +Inf in the exposition text: JSON cannot spell them, so "
                    "the parser hands the page value=null + value_text and app.js "
                    "skips them in sums; PromQL keeps NaN and the panel renders "
                    "'No data'",
            "instance": nan,
            "non_finite_series": [
                {"name": name, "value_text": hits[0].get("value_text"),
                 "value": hits[0].get("value"), "n": len(hits)}
                for name, hits in scrape(args.fixture_nan)["series"].items()
                if any(h["value"] is None for h in hits)
            ],
            "page_figures": sample["page"],
            "panel_figures": {k: v["panel"] for k, v in sample["prom"].items()},
            "panel_kinds": {k: v.get("kinds") for k, v in sample["prom"].items()},
            "classes": [classify(f, sample, "5m") for f in FIGURES],
            "class": "nan-handling",
        }

    # -- P6 idle-zero: the average gauge drops to 0 between requests ------------
    report["idle_zero"] = idle_zero_probe(args, targets)

    # -- P7 the gauge's own window is reset by every reader --------------------
    report["gauge_window_reset"] = gauge_reset_probe(args)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


RESET_TRUTH_PCT = 80.0        # mock_upstream --mode reset: 80 accepted per 100 drafted
RESET_CACHE_TRUTH_PCT = 90.0  # ... and the cache covers 90 of every 100 tokens

RESET_PROOF = ("monitor/tests/mock_upstream.py --mode reset: draft_tokens_accepted_total "
               "restarts every 45 s under a draft_tokens_total that keeps climbing, so "
               "the truth is 80% by construction and stays 80% while the scraped "
               "counters do not")


def counter_reset_probe(args, targets) -> dict:
    """Watch a counter reset happen, and see which of the two maths survives it.

    A live server does not obligingly restart inside a measurement window, so the
    reset is driven by a double rather than waited for. Both readings come from the
    same Prometheus at the same instant: the page's arithmetic on the scraped counter
    values, and the dashboard's own expression for the same ratio.
    """
    out = {
        "what": "a counter that restarts: rate() adds the pre-reset tokens back into "
                "the window, a division of two lifetime counters silently treats the "
                "restart as a shorter lifetime and reads low forever after",
        "truth_pct": RESET_TRUTH_PCT,
        "proof": RESET_PROOF,
        "class": "counter-reset",
    }
    fixture = getattr(args, "fixture_reset", "")
    if not fixture:
        out["observed"] = None
        out["note"] = "not observed: run with --fixture-reset <url>"
        return out
    try:
        row = take_sample(fixture, args.prometheus, targets,
                          instance=urllib.parse.urlparse(fixture).netloc,
                          windows=("1m", "5m", "15m"),
                          figures=("draft_accept_pct", "cache_hit_pct"))
    except (RuntimeError, OSError, ValueError) as exc:
        out["observed"] = None
        out["note"] = f"not observed: {exc}"
        return out
    panel = row["prom"]["draft_accept_pct"]["panel"]
    page = row["page"]["draft_accept_pct"]
    cache_panel = row["prom"]["cache_hit_pct"]["panel"] if "cache_hit_pct" in row["prom"] else {}
    drafted = (scrape(fixture)["series"].get("halogen:draft_tokens_total") or [{}])[0].get("value")
    out["observed"] = {
        "fixture": fixture,
        # drafted grows at a known 100/s, so the counters say how old the fixture is.
        "fixture_age_s": None if drafted is None else drafted / 100.0,
        "page_lifetime_pct": page,
        "panel_windowed_pct": panel.get("5m"),
        "panel_windows_pct": panel,
        "page_cache_hit_pct": row["page"].get("cache_hit_pct"),
        "panel_cache_hit_pct": cache_panel.get("5m"),
        "truth_cache_hit_pct": RESET_CACHE_TRUTH_PCT,
    }
    if page is not None and panel.get("5m") is not None:
        out["difference"] = abs(page - panel["5m"])
    out["explanation"] = (
        "The page divides two instantaneous counter values, so as soon as the "
        f"numerator restarts it reports acceptance since that reset — here {fmt(page)}% "
        f"against a true {RESET_TRUTH_PCT:.0f}%, and it keeps decaying. rate() puts the "
        f"dropped back, so the panel reads {fmt(panel.get('5m'))}% and stays there. The "
        "page's figure never recovers: a lifetime division has no window to forget "
        "into, which is the whole reason the tile was deleted rather than tuned.")
    return out


def idle_zero_probe(args, targets) -> dict:
    """Two gauges, one quiet box: the tile goes to 0 while rate() still shows work."""
    try:
        sample = take_sample(args.upstream, args.prometheus, targets,
                             windows=("1m", "5m"))
    except (urllib.error.URLError, ConnectionError, RuntimeError, OSError) as err:
        return {"error": str(err), "class": "no-data"}
    return {
        "what": "llamacpp:*_tokens_seconds are 'average since my last scrape' gauges: "
                "an idle server reports 0, and every scrape resets the window, so the "
                "tile reads 0 while rate() over a window still shows the last burst",
        "page_gauge": sample["page"]["decode_tok_s"],
        "panel": sample["prom"]["decode_tok_s"]["panel"],
        "class": "idle-zero",
    }


def gauge_reset_probe(args, samples: int = 8, pause: float = 1.0) -> dict:
    """`llamacpp:*_tokens_seconds` is "average since somebody last read me".

    Every read of /metrics resets the server's own window, so the number a reader
    sees depends on who else is reading. Start one long decode, poll the gauge once
    a second, and the same quantity in the same second of wall clock ranges over a
    factor of several — while the counters, which is what Prometheus uses, climb
    monotonically. That is the clearest possible answer to "which number should the
    operator trust".
    """
    import threading

    error = [None]

    def busy():
        try:
            probe_completion(args.upstream, max_tokens=400, timeout=180)
        except Exception as err:  # noqa: BLE001 - a failed probe is a result too
            error[0] = str(err)

    thread = threading.Thread(target=busy, daemon=True)
    thread.start()
    seen: list[float] = []
    counters: list = []
    deadline = time.time() + 60
    while thread.is_alive() and len(seen) < samples and time.time() < deadline:
        series = scrape(args.upstream)["series"]
        gauge = (series.get(NAMES["decode_gauge"]) or [{}])[0].get("value")
        counter = (series.get(NAMES["gen_tokens"]) or [{}])[0].get("value")
        if gauge is not None:
            seen.append(gauge)
            counters.append(counter)
        time.sleep(pause)
    thread.join(timeout=5)
    growth = [b - a for a, b in zip(counters, counters[1:])
              if isinstance(a, (int, float)) and isinstance(b, (int, float))]
    return {
        "what": "the average-since-last-scrape gauge, polled every second during one "
                "long decode: each read resets the window the server averages over, "
                "so the tile's number depends on who else is scraping",
        "gauge_samples": seen,
        "counter_deltas_per_sample": growth,
        "spread_factor": (max(seen) / min(seen)) if seen and min(seen) > 0 else None,
        "request_failed": error[0],
        "class": "window",
    }


def probe_completion(url: str, *, max_tokens: int, timeout: float) -> dict:
    """One completion, for probes that need the box to actually be generating."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import drive_load  # local import: only the probes that need load pay for it

    model = drive_load.resolve_model(url, timeout=timeout)
    return drive_load.completions(url, model, drive_load.PREFIX + " Count from 1 to 400.",
                                  max_tokens, timeout)


def cmd_page(args) -> int:
    """Print the page-side figures for one live upstream as JSON.

    Small on purpose: `monitor/tests/test_parity_oracle.test.mjs` calls this and
    compares its numbers against what the real page renders in a headless DOM, so the
    harness' model of the page cannot quietly drift away from the page itself. The
    harness compares two views of the server, so if its model of one view is wrong the
    whole comparison is worthless — this is the check that says it is not.
    """
    scraped = scrape(args.upstream)
    figures = page_figures(scraped)
    # Everything by default: the oracle test needs to see the figures the page no
    # longer renders as well, to prove they are still absent.
    keep = args.only or sorted(FIGURE_BY_KEY)
    unknown = [key for key in keep if key not in FIGURE_BY_KEY]
    if unknown:
        raise SystemExit(f"unknown figure(s): {unknown}")
    out = {key: figures.get(key) for key in keep}
    print(json.dumps({"upstream": args.upstream, "series": scraped["count"], "page": out},
                     indent=2, sort_keys=True))
    return 0


def cmd_selftest(args) -> int:
    """No Prometheus, no model server: the page-side model and the classes."""
    text = json.loads((HERE / "fixtures" / "scrapes.json").read_text(encoding="utf-8"))
    failures = []
    for name, scrape_text in text.items():
        parsed = serve.parse_prometheus(scrape_text)
        by_name: dict[str, list] = {}
        for s in parsed["series"]:
            by_name.setdefault(s["name"], []).append(s)
        figures = page_figures({"series": by_name})
        expect_path = HERE / "fixtures" / "expected.json"
        expected = json.loads(expect_path.read_text(encoding="utf-8")).get(name, {})
        for key, want in expected.items():
            got = figures.get(key)
            if want is None and got is None:
                continue
            if want is None or got is None or abs(got - want) > abs(want) * 1e-9 + 1e-9:
                failures.append(f"{name}.{key}: page={got} expected={want}")
        print(f"[selftest] {name}: {len(parsed['series'])} series, "
              f"non-finite={sum(1 for v in by_name.values() for h in v if h['value'] is None)}")
    if failures:
        for failure in failures:
            print(f"[selftest] FAIL {failure}", file=sys.stderr)
        return 1
    print("[selftest] the page-side model reproduces every fixture it claims to")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    record = sub.add_parser("record", help="paired samples while both views are live")
    record.add_argument("--upstream", default="http://llm.home:8080")
    record.add_argument("--prometheus", default="http://127.0.0.1:9090")
    record.add_argument("--interval", type=float, default=15.0,
                        help="seconds between paired samples (match the scrape interval)")
    record.add_argument("--duration", default="1h")
    record.add_argument("--max-samples", type=int, default=0)
    record.add_argument("--windows", nargs="+", default=list(DEFAULT_WINDOWS))
    record.add_argument("--scrape-interval", type=float, default=15.0,
                        help="the Prometheus scrape interval; sets how far back the "
                             "skew test looks for a value Prometheus itself saw")
    record.add_argument("--out", default=str(HERE / "runs" / "live"))
    record.add_argument("--strict", action="store_true",
                        help="exit non-zero on any unexplained delta")
    record.set_defaults(func=cmd_record)

    report = sub.add_parser("report", help="aggregate a recorded run into a table")
    report.add_argument("samples")
    report.add_argument("--csv", default="")
    report.add_argument("--reclassify", action="store_true",
                        help="re-run classify() over samples.raw.jsonl instead of "
                             "trusting the classes recorded during the run")
    report.add_argument("--json", action="store_true",
                        help="machine-readable summary instead of the text table")
    report.set_defaults(func=cmd_report)

    probe = sub.add_parser("probe", help="the delta classes a quiet window cannot show")
    probe.add_argument("--upstream", default="http://llm.home:8080")
    probe.add_argument("--prometheus", default="http://127.0.0.1:9090")
    probe.add_argument("--fixture-fanout", default="",
                       help="an upstream whose /metrics fans one name out over label sets")
    probe.add_argument("--fixture-reset", default="http://127.0.0.1:8113",
                       help="mock_upstream --mode reset, for the counter-reset probe")
    probe.add_argument("--fixture-nan", default="",
                       help="an upstream whose /metrics contains NaN/+Inf")
    probe.add_argument("--scrape-interval", type=float, default=15.0)
    probe.add_argument("--out", default=str(HERE / "runs" / "probes"))
    probe.set_defaults(func=cmd_probe)

    page = sub.add_parser("page", help="the page-side figures for one live upstream, as JSON")
    page.add_argument("--upstream", default="http://llm.home:8080")
    page.add_argument("--only", nargs="+", default=[],
                      help="figures to print (default: the ones the page still renders)")
    page.set_defaults(func=cmd_page)

    selftest = sub.add_parser("selftest", help="offline check of the page-side model")
    selftest.set_defaults(func=cmd_selftest)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
