#!/usr/bin/env python3
"""Generate `dashboards/llm-server.json` — the LLM Server Grafana dashboard.

This file decides what the monitoring stack *means*: every number the bespoke page
computed in JavaScript becomes PromQL here, and the questions that page could not
answer (p99 TTFT over a week, "was the KV cache full overnight?", per-model
comparison) get a panel. Everything it emits is a committed file — nothing is
clicked into existence (ADR 0001 sub-decision 3).

Why a generator instead of a committed blob
------------------------------------------
* A hand-edited Grafana JSON is ~4000 lines of which ~90 % is UI noise
  (`"description": ""`, `"showPoints": "auto"`, per-panel defaults). Diffs of it
  are unreadable, so review of the only thing that actually matters — the PromQL —
  stops happening.
* Every panel's `description` states its PromQL, generated from the target
  expressions themselves rather than hand-copied. Grafana's UI does not show the
  query next to the chart, so the JSON is the only place a reader can ask "what
  produced this?"; generating the note means it cannot drift from the query it
  documents, and `check_monitoring.py` asserts the two agree.
* Layout is computed (flow layout, wrap at 24 columns), so inserting a panel
  cannot silently overlap its neighbour — the usual reason a provisioned dashboard
  looks broken on first open.

Usage::

    python3 monitoring/grafana/make_dashboard.py           # write the JSON
    python3 monitoring/grafana/make_dashboard.py --check   # fail if the JSON is stale
    python3 monitoring/check_monitoring.py                 # structural checks + drift

Contracts this file must not break
----------------------------------
* Panel **ids are a contract**, not a detail: `monitor/serve.py --grafana-url`
  builds deep links as ``<grafana>/d/llm-server/llm-server?...&viewPanel=<id>`` and
  ADR 0001 C4 reserves ids 1-7 for specific panels. Adding panels is fine (free ids
  start at 20, row ids at 100, so a row can never shadow a panel id — with ids
  colliding a `viewPanel=` link is ambiguous, which is what this generator fixed).
  Renumbering 1-7 breaks links that are already bookmarked.
* Likewise the dashboard uid (`llm-server`), the ADR C4 panel titles, and the
  template variable names (`job`, `instance`, `model`).
* No plugins, no CDN assets, no off-box URL, and the datasource is referenced only
  through the `${datasource}` variable (its uid is set in git:
  `provisioning/datasources/datasource.yml` -> `prometheus`).
* Vendor name-drift lives in the selector constants below and in
  `../prometheus/recording_rules.yml` — never sprinkled through panels. Onboarding a
  serving stack means editing a constant, not twenty panels.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards" / "llm-server.json"

DATASOURCE = {"type": "prometheus", "uid": "${datasource}"}

#: Panel ids 1-7 are reserved by ADR 0001 C4. The titles are part of the contract:
#: they say what the deep link is *for*.
RESERVED = {
    1: "Decode throughput (tok/s)",
    2: "Prompt throughput (tok/s)",
    3: "Time to first token — p50 / p90 / p99",
    4: "KV-cache pressure over time",
    5: "Requests running / queued",
    6: "Prompt-cache hit ratio + draft-accept ratio",
    7: "Scrape health (llm_monitor_*) + staleness",
}
FREE_IDS = iter(range(20, 1000))
ROW_IDS = iter(range(100, 900))

#: monitoring/prometheus/prometheus.yml scrape_interval. Panels pin their minimum
#: step to it: a finer step cannot invent resolution the TSDB does not have, it just
#: lets a panel lie more finely.
SCRAPE_INTERVAL = "15s"

#: KV pressure bands, matching alert_thresholds.env (warn 0.90 / critical 0.97 on
#: the alerts; the ticket asks for visible 80 % / 90 % guides on the panel, so the
#: panel's *visual* bands are 0.8 / 0.9 and the alert numbers stay authoritative).
KV_BANDS = [
    {"color": "green", "value": None},
    {"color": "yellow", "value": 0.8},
    {"color": "red", "value": 0.9},
]

CAVEAT_NO_DATA = (
    "\n\n'No data' here means one of: this server publishes no /metrics at all (the "
    "monitor page still works — it reads /health, /v1/models and /cache, which are "
    "not Prometheus metrics), the scrape target is down, or this build does not "
    "expose this family. The Scrape health row says which."
)
CAVEAT_RATE = (
    "\n\nrate() needs two points, so the line starts at least one scrape interval in. "
    "A counter reset (server restart) shows as a gap, not a spike to zero."
)
CAVEAT_CLAMP = (
    "\n\nclamp_min(<denominator>, 1) is deliberate. With no traffic the raw division "
    "is 0/0, PromQL turns that into NaN and Grafana drops the sample — a hole in the "
    "line that reads like a missing metric. A clamped 0 reads as 'no ratio to report', "
    "which is the truth. The bespoke page hid the same case behind divide() "
    "(monitor/static/app.js), which is one reason this panel exists."
)
CAVEAT_RULE = (
    "\n\nSeries tagged `(rule)` are the neutral llm:* aliases from "
    "monitoring/prometheus/recording_rules.yml — the vendor-drift seam ADR 0001 C3 "
    "names, and what alert rules should read. They are plotted *alongside* the raw "
    "expression, not instead of it, on purpose: a panel must still render against a "
    "server whose rules have never run, and 'No data' has to mean no data rather than "
    "no rule. Where the two disagree the rule is stale — or the windows differ: rules "
    "use a fixed 5m, panels use $__rate_interval."
)
CAVEAT_VARS = (
    "\n\n$job / $instance / $model default to All, whose allValue is .*, so an absent "
    "label widens the selector instead of emptying the panel."
)

# --------------------------------------------------------------------------- #
# selectors — the vendor-drift seams
# --------------------------------------------------------------------------- #
# The job monitor/static/app.js's METRIC_SIGNALS table does (monitor/static/app.js:957)
# as a PromQL constant: one seam per measurement instead of an exact name plus a
# fuzzy regex per name, in a browser.
# --------------------------------------------------------------------------- #

#: Generation (decode) token counters.
DECODE_TOKENS = (
    "llamacpp:tokens_predicted_total|llamacpp:predicted_tokens_total|vllm:generation_tokens_total"
)
#: The server's own "average since the last scrape" decode gauge. Overlaid on the
#: rate() panel while the bespoke page is retired; deleted by workspace-szw.11.
DECODE_GAUGE = "llamacpp:predicted_tokens_seconds|llamacpp:tokens_predicted_seconds"
#: The counter behind that gauge (cumulative decode seconds), for the lifetime math.
DECODE_SECONDS = "llamacpp:tokens_predicted_seconds_total|llamacpp:predicted_tokens_seconds_total"

PROMPT_TOKENS = "llamacpp:prompt_tokens_total|vllm:prompt_tokens_total"
PROMPT_GAUGE = "llamacpp:prompt_tokens_seconds"

REQUESTS_TOTAL = "halogen:requests_total|llamacpp:requests_total|vllm:request_success_total"
REQUESTS_STRUCTURED = "halogen:structured_requests_total"
REQUESTS_PROCESSING = "llamacpp:requests_processing|halogen:requests_running|vllm:num_requests_running"
REQUESTS_DEFERRED = "llamacpp:requests_deferred|halogen:requests_queued|vllm:num_requests_waiting"
SLOT_CAPACITY = "halogen:slot_capacity"

KV_RATIO = "llamacpp:kv_cache_usage_ratio|llamacpp:kv_cache_usage"
KV_TOKENS = "llamacpp:kv_cache_tokens|llamacpp:kvcache_used_tokens"
KV_POOL = (
    "halogen:kv_pool_positions|halogen:kv_pool_tokens|halogen:kv_pool_total_tokens|llamacpp:context_size"
)
KV_RESERVED = "halogen:kv_pool_reserved_tokens"

# halogen counts the prompt tokens the cache served in its own counter, while
# llama.cpp's prompt_tokens_total counts only the tokens that MISSED it. That split
# is why every cache ratio here is cached / (processed + cached), never cached /
# processed — the same convention LLMPromptCacheCold in rules.yml uses.
CACHE_CACHED = "halogen:prompt_tokens_cached_total|llamacpp:prompt_cache_tokens_cached_total"

DRAFT_ACCEPTED = "halogen:draft_tokens_accepted_total|llamacpp:draft_tokens_accepted_total"
DRAFT_TOTAL = "halogen:draft_tokens_total|llamacpp:draft_tokens_total"
DRAFT_REJECTED = "halogen:draft_tokens_rejected_total|llamacpp:draft_tokens_rejected_total"

SPEC_VERIFY = "llamacpp:speculative_time_seconds_total"
SPEC_DECIDE = "llamacpp:speculative_decide_time_seconds_total"
SPEC_ESTIMATE = "llamacpp:speculative_estimate_draft_time_seconds_total"
SPEC_DRAFT = "llamacpp:speculative_draft_time_seconds_total"

# --- latency families ------------------------------------------------------ #
# Builds spell the same measurement several ways, and a histogram family `x` is
# exposed as x_bucket / x_sum / x_count, so naming the stem once covers all three.
# Both `llama:` and `llamacpp:` are in the wild; vLLM is listed so that a second
# serving stack means editing a constant here instead of rewriting panels.
_TTFT = (
    "llama:time_to_first_token_seconds|llamacpp:time_to_first_token_seconds"
    "|llamacpp:ttft_seconds|vllm:time_to_first_token_seconds"
)
_TPOT = (
    "llama:time_per_output_token_seconds|llamacpp:time_per_output_token_seconds"
    "|llamacpp:time_per_token_seconds|vllm:time_per_output_token_seconds"
)
_E2E = (
    "llama:end_to_end_response_seconds|llamacpp:end_to_end_response_seconds"
    "|llamacpp:requests_seconds|llamacpp:request_seconds|vllm:e2e_request_latency_seconds"
)

MONITOR_SCRAPE_OK = "llm_monitor_scrape_ok"
MONITOR_SCRAPE_SUCCESS = "llm_monitor_scrape_success_total"
MONITOR_LATENCY = "llm_monitor_scrape_latency_ms"
MONITOR_STATUS = "llm_monitor_scrape_upstream_status"
MONITOR_PARSE_ERRORS = "llm_monitor_parse_errors_total"
MONITOR_PARSE_TRUNCATED = "llm_monitor_parse_truncated"
MONITOR_SERIES = "llm_monitor_series_count"
MONITOR_BUILD_INFO = "llm_monitor_build_info"

#: The `llm:*` recording-rule aliases (see ../prometheus/recording_rules.yml).
#: Panels plot these alongside their raw equivalents. The latency quantiles land
#: with this dashboard, which is exactly what ADR 0001 C3 deferred to it.
RULE_DECODE = "llm:decode_tokens_per_second"
RULE_PROMPT = "llm:prompt_tokens_per_second"
RULE_REQUESTS = "llm:requests_per_second"
RULE_KV = "llm:kv_cache_usage_ratio"
RULE_CACHE = "llm:prompt_cache_hit_ratio"
RULE_DRAFT = "llm:draft_acceptance_ratio"
RULE_RUNNING = "llm:requests_running"
RULE_QUEUED = "llm:requests_queued"
RULE_TTFT = {p: "llm:time_to_first_token_seconds:p%d" % int(round(p * 100)) for p in (0.5, 0.9, 0.99)}
RULE_TPOT = {p: "llm:time_per_output_token_seconds:p%d" % int(round(p * 100)) for p in (0.5, 0.9, 0.99)}
RULE_E2E = {p: "llm:end_to_end_response_seconds:p%d" % int(round(p * 100)) for p in (0.5, 0.9, 0.99)}


# --------------------------------------------------------------------------- #
# expression builders
# --------------------------------------------------------------------------- #


def bare(names: str) -> str:
    """`{__name__=~"a|b"}` — several vendor spellings in one selector."""
    return '{__name__=~"%s"}' % names


def sel(names: str, *, model: bool = True, model_label: str = "model") -> str:
    """A name union clamped by the dashboard's variables.

    `$job` / `$instance` always apply; `$model` only where the label can exist (a
    gauge like `up` has no model). All three default to All with allValue `.*`, so
    an unset variable widens the selector rather than emptying the panel.
    `model_label` picks which spelling the $model filter matches (`model` normally,
    `model_name` on vLLM).
    """
    parts = ['__name__=~"%s"' % names, 'job=~"$job"', 'instance=~"$instance"']
    if model:
        parts.append('%s=~"$model"' % model_label)
    return "{" + ",".join(parts) + "}"


def raw(selector: str) -> str:
    """A plain (non-`__name__`) metric selector, clamped by the same variables."""
    return selector + '{job=~"$job",instance=~"$instance"}'


def var(name: str) -> str:
    """A recording-rule alias, clamped by the same variables as the raw series."""
    return '%s{job=~"$job",instance=~"$instance",model=~"$model"}' % name


def suffix(stems: str, tail: str) -> str:
    """`"a|b"` + `"_bucket"` -> `"a_bucket|b_bucket"` (histogram family union)."""
    return "|".join(stem + tail for stem in stems.split("|"))


def rate(selector: str) -> str:
    return "sum(rate(%s[$__rate_interval]))" % selector


def rate_by(selector: str, labels: str) -> str:
    return "sum by (%s) (rate(%s[$__rate_interval]))" % (labels, selector)


def gauge_sum(selector: str) -> str:
    return "sum(%s)" % selector


def gauge_max(selector: str) -> str:
    return "max(%s)" % selector


def gauge_by(selector: str, labels: str) -> str:
    return "sum by (%s) (%s)" % (labels, selector)


def by_model(expr: str) -> str:
    """Group by one canonical `model` label, however the server spells it.

    `label_replace(..., "model", "$1", "model_name", "(.+)")` copies vLLM's
    `model_name` onto `model`; the `.+` matters — with `(.*)` an *absent* model_name
    counts as a match on the empty string and silently blanks a perfectly good
    `model` label (verified against a live Prometheus: llama.cpp series served by
    file_sd labels came out as "unknown"). The second `label_replace` then turns
    "neither label anywhere" into the literal `unknown` so the legend is never blank.

    `model_filter_union()` explains why the $model filter has to be applied to both
    spellings and why that cannot be done with one selector.
    """
    normalized = (
        'label_replace(label_replace(%s, "model", "$1", "model_name", "(.+)"), '
        '"model", "unknown", "model", "^$")' % expr
    )
    return "sum by (model) (%s)" % normalized


def by_model_rate(names: str) -> str:
    """Rate over a token counter, grouped by the canonical model, filtered by $model.

    The filter is `A{model=~"$model"} or A{model_name=~"$model"}`: PromQL has no
    "this label OR that label" matcher, and `label_match()` (which could do it after
    normalization) is a 3.x extended function that the queried Prometheus has to be
    started with --enable-feature=promql-extended-functions to know about, which a
    dashboard may not assume. `or` unions the two branches and keeps the left-hand
    value where a series matches both, so:

      * $model = All (.*)  -> both branches match everything, union is the whole set;
      * a llama.cpp `model` -> left branch matches it;
      * a vLLM `model_name` -> right branch matches it.

    With one branch instead of two, picking a specific model silently drops whichever
    spelling it does not know — the kind of empty panel that gets blamed on the server.
    """
    grouped = "sum by (job, instance, model, model_name) (rate(%s[$__rate_interval]))"
    left = grouped % sel(names)
    right = grouped % sel(names, model_label="model_name")
    return by_model("%s or %s" % (left, right))


def p_label(probability: float) -> str:
    """0.5 -> "0.5", 0.99 -> "0.99" (keeps noise out of the JSON diff)."""
    return ("%f" % probability).rstrip("0").rstrip(".")


def quantile(probability: float, stems: str) -> str:
    """`histogram_quantile(p, sum by (le) (rate(<family>_bucket[$__rate_interval])))`.

    `sum by (le)` happens *before* histogram_quantile: a quantile of per-series
    quantiles is meaningless, and aggregating first is what lets several servers or
    models share one honest line instead of shifting each other's answer. `le` has to
    survive that aggregation because it is what histogram_quantile reads.
    """
    return "histogram_quantile(%s, sum by (le) (rate(%s[$__rate_interval])))" % (
        p_label(probability),
        sel(suffix(stems, "_bucket")),
    )


def mean_from_histogram(stems: str) -> str:
    """Arithmetic mean from `_sum` / `_count` — the numbers that exist without buckets.

    Some builds ship a summary (mean available) but no buckets (no quantiles). That
    case is invisible unless a panel asks for it, so every latency panel plots this
    next to the quantiles: quantiles empty + a mean drawn means "averages only", which
    is a different situation from "no latency metrics at all".
    """
    total = "sum(rate(%s[$__rate_interval]))" % sel(suffix(stems, "_sum"))
    count = "sum(rate(%s[$__rate_interval]))" % sel(suffix(stems, "_count"))
    return "%s / clamp_min(%s, 1)" % (total, count)


def ratio(numerator: str, denominator: str) -> str:
    """`numerator / clamp_min(denominator, 1)` — the house ratio shape."""
    return "%s / clamp_min(%s, 1)" % (numerator, denominator)


#: Prompt-cache hit ratio. llama.cpp's prompt_tokens_total counts misses only, so the
#: denominator is misses + hits.
PROMPT_CACHE_HIT_RATIO = ratio(
    rate(sel(CACHE_CACHED)),
    rate(sel(PROMPT_TOKENS)) + " + " + rate(sel(CACHE_CACHED)),
)
#: Speculative-decode acceptance: accepted / proposed.
DRAFT_ACCEPT_RATIO = ratio(rate(sel(DRAFT_ACCEPTED)), rate(sel(DRAFT_TOTAL)))
#: Draft tokens spent per token accepted — the multiplier on wasted draft work.
DRAFT_PER_ACCEPTED = ratio(rate(sel(DRAFT_TOTAL)), rate(sel(DRAFT_ACCEPTED)))
#: Structured-output share of requests.
STRUCTURED_SHARE = ratio(rate(sel(REQUESTS_STRUCTURED)), rate(sel(REQUESTS_TOTAL)))
#: (verify + decide + estimated-draft - actual-draft) / verify. Below 1, speculation
#: costs wall-clock time rather than buying it.
SPEC_TIME_RATIO = "(%s + %s + %s - %s) / clamp_min(%s, 1)" % (
    rate(sel(SPEC_VERIFY)),
    rate(sel(SPEC_DECIDE)),
    rate(sel(SPEC_ESTIMATE)),
    rate(sel(SPEC_DRAFT)),
    rate(sel(SPEC_VERIFY)),
)


# --------------------------------------------------------------------------- #
# panel constructors
# --------------------------------------------------------------------------- #

LEGEND_TABLE = {
    "calcs": ["lastNotNull", "max"],
    "displayMode": "table",
    "placement": "bottom",
    "showLast": True,
}
LEGEND_LIST = {"calcs": ["lastNotNull"], "displayMode": "list", "placement": "right", "showLast": True}


def describe(notes: str, targets: list[dict]) -> str:
    """Panel description = the prose *and* the expressions it describes.

    Generated from the targets so the two cannot disagree — check_monitoring.py
    asserts every panel's description contains each of its own expressions.
    """
    lines = [notes.strip(), "", "PromQL sent by this panel:", "", "```promql"]
    for item in targets:
        lines.append("%s: %s" % (item.get("refId", "A"), item.get("expr", "")))
    lines.append("```")
    return "\n".join(lines)


def target(expr: str, legend: str, ref: str = "A") -> dict:
    return {
        "datasource": DATASOURCE,
        "editorMode": "code",
        "expr": expr,
        "legendFormat": legend,
        "range": True,
        "refId": ref,
        # Min step == the scrape interval (see SCRAPE_INTERVAL).
        "interval": SCRAPE_INTERVAL,
        "intervalFactor": 1,
    }


def _defaults(*, unit, decimals, min_, max_, thresholds, band, no_value):
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
        "thresholds": {
            "mode": "absolute",
            "steps": thresholds or [{"color": "green", "value": None}],
        },
        "unit": unit,
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    if min_ is not None:
        defaults["min"] = min_
    if max_ is not None:
        defaults["max"] = max_
    if no_value:
        # What Grafana paints instead of the words "No data". On the latency row that
        # string is the diagnosis, not a shrug.
        defaults["noValue"] = no_value
    if band and thresholds and len(thresholds) > 1:
        defaults["custom"]["thresholdsStyle"] = {"mode": "dashed"}
    return defaults


def _panel(kind, title, notes, targets, *, pid, grid, unit="short", decimals=None,
           min=None, max=None, thresholds=None, band=False, no_value=None,
           legend=None, repeat=None, max_per_row=2):
    pid = pid or next(FREE_IDS)
    w, h = grid
    body: dict = {
        "id": pid,
        "type": kind,
        "title": title,
        "description": describe(notes, targets),
        "datasource": DATASOURCE,
        "gridPos": {"h": h, "w": w, "x": 0, "y": 0},
        "targets": targets,
        "fieldConfig": {
            "defaults": _defaults(unit=unit, decimals=decimals, min_=min, max_=max,
                                  thresholds=thresholds, band=band, no_value=no_value),
            "overrides": [],
        },
    }
    if kind == "stat":
        body["options"] = {
            "colorMode": "value",
            "graphMode": "area",
            "justifyMode": "auto",
            "orientation": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "value_and_name",
            "wideLayout": True,
        }
        # Stats are point-in-time reads, not ranges.
        body["targets"] = [dict(t, range=False, instant=True) for t in targets]
        body["fieldConfig"]["defaults"]["custom"] = {}
    else:
        body["options"] = {
            "legend": legend or LEGEND_TABLE,
            "tooltip": {"mode": "multi", "sort": "desc"},
        }
        if thresholds and len(thresholds) > 1 and not band:
            body["fieldConfig"]["defaults"]["custom"]["thresholdsStyle"] = {"mode": "line"}
    if repeat:
        # Repeat-by-variable: one clone of this panel per selected value of $repeat.
        body["repeat"] = repeat
        body["repeatDirection"] = "h"
        body["maxPerRow"] = max_per_row
    return body


def ts(title, notes, targets, *, pid=None, unit="short", decimals=None, min=None,
       max=None, thresholds=None, band=False, no_value=None, legend=None,
       w=12, h=8, repeat=None, max_per_row=2):
    """A time-series panel."""
    return _panel("timeseries", title, notes, targets, pid=pid, grid=(w, h), unit=unit,
                  decimals=decimals, min=min, max=max, thresholds=thresholds, band=band,
                  no_value=no_value, legend=legend, repeat=repeat, max_per_row=max_per_row)


def stat(title, notes, targets, *, w=4, h=4, **kwargs):
    """A single-number panel (instant queries, no range)."""
    return _panel("stat", title, notes, targets, pid=kwargs.pop("pid", None), grid=(w, h),
                  **kwargs)


def markdown(title, text, *, w=24, h=2):
    return {
        "id": next(FREE_IDS),
        "type": "text",
        "title": title,
        "description": text,
        "datasource": DATASOURCE,
        "gridPos": {"h": h, "w": w, "x": 0, "y": 0},
        "options": {
            "code": {"language": "plaintext", "showLineNumbers": False},
            "content": text,
            "mode": "markdown",
        },
    }


# --------------------------------------------------------------------------- #
# template variables
# --------------------------------------------------------------------------- #


def query_variable(name, label, query, *, hint, refresh=2):
    """A Prometheus-query variable that degrades to All (=.*) instead of erroring.

    Grafana parses `label_values(a, b)` with a regex that cannot contain a comma in
    either half, and hands the first half to Prometheus as `match[]` — so it must be a
    plain series selector. No `sum(...)`, no `label_replace(...)`, no commas: anything
    fancier fails to parse and the variable silently offers no values, which is how a
    dashboard ends up filtering on an empty string and rendering nothing anywhere.
    Normalizing label names is therefore done *in the panels* (see by_model()) and in
    the recording rules, not in the variable query.
    """
    return {
        "name": name,
        "label": label,
        "type": "query",
        "query": {"query": query, "uid": "${datasource}"},
        "definition": query,
        "current": {"text": "All", "value": "$__all", "selected": True},
        "hide": 0,
        "includeAll": True,
        "allValue": ".*",
        "multi": True,
        "options": [],
        # 2 = refresh on time-range change as well as on load, so a server that comes
        # up later appears without a reload.
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
            "Panels reference ${datasource} instead of a uid baked into the JSON: the "
            "uid is provisioned from git (datasource.yml -> 'prometheus'), and a uid "
            "handmade in the UI is how a promoted dashboard ends up pointed at a "
            "datasource that does not exist."
        ),
    },
    query_variable(
        "job",
        "Scrape job",
        "label_values(up, job)",
        hint="Which scrape path produced a series: 'llm-server' is the direct target, "
             "'llm-server-via-proxy' is monitor/serve.py's passthrough (by default it "
             "carries only llm_monitor_* — see monitoring/README.md, 'The two llm "
             "scrape jobs'). 'prometheus' is the self-scrape and has no model series, "
             "so leaving it selected is harmless. Derived from up, so the list is empty "
             "rather than wrong when nothing is being scraped.",
    ),
    query_variable(
        "instance",
        "Instance",
        'label_values(up{job=~"$job"}, instance)',
        hint="host:port of a scrape target, from up/instance. monitor/serve.py's Grafana "
             "deep links set var-instance to the upstream's host:port, which is the "
             "mapping that makes a link from the live page land on the right series. "
             "Declared after $job so it narrows with it.",
    ),
    query_variable(
        "model",
        "Model",
        'label_values({__name__=~"'
        + DECODE_TOKENS + "|" + PROMPT_TOKENS + "|" + RULE_DECODE
        + '"}, model)',
        hint="Only servers that are labelled show up: llama.cpp does not label its own "
             "metrics at all (the `model` here normally comes from "
             "monitoring/prometheus/targets/llm-servers.json file_sd labels), and vLLM's "
             "own label is model_name, which the llm:* recording rules normalize onto "
             "`model` — hence the alias in the selector. An empty list means no model "
             "label anywhere: All (.*) then keeps every panel working, and the per-model "
             "panel draws one line called 'unknown'.",
    ),
]


# --------------------------------------------------------------------------- #
# rows
# --------------------------------------------------------------------------- #
# Each panel's notes say what the expression means and what it does not cover;
# describe() appends the expressions themselves. Values the bespoke page derived in
# JavaScript are labelled as such — workspace-szw.11 deletes the JS once these are
# trusted, so the panels above them must be able to stand alone.
# --------------------------------------------------------------------------- #


def overview_row():
    panels = [
        stat(
            "requests in flight",
            "Instant gauges, no rate(): `sum(llamacpp:requests_processing)` and "
            "`sum(llamacpp:requests_deferred)` (halogen's `requests_running` / "
            "`requests_queued` spellings match too).\n\ndeferred > 0 means arrivals are "
            "waiting for a slot — the earliest sign the KV budget is the limit."
            + CAVEAT_NO_DATA,
            [
                target(gauge_sum(sel(REQUESTS_PROCESSING)), "processing"),
                target(gauge_sum(sel(REQUESTS_DEFERRED)), "deferred"),
            ],
            w=4, h=5,
        ),
        stat(
            "KV cache used",
            "`max(llamacpp:kv_cache_usage_ratio)` — the same series the "
            "LLMKVCachePressure alerts read, so this tile and a page cannot disagree. "
            "1 = 100 % of the pool.",
            [target(gauge_max(sel(KV_RATIO)), "usage")],
            unit="percentunit", decimals=1, min=0, max=1, w=4, h=5,
            thresholds=KV_BANDS,
        ),
        stat(
            "decode tok/s",
            "Instant read of the same rate() as the decode panel below: windowed "
            "throughput right now, not a lifetime average.",
            [target(rate(sel(DECODE_TOKENS)), "decode tok/s")],
            unit="short", decimals=1, w=4, h=5,
        ),
        stat(
            "prompt-cache hit ratio",
            "The windowed ratio from the Cache & KV row as a single number. llama.cpp's "
            "prompt_tokens_total counts cache MISSES, hence the `+ cached` in the "
            "denominator.",
            [target(PROMPT_CACHE_HIT_RATIO, "cache hit ratio")],
            unit="percentunit", decimals=1, min=0, max=1, w=4, h=5,
            thresholds=[{"color": "red", "value": None},
                        {"color": "yellow", "value": 0.5},
                        {"color": "green", "value": 0.8}],
        ),
        stat(
            "draft acceptance",
            "Instant value of the speculative-decode acceptance ratio plotted in the "
            "Speculative decode row. No data simply means this server is not speculating "
            "(no draft counters) — not that speculation is failing.",
            [target(DRAFT_ACCEPT_RATIO, "accept ratio")],
            unit="percentunit", decimals=1, min=0, max=1, w=4, h=5,
            no_value="no speculative decode on this server",
        ),
        stat(
            "TTFT p99",
            "Instant value of panel 3's p99. 'No histogram' means exactly that: this "
            "build exposes no time_to_first_token histogram, so the server's own log is "
            "the only latency source (see the Latency row).",
            [target(quantile(0.99, _TTFT), "p99")],
            unit="s", decimals=2, w=4, h=5, no_value="no histogram on this server",
        ),
        ts(
            "Decode throughput (tok/s)",
            "Windowed rate over the generation-token counter, summed across whatever "
            "survives the $job / $instance / $model filters, plus three reference lines:\n\n"
            "* **{{instance}}** — the same rate per target, so two servers on one box "
            "stay separate lines instead of being silently added together;\n"
            "* **server gauge** — `llamacpp:predicted_tokens_seconds`, the server's own "
            "'average since last scrape' number, which is what the bespoke page rendered. "
            "Overlaid deliberately during the migration so the two definitions are "
            "comparable, and the first thing workspace-szw.11 deletes once rate() is "
            "trusted. Where they disagree, rate() is right: the gauge resets with the "
            "process and knows nothing about the window;\n"
            "* **(rule)** — the `llm:decode_tokens_per_second` alias.\n\n"
            "This is the panel that answers 'what was decode throughput at 03:00', which "
            "the bespoke page could not answer at all." + CAVEAT_RATE + CAVEAT_RULE
            + CAVEAT_VARS,
            [
                target(rate(sel(DECODE_TOKENS)), "decode tok/s (all selected)"),
                target(rate_by(sel(DECODE_TOKENS), "instance, job"), "{{instance}} {{job}}"),
                target(gauge_sum(sel(DECODE_GAUGE)), "server gauge (avg since last scrape)"),
                target(var(RULE_DECODE), "llm:decode_tokens_per_second (rule)"),
            ],
            pid=1, unit="short", decimals=2, w=12, h=8,
        ),
        ts(
            "Prompt throughput (tok/s)",
            "Prefill throughput: windowed rate over the prompt-token counter.\n\n"
            "llama.cpp counts only the prompt tokens that MISSED the prompt cache in "
            "this counter (halogen counts the served-from-cache ones separately), which "
            "is why the cache panel computes cached / (processed + cached) instead of "
            "cached / processed. `llamacpp:prompt_tokens_seconds` (the server's own "
            "average gauge) is overlaid for the same migration reason as on the decode "
            "panel, and leaves with it." + CAVEAT_RATE + CAVEAT_RULE,
            [
                target(rate(sel(PROMPT_TOKENS)), "prompt tok/s (all selected)"),
                target(rate_by(sel(PROMPT_TOKENS), "instance, job"), "{{instance}} {{job}}"),
                target(gauge_sum(sel(PROMPT_GAUGE)), "server gauge (avg since last scrape)"),
                target(var(RULE_PROMPT), "llm:prompt_tokens_per_second (rule)"),
            ],
            pid=2, unit="short", decimals=1, w=12, h=8,
        ),
        ts(
            "Requests running / queued",
            "Gauges, not rates: running vs deferred vs configured slot capacity. "
            "`deferred > 0` for minutes is queueing, and LLMQueueing in rules.yml fires "
            "on this same expression, so the panel and the alert cannot disagree.\n\n"
            "Deferred > 0 while running < slot capacity is a scheduling stall; deferred "
            "> 0 at full slots is plain saturation. Different incidents, and this is the "
            "panel that tells them apart.",
            [
                target(gauge_sum(sel(REQUESTS_PROCESSING)), "running"),
                target(gauge_sum(sel(REQUESTS_DEFERRED)), "queued"),
                target(gauge_sum(sel(SLOT_CAPACITY)), "slot capacity (halogen)"),
                target(var(RULE_RUNNING), "llm:requests_running (rule)"),
                target(var(RULE_QUEUED), "llm:requests_queued (rule)"),
            ],
            pid=5, w=8, h=8,
        ),
        ts(
            "Requests / s and structured share",
            "Request rate from the completed-request counter, and the fraction of "
            "requests decoded under a JSON schema "
            "(`halogen:structured_requests_total / halogen:requests_total`).\n\n"
            "The structured share is the useful one when a client changes behaviour: a "
            "schema jump usually precedes a decode-rate change, because grammar-"
            "constrained decoding does not overlap speculation well." + CAVEAT_RATE
            + CAVEAT_CLAMP,
            [
                target(rate(sel(REQUESTS_TOTAL)), "requests/s"),
                target(rate_by(sel(REQUESTS_TOTAL), "instance, job"), "{{instance}} {{job}}"),
                target(STRUCTURED_SHARE, "share under a JSON schema"),
                target(var(RULE_REQUESTS), "llm:requests_per_second (rule)"),
            ],
            unit="reqps", w=8, h=8,
        ),
        ts(
            "Decode tok/s per model",
            "The per-model comparison the bespoke page could not do. `label_replace` "
            "folds llama.cpp's file_sd `model` label and vLLM's `model_name` onto one "
            "`model` label and turns 'no model label at all' into the literal `unknown`, "
            "so the legend is never blank. The $model filter is applied to both spellings "
            "(`{model=~'$model'} or {model_name=~'$model'}`) because PromQL cannot match "
            "'either of two labels' in one selector and `label_match()` is an optional "
            "3.x extension this box does not assume. The `(rule)` line is the same answer "
            "computed by Prometheus, where the rules normalize the label once.\n\n"
            "On one llama.cpp server with no model label you get exactly one line called "
            "'unknown' — correct, not broken. Name the model in the labels of "
            "monitoring/prometheus/targets/llm-servers.json to get a name here.",
            [
                target(by_model_rate(DECODE_TOKENS), "{{model}}"),
                # The alias is already a rate (a recorded gauge), so it is grouped,
                # not rate()'d again — rate() of a gauge is empty, not a slower rate.
                # by_model() reuses the same unknown-label fallback as the raw line.
                target(by_model(var(RULE_DECODE)), "{{model}} (rule)"),
            ],
            unit="short", decimals=2, w=8, h=8,
        ),
        ts(
            "Decode tok/s — one panel per $instance",
            "Repeat-by-variable: Grafana clones this panel for each selected $instance "
            "value, so N servers get N charts instead of N lines competing for one axis. "
            "The expression is the per-target rate, so a clone carries its own instance "
            "only.\n\n"
            "Pin $instance to specific targets for a guaranteed one-panel-per-server; "
            "with All selected the clone set is whatever Grafana expands All to.",
            [target(rate_by(sel(DECODE_TOKENS), "instance, job"), "{{instance}} {{job}}")],
            unit="short", decimals=2, w=12, h=7, repeat="instance", max_per_row=2,
        ),
        ts(
            "Lifetime tok/s — the arithmetic the old tile did",
            "Two raw counters divided, no window: what `monitor/static/app.js` rendered "
            "before Prometheus existed here. Kept on purpose, as a floor to sanity-check "
            "the windowed rates against and as the thing to delete with the JS once "
            "parity is signed off (workspace-szw.11).\n\n"
            "It cannot say WHEN anything was slow, it averages the whole lifetime since "
            "start, and a restart silently re-bases it — which is the entire argument "
            "for the panels above." + CAVEAT_CLAMP,
            [
                target(ratio(gauge_sum(sel(DECODE_TOKENS)), gauge_sum(sel(DECODE_SECONDS))),
                       "tokens/s since server start"),
                target(rate(sel(DECODE_TOKENS)), "windowed rate for comparison"),
            ],
            unit="short", decimals=2, w=12, h=7,
        ),
    ]
    return ("Overview", panels, False)


def latency_row():
    panels = [
        markdown(
            "Before you read 'no histogram' on this row",
            "Quantiles need `_bucket` series. A `llama-server` that exposes counters and "
            "average gauges only — which is what most builds actually do — cannot answer "
            "p99 TTFT, and no dashboard can. Those panels therefore say **no histogram on "
            "this server** instead of the useless \"No data\", and each one also plots the "
            "arithmetic mean from `_sum` / `_count`, which exists even when buckets do "
            "not.\n\n"
            "The census panel at the bottom of this row states which latency families each "
            "instance publishes, so 'is this empty because of me or because of the "
            "server?' is answerable here rather than with curl.",
            h=3,
        ),
        ts(
            "Time to first token — p50 / p90 / p99",
            "Prefill + queue time before the first token: the latency a user actually "
            "notices, and the one the bespoke page could not render at all.\n\n"
            "`sum by (le)` happens *before* histogram_quantile: averaging per-series "
            "quantiles is meaningless, and aggregating first is what lets several servers "
            "or models share one honest line instead of shifting each other's answer. "
            "`le` must survive that aggregation because it is what histogram_quantile "
            "reads.\n\n"
            "The `(rule)` series are the recorded `llm:time_to_first_token_seconds:*` "
            "quantiles (ADR 0001 C3, added with these panels); if a rule is missing or "
            "stale the raw series still draw." + CAVEAT_RULE,
            [
                target(quantile(0.50, _TTFT), "p50"),
                target(quantile(0.90, _TTFT), "p90"),
                target(quantile(0.99, _TTFT), "p99"),
                target(mean_from_histogram(_TTFT), "mean (works without buckets)"),
                target(var(RULE_TTFT[0.5]), "llm:...:p50 (rule)"),
                target(var(RULE_TTFT[0.9]), "llm:...:p90 (rule)"),
                target(var(RULE_TTFT[0.99]), "llm:...:p99 (rule)"),
            ],
            pid=3, unit="s", decimals=3, w=24, h=10, legend=LEGEND_LIST,
            no_value="no histogram on this server (averages only, if even those exist)",
        ),
        ts(
            "Time per output token — p50 / p90 / p99",
            "Decode latency per generated token: how fast text streams once it has "
            "started. Roughly the reciprocal of tokens/s for one request, but the "
            "distribution is the point — a p99 here is a stall in the middle of a "
            "completion, which any throughput average hides.\n\n"
            "Families differ by build (`time_per_output_token_seconds` vs "
            "`time_per_token_seconds`), which is what the name union is for.",
            [
                target(quantile(0.50, _TPOT), "p50"),
                target(quantile(0.90, _TPOT), "p90"),
                target(quantile(0.99, _TPOT), "p99"),
                target(mean_from_histogram(_TPOT), "mean (works without buckets)"),
                target(var(RULE_TPOT[0.99]), "llm:...:p99 (rule)"),
            ],
            unit="s", decimals=3, w=12, h=9, legend=LEGEND_LIST,
            no_value="no histogram on this server (averages only, if even those exist)",
        ),
        ts(
            "End-to-end request time — p50 / p90 / p99",
            "Queue + prefill + decode for a whole request. It moves *before* the "
            "throughput panels when the box is oversubscribed, which is why it sits next "
            "to TTFT rather than in a row of its own.\n\n"
            "End-to-end growing while TTFT is flat means generation is being starved (KV "
            "pressure, speculation failing); both growing together means prefill or "
            "queueing." + CAVEAT_NO_DATA,
            [
                target(quantile(0.50, _E2E), "p50"),
                target(quantile(0.90, _E2E), "p90"),
                target(quantile(0.99, _E2E), "p99"),
                target(mean_from_histogram(_E2E), "mean (works without buckets)"),
                target(var(RULE_E2E[0.99]), "llm:...:p99 (rule)"),
            ],
            unit="s", decimals=2, w=12, h=9, legend=LEGEND_LIST,
            no_value="no histogram on this server (averages only, if even those exist)",
        ),
        ts(
            "Latency families this server publishes",
            "Not a metric about the model — a census of what the exposition actually "
            "contains, counted per instance: how many `_bucket` series (quantiles are "
            "possible), how many `_count` series (a mean is possible) and how many "
            "TTFT buckets there are (a one-bucket histogram quantiles to that bucket, so "
            "a suspiciously round p99 is usually this panel's fault, not the model's).\n\n"
            "Read it when a latency panel above is empty: counts here but no quantile "
            "means the server uses a family name this dashboard does not know yet — add "
            "the stem to the `_TTFT` / `_TPOT` / `_E2E` constants in make_dashboard.py. "
            "Nothing here at all means the server publishes no latency families.",
            [
                target("count by (instance, job) (%s)"
                       % bare(suffix(_TTFT, "_bucket") + "|" + suffix(_TPOT, "_bucket")
                              + "|" + suffix(_E2E, "_bucket")),
                       "bucket series"),
                target("count by (instance, job) (%s)"
                       % bare(suffix(_TTFT, "_count") + "|" + suffix(_TPOT, "_count")
                              + "|" + suffix(_E2E, "_count")),
                       "summary/histogram _count series"),
                target("count by (instance, job) (%s)" % bare(suffix(_TTFT, "_bucket")),
                       "TTFT buckets"),
            ],
            w=24, h=5, legend=LEGEND_LIST,
            no_value="no latency families on this server",
        ),
    ]
    return ("Latency", panels, True)


def cache_row():
    panels = [
        ts(
            "KV-cache pressure over time",
            "The 'was the KV cache full overnight?' panel: widen the range to 12h or "
            "24h and the answer is on the chart. Same series the LLMKVCachePressure "
            "alerts read, so this panel and a page cannot disagree.\n\n"
            "Bands are drawn at 80 % and 90 % as visual guides; the alert numbers that "
            "actually ship live in `prometheus/alert_thresholds.env` (warn 0.90, critical "
            "0.97) — the bands deliberately sit *below* the alerts so the chart warns "
            "before the pager does. `1h average` separates a spike from a sustained "
            "squeeze, and the per-instance lines keep two servers apart.\n\n"
            "A subquery (`[1h:5m]`) is used for the average because `avg_over_time` "
            "cannot range over the result of `max(...)` any other way — writing "
            "`avg_over_time(max(x)[1h])` is a syntax error, not a shortcut.",
            [
                target(gauge_max(sel(KV_RATIO)), "worst instance"),
                target("avg_over_time(%s[%s:5m])" % (gauge_max(sel(KV_RATIO)), "1h"),
                       "1h average of the worst instance"),
                target(gauge_by(sel(KV_RATIO), "instance, job"), "{{instance}} {{job}}"),
                target(var(RULE_KV), "llm:kv_cache_usage_ratio (rule)"),
            ],
            pid=4, unit="percentunit", decimals=1, min=0, max=1, w=12, h=9,
            thresholds=KV_BANDS, band=True, legend=LEGEND_LIST,
            no_value="no KV-cache metrics on this server",
        ),
        ts(
            "KV tokens in use vs pool capacity",
            "The absolute numbers behind the ratio: tokens resident in the cache, the "
            "size the pool was configured with (`halogen:kv_pool_positions`, or "
            "`llamacpp:context_size` on plain llama.cpp), and the positions the front-end "
            "slots reserved but are not using yet.\n\n"
            "The ratio is what to alert on; this is what to read alongside it. A ratio at "
            "95 % with a lot of reserved-but-idle tokens is a slot-sizing problem, not a "
            "context-length problem, and the fix is different.",
            [
                target(gauge_sum(sel(KV_TOKENS)), "in use"),
                target(gauge_sum(sel(KV_POOL)), "pool capacity"),
                target(gauge_sum(sel(KV_RESERVED)), "reserved by slots (halogen)"),
                target(gauge_by(sel(KV_TOKENS), "instance, job"), "in use {{instance}}"),
            ],
            unit="short", w=12, h=9, legend=LEGEND_LIST,
            no_value="no KV-cache metrics on this server",
        ),
        ts(
            "Prompt-cache hit ratio + draft-accept ratio",
            "The two ratios the bespoke page divided by hand in JavaScript, now as "
            "windowed rates:\n\n"
            "* cache hit ratio = `rate(prompt_tokens_cached_total) / (rate(prompt_tokens_"
            "total) + rate(prompt_tokens_cached_total))` — the `+ cached` in the "
            "denominator is the llama.cpp convention (prompt_tokens_total counts misses "
            "only);\n"
            "* draft acceptance = `rate(draft_tokens_accepted_total) / "
            "rate(draft_tokens_total)`.\n\n"
            "Plotted together because they explain each other: a cold prompt cache and a "
            "collapsing draft-accept ratio together mean something restarted, or slots are "
            "evicting each other's prefixes — which is what LLMPromptCacheCold pages on."
            + CAVEAT_CLAMP + CAVEAT_RULE,
            [
                target(PROMPT_CACHE_HIT_RATIO, "prompt-cache hit ratio"),
                target(DRAFT_ACCEPT_RATIO, "draft-accept ratio"),
                target(var(RULE_CACHE), "llm:prompt_cache_hit_ratio (rule)"),
                target(var(RULE_DRAFT), "llm:draft_acceptance_ratio (rule)"),
            ],
            pid=6, unit="percentunit", decimals=1, min=0, max=1, w=12, h=9,
            legend=LEGEND_LIST, no_value="no cache / speculation counters on this server",
        ),
        ts(
            "Prompt cache: served from cache vs reprocessed",
            "The two halves of the ratio above in tokens/s, because a ratio alone cannot "
            "tell a good day from an idle one: 100 % hit on two requests is not 100 % hit "
            "on two thousand.\n\n"
            "`served from cache` climbing while `reprocessed` stays flat is the cache "
            "doing its job. Both falling together means the prefix changed — a rebuilt "
            "system prompt, a model swap, a restart — and every TTFT after it pays for "
            "that work." + CAVEAT_RATE,
            [
                target(rate(sel(CACHE_CACHED)), "served from cache"),
                target(rate(sel(PROMPT_TOKENS)), "reprocessed (cache misses)"),
                target(rate_by(sel(CACHE_CACHED), "instance, job"), "cached {{instance}}"),
            ],
            unit="short", decimals=1, w=12, h=9, legend=LEGEND_LIST,
            no_value="no prompt-cache counters on this server",
        ),
    ]
    return ("Cache & KV", panels, True)


def spec_row():
    panels = [
        stat(
            "draft acceptance",
            "Instant value of the acceptance ratio: of the tokens the draft head "
            "proposed, how many the target model kept. Below ~0.5 the draft is mostly "
            "wasted work.",
            [target(DRAFT_ACCEPT_RATIO, "accept ratio")],
            unit="percentunit", decimals=1, min=0, max=1, w=6, h=5,
            thresholds=[{"color": "red", "value": None},
                        {"color": "yellow", "value": 0.5},
                        {"color": "green", "value": 0.75}],
            no_value="no speculative decode on this server",
        ),
        stat(
            "drafts per accepted token",
            "Draft tokens spent per token accepted — the multiplier on wasted draft "
            "work, and the number to quote when arguing a drafter is not earning its "
            "keep.",
            [target(DRAFT_PER_ACCEPTED, "draft tokens per accepted")],
            decimals=2, min=0, w=6, h=5,
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 2},
                        {"color": "red", "value": 4}],
            no_value="no speculative decode on this server",
        ),
        stat(
            "speculation time ratio",
            "`(verify + decide + estimated-draft - actual-draft) / verify`. Below 1, "
            "speculation costs more wall-clock than it saves; the break-even line is "
            "drawn at 1 on the panel below.\n\n"
            "The `- actual-draft` term is there because llama.cpp reports draft time "
            "separately from the speculation window; without it the ratio reads high on "
            "every MTP build.",
            [target(SPEC_TIME_RATIO, "time ratio")],
            decimals=2, min=0, w=6, h=5,
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
            no_value="no speculative decode on this server",
        ),
        stat(
            "draft tokens / s",
            "The draft head's own throughput, next to the accepted subset. Proposed is "
            "always the larger number; a draft rate that jumps while decode flatlines is "
            "speculation spinning against a stalled target model.",
            [target(rate(sel(DRAFT_TOTAL)), "proposed tok/s"),
             target(rate(sel(DRAFT_ACCEPTED)), "accepted tok/s")],
            unit="short", decimals=1, w=6, h=5,
            no_value="no speculative decode on this server",
        ),
        ts(
            "Draft acceptance over time",
            "The acceptance ratio with the recording-rule alias beside it, plus the "
            "reciprocal in tokens. A collapsing acceptance ratio and a rising 'drafts per "
            "accept' are the same event described twice; having both on the chart is "
            "cheaper than doing the arithmetic at 3am." + CAVEAT_CLAMP + CAVEAT_RULE,
            [
                target(DRAFT_ACCEPT_RATIO, "accept ratio"),
                target(var(RULE_DRAFT), "llm:draft_acceptance_ratio (rule)"),
                target(DRAFT_PER_ACCEPTED, "draft tokens per accepted"),
            ],
            unit="percentunit", decimals=1, w=12, h=8, legend=LEGEND_LIST,
            no_value="no speculative decode on this server",
        ),
        ts(
            "Draft tokens: proposed / accepted / rejected",
            "The three counters in tokens/s. `proposed = accepted + rejected` unless this "
            "build has no rejected counter — then that line is simply absent, which is a "
            "missing metric, not a zero.\n\n"
            "Read the shape: accepted stepping down while proposed holds is the drafter "
            "getting worse (new prompt distribution); both dropping is speculation having "
            "been switched off or the drafter unloading.",
            [
                target(rate(sel(DRAFT_TOTAL)), "proposed"),
                target(rate(sel(DRAFT_ACCEPTED)), "accepted"),
                target(rate(sel(DRAFT_REJECTED)), "rejected"),
            ],
            unit="short", decimals=1, w=12, h=8, legend=LEGEND_LIST,
            no_value="no speculative decode on this server",
        ),
        ts(
            "Speculative decode time budget",
            "`rate()` over the four speculative timers: verify, decide, estimated-draft "
            "and actual-draft.\n\n"
            "If draft time grows faster than verify time, speculation itself is the "
            "bottleneck. If verify dominates, speculation has stopped paying and the "
            "drafter (or `--specdraft`) deserves a second look. Pairs with the time-ratio "
            "tile above: that is the verdict, this is the evidence.",
            [
                target(rate(sel(SPEC_VERIFY)), "verify"),
                target(rate(sel(SPEC_DECIDE)), "decide"),
                target(rate(sel(SPEC_ESTIMATE)), "estimated draft"),
                target(rate(sel(SPEC_DRAFT)), "actual draft"),
            ],
            unit="s", decimals=2, w=24, h=8, legend=LEGEND_LIST,
            no_value="no speculative timers on this server",
        ),
    ]
    return ("Speculative decode", panels, True)


def scrape_row():
    panels = [
        stat(
            "targets up",
            "`sum(up{job=~\"$job\"})` beside `count(up{...})`: how many scrape targets "
            "answered their last scrape. Any shortfall is the real explanation for every "
            "empty panel elsewhere on this dashboard.",
            [
                target(gauge_sum(raw("up")), "up"),
                target("count(%s)" % raw("up"), "targets configured"),
            ],
            w=6, h=5, min=0, no_value="nothing scraped for this job selection",
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
        ),
        stat(
            "monitor scrape ok",
            "`min(llm_monitor_scrape_ok)` — 1 when monitor/serve.py's own passthrough "
            "scrape of the model server last succeeded, 0 when it failed, and 'no monitor "
            "scraped' when nothing is scraping the proxy at all (a direct-only setup, "
            "which is a supported configuration).\n\n"
            "This is the tile that separates 'the model is idle' from 'the thing watching "
            "the model is blind'." + CAVEAT_NO_DATA,
            [target("min(%s)" % MONITOR_SCRAPE_OK, "scrape ok")],
            w=6, h=5, min=0, max=1,
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
            no_value="no monitor scrape (direct-only setup?)",
        ),
        stat(
            "monitors reporting",
            "`count(llm_monitor_build_info)`: how many distinct monitor processes are "
            "reporting. The value is always 1 per process; the information is in the "
            "labels (version, upstream). A model server with no /metrics still produces "
            "this series, which is how you tell a proxy that is up from a proxy that is "
            "blind.",
            [target("count(%s)" % MONITOR_BUILD_INFO, "monitors reporting")],
            w=6, h=5, no_value="no monitor scraped yet",
        ),
        stat(
            "alerts firing",
            "`count(ALERTS{alertstate=\"firing\"})`. Here because this box wants one "
            "place to look: Grafana reads the alert list off the same provisioned "
            "datasource Prometheus evaluated them with.",
            [target('count(ALERTS{alertstate="firing"})', "firing")],
            w=6, h=5,
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
            no_value="none firing",
        ),
        ts(
            "Scrape health (llm_monitor_*) + staleness",
            "Three failure layers in one panel, read top-down:\n\n"
            "* `up{job=~\"$job\"}` — 0 means Prometheus could not reach the target at all: "
            "nothing was scraped, and every other panel on this dashboard is showing "
            "history;\n"
            "* `min(llm_monitor_scrape_ok)` — 0 means the monitor is up but *its* scrape "
            "of the model server failed (no /metrics route, timeout, 5xx). Different "
            "incident, different runbook section, and precisely the distinction the "
            " bespoke 'is it down?' tile could not make;\n"
            "* `increase(llm_monitor_scrape_success_total[...])` — successful passthrough "
            "scrapes in the window, the canonical 'is the monitor actually scraping' "
            "signal (ADR 0001 C2);\n"
            "* `time() - timestamp(up{...})` — how stale the newest sample is, which is "
            "how a target that answers but has stopped advancing (a proxy replaying a "
            "buffered response) becomes visible.\n\n"
            "The `llm_monitor_*` series are deliberately NOT clamped by $job/$instance: "
            "'is the telemetry pipeline itself alive' has to stay answerable while the "
            "variables are narrowed to one server, and the `upstream` label already "
            "separates several monitors (one proxy can front several servers via "
            "`/metrics?url=`).",
            [
                target(raw("up"), "{{job}} {{instance}} up"),
                target("min(%s)" % MONITOR_SCRAPE_OK, "monitor scrape ok"),
                target("sum(increase(%s[$__rate_interval]))" % MONITOR_SCRAPE_SUCCESS,
                       "successful monitor scrapes in window"),
                target("max(time() - timestamp(%s))" % raw("up"), "worst sample age (s)"),
            ],
            pid=7, w=12, h=8, legend=LEGEND_LIST,
        ),
        ts(
            "Monitor scrape latency and upstream status",
            "`llm_monitor_scrape_latency_ms` and `llm_monitor_scrape_upstream_status`, "
            "one series per `upstream` label (one proxy can front several servers via "
            "`/metrics?url=`).\n\n"
            "serve.py caps an upstream call at 5 s, so a latency line pinned at 5000 ms "
            "means the cap fired and Prometheus saw a scrape failure (502), not a "
            "slow-but-successful scrape. Status 0 means no answer at all.",
            [
                target(MONITOR_LATENCY, "{{upstream}} ms"),
                target(MONITOR_STATUS, "{{upstream}} status"),
            ],
            unit="ms", decimals=0, w=12, h=8, legend=LEGEND_LIST,
            no_value="no monitor scrape (direct-only setup?)",
        ),
        ts(
            "Monitor scrape success rate",
            "`rate(llm_monitor_scrape_success_total[$__rate_interval])` per upstream. "
            "With the 15 s scrape interval the healthy value is about 0.067/s — one "
            "successful scrape per interval — and it falls to 0 exactly when the monitor "
            "goes blind, with counter resets (a monitor restart) showing as a gap.\n\n"
            "Plotted against `up` so 'Prometheus could not scrape the monitor' and 'the "
            "monitor could not scrape the model' are distinguishable on one chart.",
            [
                target("rate(%s[$__rate_interval])" % MONITOR_SCRAPE_SUCCESS,
                       "{{upstream}} scrapes/s"),
                target(raw("up"), "{{job}} {{instance}} up"),
            ],
            unit="ops", decimals=3, w=12, h=8, legend=LEGEND_LIST,
        ),
        ts(
            "Parse errors, truncation, series count",
            "`increase(llm_monitor_parse_errors_total[$__rate_interval])`, "
            "`max(llm_monitor_parse_truncated)` and `llm_monitor_series_count`.\n\n"
            "The passthrough hands the exposition text to Prometheus verbatim no matter "
            "what these say — they describe the monitor's *parsed* view (2000-series cap, "
            "one deliberately simple parser). Non-zero parse errors means a server changed "
            "its format and it is the derived panels, not the raw text, that should be "
            "distrusted.",
            [
                target("sum(increase(%s[$__rate_interval]))" % MONITOR_PARSE_ERRORS,
                       "parse errors in window"),
                target("max(%s)" % MONITOR_PARSE_TRUNCATED, "series cap hit"),
                target(MONITOR_SERIES, "{{upstream}} series kept"),
            ],
            w=12, h=8, legend=LEGEND_LIST,
            no_value="no monitor scrape (direct-only setup?)",
        ),
        markdown(
            "Monitoring the monitor",
            "Prometheus only knows it is down when something else says so. These panels "
            "read `llm_monitor_*`, published by `monitor/serve.py`'s `/metrics` "
            "passthrough, and `GET /api/scrape-health` on the live page reads the same "
            "story back out of Prometheus' `/api/v1/targets` — so a dead Prometheus is "
            "visible without opening Grafana at all. See `monitoring/README.md` and "
            "ADR 0001 C2.",
            h=2,
        ),
    ]
    return ("Scrape health", panels, True)


# --------------------------------------------------------------------------- #
# layout
# --------------------------------------------------------------------------- #


def flow(panels: list[dict], *, start_y: int) -> int:
    """Assign gridPos by flowing panels left-to-right, wrapping at 24 columns.

    Returns the height used, so the next row can start below it. Hand-written
    gridPos is why provisioned dashboards overlap after one insertion: every y below
    the insertion point has to be remembered by hand.
    """
    x = 0
    y = start_y
    row_height = 0
    for panel in panels:
        w = panel["gridPos"]["w"]
        h = panel["gridPos"]["h"]
        if x + w > 24:
            x = 0
            y += row_height
            row_height = 0
        panel["gridPos"] = {"h": h, "w": w, "x": x, "y": y}
        x += w
        row_height = max(row_height, h)
    return y + row_height - start_y


def build() -> dict:
    """Assemble the dashboard.

    Collapsed rows keep their panels nested inside the row entry (that is how
    Grafana stores them); an expanded row's children live at the top level with real
    gridPos, which is the shape Grafana writes for an expanded row and the one it is
    guaranteed to render without a re-layout on first open.
    """
    out: list[dict] = []
    y = 0
    for title, panels, collapsed in [
        overview_row(), latency_row(), cache_row(), spec_row(), scrape_row()
    ]:
        used = flow(panels, start_y=y + 1)
        entry: dict = {
            "id": next(ROW_IDS),
            "type": "row",
            "title": title,
            "collapsed": collapsed,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "datasource": DATASOURCE,
        }
        if collapsed:
            entry["panels"] = panels
            out.append(entry)
            y += 1
        else:
            entry["panels"] = []
            out.append(entry)
            out.extend(panels)
            y += used
    return {
        "annotations": {"list": []},
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,
        "id": None,
        "links": [],
        # 15s == the scrape interval: refreshing faster cannot show anything new.
        "refresh": "15s",
        # Dashboard-level minimum step, same reason; panels repeat it per target
        # because that is where Grafana reads it from for each query.
        "interval": SCRAPE_INTERVAL,
        "liveNow": False,
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
        "panels": out,
    }


def render() -> str:
    return json.dumps(build(), indent=2) + "\n"


def main(argv: list[str]) -> int:
    if "--check" in argv:
        want = render()
        have = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if want != have:
            sys.stderr.write(
                "STALE: %s is not what make_dashboard.py generates.\n"
                "Run: python3 monitoring/grafana/make_dashboard.py\n" % OUT
            )
            return 1
        print("%s is up to date with make_dashboard.py" % OUT)
        return 0
    text = render()
    OUT.write_text(text, encoding="utf-8")
    dash = json.loads(text)
    entries = dash["panels"]
    nested = sum(len(p.get("panels") or []) for p in entries if p.get("type") == "row")
    print("wrote %s: %d rows, %d panels (%d nested in collapsed rows)" % (
        OUT,
        sum(1 for p in entries if p.get("type") == "row"),
        sum(1 for p in entries if p.get("type") != "row") + nested,
        nested,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
