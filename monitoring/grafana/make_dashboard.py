#!/usr/bin/env python3
"""Generate `dashboards/llm-server.json` — the LLM Server Grafana dashboard.

Why a generator instead of a committed blob:

* A hand-edited Grafana JSON is ~4000 lines of which ~90 % is UI noise
  (`"description": ""`, `"showPoints": "auto"`, per-panel defaults). Diffs of it
  are unreadable, so review of the only thing that actually matters — the PromQL —
  stops happening.
* Every panel here carries its PromQL in its `description`. Grafana's UI does not
  show the expression next to the chart, so a JSON blob in git is the only place a
  reader can ask "what query produced this?". In the generator that question is
  answered three lines from the query.

Regenerate (this is what CI runs `--check` against)::

    python3 monitoring/grafana/make_dashboard.py
    python3 monitoring/check_monitoring.py

Panel **ids are a contract**, not a detail: `monitor/serve.py --grafana-url` builds
deep links as ``<grafana>/d/llm-server/llm-server?...&viewPanel=<id>``, and ADR 0001
C4 reserves ids 1-7 for specific panels. Adding panels is fine (the flow allocator
hands out ids from 20); renumbering 1-7 breaks links that are already bookmarked.
Same for the dashboard uid (`llm-server`) and the template variable names
(`instance`, `model`).

Layout is computed, not hand-written: panels flow left-to-right and wrap at 24
columns, so adding one cannot silently overlap its neighbour — the usual reason a
provisioned dashboard JSON ends up looking broken on first open.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards" / "llm-server.json"

DATASOURCE = {"type": "prometheus", "uid": "${datasource}"}
FIRST_REF = r"\b(sum|max|min|avg|count|topk|bottomk|histogram_quantile|label_replace|clamp_min|clamp_max|rate|irate|increase|delta|idelta|abs|absent|ceil|floor|round|vector|time|scalar|vector)\("

#: Panel ids 1-7 are reserved by ADR 0001 C4 (see module docstring).
RESERVED = {
    1: "decode throughput (tok/s)",
    2: "prompt throughput (tok/s)",
    3: "latency quantiles — TTFT p50/p90/p99",
    4: "KV-cache pressure over time",
    5: "requests running / queued",
    6: "prompt-cache hit ratio + draft-accept ratio",
    7: "scrape health (llm_monitor_*) + uptime",
}
FREE_IDS = iter(range(20, 1000))
ROW_IDS = iter(range(100, 900))

CAVEAT_NO_DATA = (
    "\n\n'No data' here is usually one of: the server publishes no /metrics (the "
    "monitor page still works — see monitoring/README.md), the scrape target is "
    "down, or this build does not expose the family. The panels above say which."
)
CAVEAT_RATE = (
    "\n\nrate() needs two points: the line starts at least one scrape interval in, "
    "and a counter reset (server restart) shows as a gap, not a spike to zero."
)

# --------------------------------------------------------------------------- #
# selectors
# --------------------------------------------------------------------------- #

#: Same vendor-drift idea as monitor/static/app.js METRIC_SIGNALS, one place only.
DECODE_TOKENS = "llamacpp:tokens_predicted_total|llamacpp:predicted_tokens_total|vllm:prompt_tokens_total"
PROMPT_TOKENS = "llamacpp:prompt_tokens_total|vllm:prompt_tokens_total"
DECODE_SECONDS = "llamacpp:tokens_predicted_seconds_total|llamacpp:predicted_seconds_total"
TTFT_BUCKETS = "llamacpp:time_to_first_token_seconds_bucket|llamacpp:ttft_seconds_bucket"
TPOT_BUCKETS = "llamacpp:time_per_token_seconds_bucket|llamacpp:tpot_seconds_bucket"
E2E_BUCKETS = "llamacpp:requests_seconds_bucket|llamacpp:request_seconds_bucket"
KV_RATIO = "llamacpp:kv_cache_usage_ratio|llamacpp:kv_cache_usage"
KV_TOKENS = "llamacpp:kv_cache_tokens|llamacpp:kv_tokens"
KV_POOL = "halogen:kv_pool_total_tokens|llamacpp:context_size"
CACHE_CACHED = "llamacpp:prompt_cache_tokens_cached_total"
CACHE_PROCESSED = "llamacpp:prompt_cache_tokens_processed_total"
DRAFT_ACCEPTED = "llamacpp:draft_tokens_accepted_total"
DRAFT_TOTAL = "llamacpp:draft_tokens_total"
SPEC_TIME = "llamacpp:speculative_time_seconds_total"
SPEC_DECIDE = "llamacpp:speculative_decide_time_seconds_total"
SPEC_EDRAFT = "llamacpp:speculative_estimate_draft_time_seconds_total"
SPEC_FULL = "llamacpp:speculative_draft_time_seconds_total"
REQUESTS_TOTAL = "halogen:requests_total|llamacpp:requests_total"
REQUESTS_STRUCTURED = "halogen:structured_requests_total"
REQUESTS_PROCESSING = "llamacpp:requests_processing|halogen:requests_running"
REQUESTS_DEFERRED = "llamacpp:requests_deferred|halogen:requests_queued"
REQUESTS_SLOTS = "halogen:slot_capacity"
DRAFT_REJECTED = "llamacpp:draft_tokens_rejected_total"


def sel(names: str) -> str:
    """`{__name__=~"a|b"}` — matches several vendor spellings in one selector."""
    return '{__name__=~"%s"}' % names


def rate(names: str) -> str:
    return f"sum(rate({sel(names)}[$__rate_interval]))"


def by_model(expr: str) -> str:
    """Group by one canonical `model` label across llama.cpp / vLLM spellings."""
    return (
        "sum by (model) (label_replace(label_replace("
        f"{expr}, "
        '"model", "$1", "model_name", "(.*)"), '
        '"model", "unknown", "model", "^$"))'
    )


def hb(buckets: str) -> str:
    """Histogram selector: `{__name__=~"a|b"}` (kept out of f-strings: braces)."""
    return '{__name__=~"%s"}' % buckets


def by_instance(expr: str) -> str:
    return f"sum by (instance, job) ({expr})"


def target(expr: str, legend: str, ref: str = "A") -> dict:
    return {
        "datasource": DATASOURCE,
        "editorMode": "code",
        "expr": expr,
        "legendFormat": legend,
        "range": True,
        "refId": ref,
        "interval": "15s",
        "intervalFactor": 1,
    }


# --------------------------------------------------------------------------- #
# panel constructors
# --------------------------------------------------------------------------- #


def _base(pid, kind, title, description, targets, *, unit, decimals, min_, max_,
          thresholds, legend, reduce, extra):
    steps = thresholds or [{"color": "green", "value": None}]
    defaults = {
        "color": {"mode": "palette-classic"},
        "custom": {
            "axisBorderShow": False,
            "axisCenteredZero": False,
            "axisLabel": "",
            "drawStyle": "line",
            "fillOpacity": 8,
            "gradientMode": "none",
            "lineInterpolation": "linear",
            "lineWidth": 1,
            "pointSize": 4,
            "showPoints": "auto",
            "spanNulls": False,
            "stacking": {"group": "A", "mode": "none"},
            "thresholdsStyle": {"mode": "off"},
        },
        "mappings": [],
        "thresholds": {"mode": "absolute", "steps": steps},
        "unit": unit,
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    if min_ is not None:
        defaults["min"] = min_
    if max_ is not None:
        defaults["max"] = max_
    body = {
        "id": pid,
        "type": kind,
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "legend": legend
            or {"calcs": ["lastNotNull", "max"], "displayMode": "table", "placement": "bottom"},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }
    if kind == "stat":
        body["options"] = {
            "colorMode": "value",
            "graphMode": "area",
            "justifyMode": "auto",
            "orientation": "auto",
            "reduceOptions": {"calcs": reduce or ["lastNotNull"], "fields": "", "values": False},
            "textMode": "auto",
            "wideLayout": True,
        }
        body["targets"] = [dict(t, range=False) for t in targets]
        body["fieldConfig"]["defaults"]["custom"] = {}
        body["targets"] = [dict(t, instant=True) for t in body["targets"]]
    if kind == "timeseries":
        body["fieldConfig"]["defaults"]["custom"]["thresholdsStyle"] = {
            "mode": "line" if thresholds and len(thresholds) > 1 else "off"
        }
    if extra:
        _merge(body, extra)
    return body


def _merge(dst: dict, src: dict) -> None:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _merge(dst[key], value)
        else:
            dst[key] = value


def ts(title, description, targets, *, pid=None, unit="short", decimals=None, min=None,
       max=None, thresholds=None, legend=None, h=8, w=12):
    """A time-series panel. `h`/`w` are grid cells; the flow layout does the rest."""
    pid = pid or next(FREE_IDS)
    return _base(
        pid, "timeseries", title, description, targets, unit=unit, decimals=decimals,
        min_=min, max_=max, thresholds=thresholds, legend=legend, reduce=None,
        extra={"gridPos": {"h": h, "w": w, "x": 0, "y": 0}},
    )


def stat(title, description, targets, *, pid=None, unit="short", decimals=None, min=None,
         max=None, thresholds=None, reduce=None, w=4, h=4):
    pid = pid or next(FREE_IDS)
    return _base(
        pid, "stat", title, description, targets, unit=unit, decimals=decimals,
        min_=min, max_=max, thresholds=thresholds, legend=None, reduce=reduce,
        extra={"gridPos": {"h": h, "w": w, "x": 0, "y": 0}},
    )


def markdown(title, text, *, w=24, h=2):
    return {
        "id": next(FREE_IDS),
        "type": "text",
        "title": title,
        "description": text,
        "datasource": DATASOURCE,
        "gridPos": {"h": h, "w": w, "x": 0, "y": 0},
        "options": {"code": {"language": "plaintext", "showLineNumbers": False},
                    "content": text, "mode": "markdown"},
    }


def row(title, panels, *, collapsed=True):
    """Wrap panels in a Grafana `row`.

    Panel ids must be unique *dashboard-wide*, rows included, so rows take ids from
    a separate range and the reserved 1-7 can never collide with one.
    """
    rid = next(ROW_IDS)
    return {
        "id": rid,
        "type": "row",
        "title": title,
        "collapsed": collapsed,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": 0},
        "panels": panels,
        "datasource": DATASOURCE,
    }


def variable(name, kind, label, query, *, hint="", current=("$__all", "$__all"),
             multi=True, include_all=True, all_value=".*", refresh=1):
    return {
        "name": name,
        "label": label,
        "type": kind,
        "query": query if kind == "query" else {"query": query, "uid": "${datasource}"},
        "current": {"text": current[0], "value": current[1], "selected": True},
        "hide": 0,
        "includeAll": include_all,
        "allValue": all_value,
        "multi": multi,
        "options": [],
        "refresh": refresh,
        "regex": "",
        "skipUrlSync": False,
        "sort": 1,
        "description": hint,
    }


VARIABLES = [
    {
        "name": "datasource",
        "label": "Data source",
        "type": "datasource",
        "query": "prometheus",
        "current": {"text": "Prometheus", "value": "prometheus", "selected": True},
        "hide": 0,
        "includeAll": False,
        "multi": False,
        "options": [],
        "refresh": 1,
        "regex": "",
        "skipUrlSync": False,
        "description": (
            "Panels reference ${datasource} rather than a hardcoded uid: the "
            "provisioned uid is set in git (datasource.yml -> 'prometheus') and a "
            "uid baked into the JSON is how a promoted dashboard breaks in staging."
        ),
    },
    variable("job", "job", "Scrape job", "label_values(llm_monitor_build_info, job)",
             hint="Which scrape path produced the series: 'llm-server' is the direct "
                  "target, 'llm-server-via-proxy' is monitor/serve.py's /metrics "
                  "passthrough. Exactly one of them should carry a given server's "
                  "metrics (monitoring/README.md) — this variable is how you check.",
             current=("All", "$__all")),
    variable("instance", "instance", "Instance",
             "label_values(up{job=~\"$job\"}, instance)",
             hint="host:port of the scrape target. The monitor page's Grafana links "
                  "set var-instance to the upstream host:port."),
    variable("model", "model", "Model",
             "label_values(" + by_model(rate(DECODE_TOKENS)).replace("$__rate_interval", "5m")
             + ", model)",
             hint="Model label: file_sd's `model` label (llama.cpp usually has none) "
                  "or vLLM's model_name. 'unknown' means the server does not label "
                  "its own metrics at all — add it to the file_sd labels.",
             current=("All", "$__all")),
]


def var_q(*, with_model: bool = False) -> str:
    """Selector clamped by $job / $instance (and $model where the label exists).

    The variables default to All with allValue `.*`, so an unset variable widens
    instead of silently filtering everything out.
    """
    base = '{job=~"$job", instance=~"$instance"}'
    return base + (', model=~"$model"' if with_model else "")


# --------------------------------------------------------------------------- #
# rows
# --------------------------------------------------------------------------- #
# Every description states the PromQL in plain words: Grafana's panel edit view
# shows the expression but not why it is the right one, and the point of the
# dashboard is to answer questions the bespoke page could not.
# --------------------------------------------------------------------------- #


def overview_row():
    panels = [
        stat(
            "requests in flight",
            "Instant gauges, no rate(): sum(llamacpp:requests_processing) and "
            "sum(llamacpp:requests_deferred).\n\ndeferred > 0 means arrivals are "
            "waiting for a slot — the earliest sign the KV budget is the limit.",
            [
                target("sum(llamacpp:requests_processing)", "processing"),
                target("sum(llamacpp:requests_deferred)", "deferred"),
            ],
            w=6, h=5,
        ),
        stat(
            "KV cache used",
            "max(llamacpp:kv_cache_usage_ratio) — the same series the "
            "LLMKVCachePressure alerts read, so this tile and the alerts cannot "
            "disagree. Shown as a percentage.",
            [target(f"max({sel(KV_RATIO)})", "usage")],
            unit="percentunit", decimals=1, min=0, max=1, w=6, h=5,
            thresholds=[
                {"color": "green", "value": None},
                {"color": "yellow", "value": 0.8},
                {"color": "red", "value": 0.9},
            ],
        ),
        stat(
            "TTFT p99",
            "Same expression as panel 3, instant instead of ranged. 'No data' means "
            "this build exposes no TTFT histogram — the server's own log is then the "
            "only latency source.",
            [target(
                "histogram_quantile(0.99, sum by (le) rate("
                f"{sel(TTFT_BUCKETS)}[$__rate_interval]))", "p99")],
            unit="s", decimals=2, w=6, h=5,
        ),
        stat(
            "TPOT p99",
            "histogram_quantile(0.99, sum by (le) rate(<tpot_bucket>[$__rate_interval]))"
            " — decode latency per output token, i.e. how fast text appears.",
            [target(
                "histogram_quantile(0.99, sum by (le) rate("
                f"{sel(TPOT_BUCKETS)}[$__rate_interval]))", "p99")],
            unit="s", decimals=3, w=6, h=5,
        ),
        ts(
            "Decode throughput (tok/s)",
            "PromQL: sum(rate({__name__=~\"llamacpp:tokens_predicted_total|"
            "llamacpp:predicted_tokens_total\"}[$__rate_interval]))\n\n"
            "The windowed rate over the counter, per-instance so two servers on one "
            "box stay separate lines. The third series is the server's own "
            "llamacpp:predicted_tokens_seconds gauge — an average since the last "
            "scrape that resets with the process — overlaid on purpose while the "
            "bespoke page is retired: where they disagree, rate() is right.\n\n"
            "This is the panel that answers 'what was decode throughput at 03:00', "
            "which the old page could not answer at all.",
            [
                target("sum(rate({__name__=~\"llamacpp:tokens_predicted_total|"
                       "llamacpp:predicted_tokens_total\"}[$__rate_interval]))", "decode tok/s"),
                target(by_instance(f"rate({sel(DECODE_TOKENS)}[$__rate_interval])"), "{{instance}}"),
                target("sum(llamacpp:predicted_tokens_seconds)", "server gauge (since last scrape)"),
            ],
            pid=1,
        ),
        ts(
            "Prompt throughput (tok/s)",
            "PromQL: sum(rate({__name__=~\"llamacpp:prompt_tokens_total|"
            "vllm:prompt_tokens_total\"}[$__rate_interval]))\n\n"
            "Prefill throughput. llama.cpp counts only the prompt tokens that MISSED "
            "the prompt cache in this counter, which is why the cache panel computes "
            "cached / (processed + cached) rather than cached / processed." + CAVEAT_RATE,
            [target(f"sum(rate({sel(PROMPT_TOKENS)}[$__rate_interval]))", "prompt tok/s"),
             target(by_instance(f"rate({sel(PROMPT_TOKENS)}[$__rate_interval])"), "{{instance}}")],
            pid=2,
        ),
        ts(
            "Requests running / queued",
            "PromQL: sum(llamacpp:requests_processing) / sum(llamacpp:requests_deferred)"
            " (halogen spellings requests_running / requests_queued match too).\n\n"
            "Deferred > 0 for minutes is queueing, and the LLMQueueing alert fires on "
            "exactly this expression — the panel and the alert are the same query.",
            [
                target(f"sum({sel(REQUESTS_PROCESSING)})", "running"),
                target(f"sum({sel(REQUESTS_DEFERRED)})", "queued"),
                target(f"{sel(REQUESTS_SLOTS)}", "slot capacity (halogen)"),
            ],
            pid=5,
        ),
        ts(
            "Requests / s and structured share",
            "PromQL: sum(rate({__name__=~\"halogen:requests_total|llamacpp:requests_total\""
            "[$__rate_interval])) and halogen:structured_requests_total / "
            "clamp_min(halogen:requests_total, 1).\n\n"
            "clamp_min keeps a restart's 0/0 from rendering NaN as a hole in the line."
            + CAVEAT_NO_DATA,
            [
                target(rate(REQUESTS_TOTAL), "requests/s"),
                target(f"sum(rate({sel(REQUESTS_STRUCTURED)}[$__rate_interval])) / "
                       f"clamp_min(sum(rate({sel(REQUESTS_TOTAL)}[$__rate_interval])), 1)",
                       "share under a JSON schema", "B"),
            ],
        ),
        ts(
            "Per-model decode tok/s",
            "PromQL: sum by (model) (label_replace(label_replace(rate(<tokens_total>"
            "[$__rate_interval]), \"model\", \"$1\", \"model_name\", \"(.*)\"), "
            "\"model\", \"unknown\", \"model\", \"^$\"))\n\n"
            "The two label_replace calls map llama.cpp (a `model` label from file_sd, "
            "or none) and vLLM (`model_name`) onto one name, and turn 'no model label "
            "at all' into the literal 'unknown' so the legend is never blank. This is "
            "the per-model comparison the bespoke page could not do.",
            [target(by_model(rate(DECODE_TOKENS)), "{{model}}")],
        ),
        ts(
            "Lifetime tok/s — what the old tile actually showed",
            "PromQL: sum(llamacpp:tokens_predicted_total) / clamp_min("
            "sum(llamacpp:tokens_predicted_seconds_total), 1)\n\n"
            "This is the arithmetic the bespoke page did in JavaScript: two raw "
            "counters, no window, no reset handling. Kept as a floor to sanity-check "
            "the windowed rates against — it cannot say WHEN anything was slow, which "
            "is what the panels above are for.",
            [target(f"sum({sel(DECODE_TOKENS)}) / clamp_min(sum({sel(DECODE_SECONDS)}), 1)",
                    "tokens/s since server start")],
        ),
    ]
    return row("Overview", panels, collapsed=False)


def latency_row():
    panels = [
        markdown(
            "Histogram availability",
            "Panels on this row are empty when the server exposes no latency "
            "histograms. `llama-server` builds without `-otl` / metrics enabled may "
            "skip them; the monitor page still works because it reads /health, "
            "/v1/models and /cache — see the runbook.",
            h=2,
        ),
        ts(
            "Time to first token — p50 / p90 / p99",
            "PromQL: histogram_quantile(0.5|0.9|0.99, sum by (le) rate("
            "{__name__=~\"llamacpp:time_to_first_token_seconds_bucket|"
            "llamacpp:ttft_seconds_bucket\"}[$__rate_interval]))\n\n"
            "`sum by (le)` first: quantiles of per-series quantiles are meaningless, "
            "and this keeps a second server or a second model from changing the "
            "answer. `le` is what the quantile reads, so it must survive the "
            "aggregation.\n\nThis is the p99-TTFT-over-time question the bespoke page "
            "could not answer; the recording-rule aliases are "
            "llm:time_to_first_token_seconds:{p50,p90,p99}.",
            [
                target("histogram_quantile(0.5, sum by (le) rate("
                       f"{hb(TTFT_BUCKETS)}[$__rate_interval]))", "p50"),
                target("histogram_quantile(0.9, sum by (le) rate("
                       f"{hb(TTFT_BUCKETS)}[$__rate_interval]))", "p90"),
                target("histogram_quantile(0.99, sum by (le) rate("
                       f"{hb(TTFT_BUCKETS)}[$__rate_interval]))", "p99"),
            ],
            pid=3, unit="s", h=9, w=24,
        ),
        ts(
            "Time per output token — p50 / p90 / p99",
            "PromQL: histogram_quantile(0.5|0.9|0.99, sum by (le) rate("
            "{__name__=~\"llamacpp:time_per_token_seconds_bucket|"
            "llamacpp:tpot_seconds_bucket\"}[$__rate_interval]))\n\n"
            "Decode latency per token — what a reader perceives as streaming speed.",
            [
                target("histogram_quantile(0.5, sum by (le) rate("
                       f"{hb(TPOT_BUCKETS)}[$__rate_interval]))", "p50"),
                target("histogram_quantile(0.99, sum by (le) rate("
                       f"{hb(TPOT_BUCKETS)}[$__rate_interval]))", "p99"),
            ],
            unit="s", h=9, w=12,
        ),
        ts(
            "End-to-end request time — p50 / p90 / p99",
            "PromQL: histogram_quantile(..., sum by (le) rate("
            "{__name__=~\"llamacpp:requests_seconds_bucket|llamacpp:request_seconds_bucket\"}"
            "[$__rate_interval]))\n\n"
            "Queue time + prefill + decode together, so it moves before the throughput "
            "panels do when the box is oversubscribed." + CAVEAT_NO_DATA,
            [
                target("histogram_quantile(0.5, sum by (le) rate("
                       f"{hb(E2E_BUCKETS)}[$__rate_interval]))", "p50"),
                target("histogram_quantile(0.9, sum by (le) rate("
                       f"{hb(E2E_BUCKETS)}[$__rate_interval]))", "p90"),
                target("histogram_quantile(0.99, sum by (le) rate("
                       f"{hb(E2E_BUCKETS)}[$__rate_interval]))", "p99"),
            ],
            unit="s", h=9, w=12,
        ),
    ]
    return row("Latency", panels)


def cache_row():
    panels = [
        ts(
            "KV-cache pressure over time",
            "PromQL: max(llamacpp:kv_cache_usage_ratio) with an 80 % / 97 % band, plus "
            "avg_over_time(...[1h]) so a spike and a sustained squeeze are "
            "distinguishable at a glance.\n\n"
            "This is the 'was the KV cache full overnight?' panel: set the range to "
            "12h or 24h and the answer is on the chart. Same series the "
            "LLMKVCachePressure alerts read, so this panel and paging cannot disagree.",
            [
                target(f"max({sel(KV_RATIO)})", "usage"),
                target(f"avg_over_time(max({sel(KV_RATIO)})[1h])", "1h average"),
            ],
            pid=4, unit="percentunit", min=0, max=1, decimals=1, h=9,
            thresholds=[
                {"color": "green", "value": None},
                {"color": "yellow", "value": 0.8},
                {"color": "red", "value": 0.9},
            ],
        ),
        ts(
            "Prompt-cache hit ratio + draft-accept ratio",
            "PromQL: rate(prompt_cache_tokens_cached_total) / clamp_min(rate(processed) "
            "+ rate(cached), 1) — the llama.cpp pair, and rate(draft_tokens_accepted"
            "_total) / clamp_min(rate(draft_tokens_total), 1) for speculative decode.\n\n"
            "Two ratios that explain each other: a cold prompt cache and a collapsing "
            "draft-accept ratio together mean something restarted or the slots are "
            "evicting each other. Both are clamp_min-guarded so a restart gives a flat "
            "line, not NaN.",
            [
                target(f"sum(rate({sel(CACHE_CACHED)}[$__rate_interval])) / clamp_min("
                       f"sum(rate({sel(CACHE_PROCESSED)}[$__rate_interval])) + "
                       f"sum(rate({sel(CACHE_CACHED)}[$__rate_interval])), 1)",
                       "prompt-cache hit ratio"),
                target(f"sum(rate({sel(DRAFT_ACCEPTED)}[$__rate_interval])) / clamp_min("
                       f"sum(rate({sel(DRAFT_TOTAL)}[$__rate_interval])), 1)",
                       "draft-accept ratio"),
            ],
            pid=6, unit="percentunit", min=0, max=1, decimals=1, h=9,
        ),
        ts(
            "KV tokens in use vs pool capacity",
            "PromQL: sum(llamacpp:kv_cache_tokens) and sum(halogen:kv_pool_total_tokens)"
            " (llama.cpp's own ratio is kv_cache_usage_ratio; the halogen pool gauges "
            "split it into reserved and free).\n\n"
            "The ratio is enough to alert on; the absolute numbers say how much context "
            "is actually resident, and whether reserved-but-idle tokens are the ones "
            "making the ratio look bad.",
            [
                target(f"sum({sel(KV_TOKENS)})", "in use"),
                target(f"sum({sel(KV_POOL)})", "pool capacity"),
                target("sum(halogen:kv_pool_reserved_tokens)", "reserved (halogen)"),
            ],
            unit="short", h=8,
        ),
        ts(
            "Prompt cache: cached vs processed",
            "PromQL: sum(rate(llamacpp:prompt_cache_tokens_cached_total"
            "[$__rate_interval])) and sum(rate(...processed_total[$__rate_interval])).\n\n"
            "cached climbing while processed falls is the cache doing its job; both "
            "falling together means the prefix changed (a rebuilt system prompt, or a "
            "restart) and TTFT pays for it." + CAVEAT_RATE,
            [
                target(f"sum(rate({sel(CACHE_CACHED)}[$__rate_interval]))", "served from cache"),
                target(f"sum(rate({sel(CACHE_PROCESSED)}[$__rate_interval]))", "reprocessed"),
            ],
            unit="short", h=8,
        ),
        ts(
            "Queueing: deferred / running / slots",
            "PromQL: sum(llamacpp:requests_deferred), sum(llamacpp:requests_processing), "
            "sum(halogen:slot_capacity).\n\n"
            "Deferred > 0 while running < slot capacity is a different problem from "
            "deferred > 0 at full slots: the first is a scheduling stall, the second is "
            "simple saturation.",
            [
                target(f"sum({sel(REQUESTS_DEFERRED)})", "deferred"),
                target(f"sum({sel(REQUESTS_PROCESSING)})", "processing"),
                target(f"{sel(REQUESTS_SLOTS)}", "slot capacity"),
            ],
            h=7, w=24,
        ),
    ]
    return row("Cache & KV", panels)


def spec_row():
    panels = [
        stat(
            "draft acceptance",
            "rate(llamacpp:draft_tokens_accepted_total) / clamp_min(rate("
            "llamacpp:draft_tokens_total), 1) — instant value of the same expression "
            "as panel 6's second series.",
            [target(f"sum(rate({sel(DRAFT_ACCEPTED)}[$__rate_interval])) / clamp_min("
                    f"sum(rate({sel(DRAFT_TOTAL)}[$__rate_interval])), 1)", "accept ratio")],
            unit="percentunit", decimals=1, min=0, max=1, w=6, h=5,
        ),
        stat(
            "speculative speedup ratio",
            "((verify + decide + estimated-draft) - actual-draft) / verify — below 1 "
            "means speculation is costing wall-clock time rather than saving it. The "
            "break-even line is drawn at 1.",
            [target(
                "((sum(rate({__name__=~\"llamacpp:speculative_time_seconds_total\"}"
                "[$__rate_interval])) + sum(rate("
                "{__name__=~\"llamacpp:speculative_decide_time_seconds_total\"})"
                "[$__rate_interval])) + sum(rate({__name__=~\""
                "llamacpp:speculative_estimate_draft_time_seconds_total\"}"
                "[$__rate_interval]))) - sum(rate("
                "{__name__=~\"llamacpp:speculative_draft_time_seconds_total\"}"
                "[$__rate_interval]))) / clamp_min(sum(rate("
                "{__name__=~\"llamacpp:speculative_time_seconds_total\"}"
                "[$__rate_interval])), 1)", "time ratio")],
            decimals=2, w=6, h=5, min=0,
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
        ),
        ts(
            "Draft acceptance and drafts per accept",
            "PromQL: rate(draft_tokens_accepted_total) / clamp_min(rate("
            "draft_tokens_total), 1) and rate(draft_tokens_total) / clamp_min("
            "rate(draft_tokens_accepted_total), 1).\n\n"
            "'Drafts per accepted token' is the multiplier on wasted draft work — "
            "watch it alongside the speculative speedup ratio when decode drops.",
            [
                target(f"sum(rate({sel(DRAFT_ACCEPTED)}[$__rate_interval])) / clamp_min("
                       f"sum(rate({sel(DRAFT_TOTAL)}[$__rate_interval])), 1)", "accept ratio"),
                target(f"sum(rate({sel(DRAFT_TOTAL)}[$__rate_interval])) / clamp_min("
                       f"sum(rate({sel(DRAFT_ACCEPTED)}[$__rate_interval])), 1)",
                       "draft tokens per accepted"),
            ],
            h=8, w=12,
        ),
        ts(
            "Speculative decode time budget",
            "PromQL: rate() over llamacpp:speculative_time_seconds_total (verify), "
            "_decide_time_, _estimate_draft_time_ and _draft_time_ (actual draft).\n\n"
            "If draft time grows faster than verify time the speculation is the "
            "bottleneck; if verify dominates, speculation has stopped paying and "
            "--specdraft or the drafter should be revisited.",
            [
                target(f"sum(rate({sel(SPEC_TIME)}[$__rate_interval]))", "verify"),
                target(f"sum(rate({sel(SPEC_DECIDE)}[$__rate_interval]))", "decide"),
                target(f"sum(rate({sel(SPEC_EDRAFT)}[$__rate_interval]))", "estimated draft"),
                target(f"sum(rate({sel(SPEC_FULL)}[$__rate_interval]))", "actual draft"),
            ],
            unit="s", h=8, w=12,
        ),
    ]
    return row("Speculative decode", panels)


def scrape_row():
    panels = [
        stat(
            "monitor version",
            "llm_monitor_build_info{version} — constant 1, the version travels as a "
            "label. Proves the /metrics passthrough is being scraped at all.",
            [target("count(llm_monitor_build_info)", "monitors reporting")],
            w=5, h=5,
        ),
        stat(
            "last scrape ok",
            "min(llm_monitor_scrape_ok) — 1 when the monitor's most recent upstream "
            "scrape returned exposition text, 0 when it failed, 'No data' when nothing "
            "scrapes the monitor.",
            [target("min(llm_monitor_scrape_ok)", "scrape ok")],
            w=5, h=5, min=0, max=1,
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
        ),
        stat(
            "firing alerts",
            "ALERTS{alertstate=\"firing\"} counted. Kept on this dashboard because "
            "Grafana's own alerting UI is a second place to look, and this box wants "
            "one.",
            [target("count(ALERTS{alertstate=\"firing\"})", "firing")],
            w=5, h=5,
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
        ),
        ts(
            "Scrape health (llm_monitor_*) + uptime",
            "PromQL: min(llm_monitor_scrape_ok) and up{job=~\"$job\"}, plus "
            "sum(increase(llm_monitor_scrape_success_total[5m])) — the canonical "
            "'is the monitor actually scraping' signal from ADR 0001 C2.\n\n"
            "Read it top-down: up == 0 means Prometheus could not reach the target; "
            "llm_monitor_scrape_ok == 0 means the monitor is up but its scrape of the "
            "model server failed (no /metrics, timeout, 5xx). Those are different "
            "incidents and the runbook splits on exactly this line.",
            [
                target("up{job=~\"$job\"}", "{{job}} {{instance}} up"),
                target("min(llm_monitor_scrape_ok)", "monitor scrape ok"),
                target("sum(increase(llm_monitor_scrape_success_total[$__rate_interval]))",
                       "successful scrapes in window"),
            ],
            pid=7, h=8, w=12,
        ),
        ts(
            "Scrape latency and upstream status",
            "PromQL: llm_monitor_scrape_latency_ms and "
            "llm_monitor_scrape_upstream_status, per upstream label.\n\n"
            "The monitor proxies with a hard 5 s wall-clock cap, so a latency line "
            "pinned at 5000 ms means the cap fired and Prometheus saw a scrape failure "
            "(502), not a slow-but-successful scrape.",
            [
                target("llm_monitor_scrape_latency_ms", "{{upstream}} ms"),
                target("llm_monitor_scrape_upstream_status", "{{upstream}} status"),
            ],
            h=8, w=12,
        ),
        ts(
            "Parse errors, truncation, series count",
            "PromQL: increase(llm_monitor_parse_errors_total[$__rate_interval]), "
            "max(llm_monitor_parse_truncated) and llm_monitor_series_count.\n\n"
            "The exposition text is passed through verbatim no matter what these say — "
            "they describe the monitor's *parsed* view (MAX_SERIES 2000, one parser). "
            "Non-zero parse errors means a server changed its format and Grafana's "
            "derived panels, not the raw text, are what to distrust.",
            [
                target(f"sum(increase(llm_monitor_parse_errors_total[${'{'}__rate_interval}]))",
                       "parse errors"),
                target("max(llm_monitor_parse_truncated)", "truncated"),
                target("llm_monitor_series_count", "{{upstream}} series"),
            ],
            h=8, w=12,
        ),
        markdown(
            "Monitor the monitor",
            "Prometheus knows when *it* is down only from outside. These panels read "
            "`llm_monitor_*`, published by `monitor/serve.py`'s `/metrics` passthrough; "
            "`GET /api/scrape-health` on the monitor page reads this same Prometheus "
            "back over `/api/v1/targets`, so a dead Prometheus is visible without "
            "opening Grafana — see `monitoring/README.md`.",
            h=2,
        ),
    ]
    return row("Scrape health", panels)


def flow(panels, *, start_y=1):
    """Assign gridPos by flowing panels left-to-right, wrapping at 24 columns.

    Hand-written gridPos is why provisioned dashboards overlap after one panel is
    inserted: every y after the insertion point has to be remembered. Collapsed rows
    make it worse because Grafana re-lays-out on expand.
    """
    x = y = 0
    for p in panels:
        w = p["gridPos"]["w"]
        h = p["gridPos"]["h"]
        if x + w > 24:
            x = 0
            y += h
        p["gridPos"] = {"h": h, "w": w, "x": x, "y": y + start_y}
        x += w
    return panels


def build() -> dict:
    rows = [
        overview_row(),
        latency_row(),
        cache_row(),
        spec_row(),
        scrape_row(),
    ]
    for r in rows:
        flow(r["panels"], start_y=1)
    return {
        "annotations": {"list": []},
        "editable": True,
        "graphTooltip": 1,
        "id": None,
        "links": [],
        # 15s == the scrape interval; refreshing faster cannot show anything new.
        "refresh": "15s",
        "schemaVersion": 39,
        "tags": ["llm", "llama.cpp", "halogen", "vllm", "llm-monitor"],
        "templating": {"list": VARIABLES},
        "time": {"from": "now-6h", "to": "now"},
        "timepicker": {
            "refresh_intervals": ["15s", "30s", "1m", "5m", "15m", "1h"],
            "time_options": ["1h", "6h", "24h", "7d", "30d"],
        },
        "timezone": "browser",
        "title": "LLM Server",
        "uid": "llm-server",
        "version": 1,
        "weekStart": "",
        "panels": rows,
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"wrote {OUT}")
