#!/usr/bin/env python3
"""Validate the monitoring stack without Docker: stdlib-only structural checks.

    python3 monitoring/check_monitoring.py            # ~all of it
    python3 monitoring/check_monitoring.py --promtool # also run promtool if installed

This is the "runnable locally with no Docker" half of the CI story; the other half
is `.github/workflows/monitoring.yml` (ticket workspace-szw.8), which wraps these
checks plus promtool and `docker compose config`. Everything here works on a
checkout with nothing but python3: the YAML is checked with a small purpose-built
reader rather than PyYAML
— the repo's rule is no dependencies to install, and the files being checked are
ours, written in a deliberately regular style.

What is actually being defended:

* rules.yml is the render of rules.yml.tmpl + alert_thresholds.env (drift fails);
* every alert carries `for:`, a severity, and a summary, and has a firing and a
  non-firing scenario in unit-tests.yml;
* prometheus.yml scrapes the model server directly *and* through the passthrough,
  caps retention by size, and does not rename metrics (ADR workspace-szw.1);
* the dashboard JSON is structurally sound, references its datasource by
  variable rather than a UI-generated uid, and contains no off-box URL;
* compose.yaml pins images, bounds memory and CPU, binds loopback, and sets both
  retention caps.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PROM_DIR = HERE / "prometheus"
GRAFANA_DIR = HERE / "grafana"
DASH_DIR = GRAFANA_DIR / "dashboards"

#: ADR 0001 C4 reserves these panel ids: `monitor/static/app.js` builds
#: `?viewPanel=<id>` links against them, so renumbering silently breaks the page's
#: Grafana links. Value = words that must appear in that panel's title.
RESERVED_PANELS = {
    1: ("decode",),
    2: ("prompt",),
    3: ("first token",),
    4: ("kv",),
    5: ("requests",),
    6: ("prompt-cache",),
    7: ("scrape health",),
}

FAILS: list[str] = []
CHECKS = 0


def check(condition: bool, message: str) -> bool:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILS.append(message)
    return bool(condition)


# --------------------------------------------------------------------------- #
# a deliberately small YAML reader
# --------------------------------------------------------------------------- #
# Enough of YAML to read the files this directory contains (block maps, block
# sequences, plain and quoted scalars, comments). Long expressions in rules.yml
# are written as `>-` folded scalars, which is the only block style used.


def _scalar(text: str):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(item) for item in inner.split(",")] if inner else []
    if text.startswith("{") and text.endswith("}"):
        inner = text[1:-1].strip()
        if not inner:
            return {}
        out = {}
        for item in inner.split(","):
            key, _, value = item.partition(":")
            out[_scalar(key)] = _scalar(value)
        return out
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~", ""):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def parse_yaml(text: str):
    """Parse the YAML subset this directory is written in.

    Block maps, block sequences (including ``- key: value`` items), plain and
    quoted scalars, inline ``[a, b]`` lists, ``>-`` folded scalars and comments.
    That is everything rules.yml / prometheus.yml / compose.yaml use; anything
    fancier (anchors, flow maps with nesting, multi-document) raises rather than
    being silently misread, which is the point of a validator.
    """
    lines: list[list] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        lines.append([len(raw) - len(raw.lstrip(" ")), raw.strip()])
    if not lines:
        return {}
    anchors: dict[str, object] = {}

    def folded(index: int, indent: int):
        parts = []
        while index < len(lines) and lines[index][0] > indent and not lines[index][1].startswith("#"):
            parts.append(lines[index][1])
            index += 1
        return " ".join(parts), index

    def parse_map(index: int, indent: int):
        out: dict = {}
        merges: list[dict] = []
        while index < len(lines):
            ind, content = lines[index]
            if ind != indent or content.startswith("- "):
                break
            if ind > indent:
                raise ValueError(f"unexpected indent: {content!r}")
            key, sep, rest = content.partition(":")
            if not sep:
                raise ValueError(f"not a key: {content!r}")
            key, rest = _scalar(key), rest.strip()
            anchor = None
            if rest.startswith("&"):
                anchor, _, rest = rest.partition(" ")
                anchor = anchor[1:]
                rest = rest.strip()
            index += 1
            value: object = None
            if rest in (">-", ">", "|", "|-"):
                value, index = folded(index, indent)
            elif rest:
                value = _scalar(rest)
            elif index < len(lines) and lines[index][0] > indent:
                value, index = parse(index, lines[index][0])
            elif index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("- "):
                value, index = parse_seq(index, indent)
            if anchor:
                anchors[anchor] = value
            if key == "<<":
                # compose/YAML merge keys: the anchor provides defaults, so keys
                # written on the service itself win.
                referenced = str(value or "").strip().lstrip("*")
                anchor_defaults = anchors.get(referenced)
                if isinstance(anchor_defaults, dict):
                    merges.append(anchor_defaults)
            else:
                out[key] = value
        for defaults in merges:
            for key, value in defaults.items():
                out.setdefault(key, value)
        return out, index

    def parse_seq(index: int, indent: int):
        out: list = []
        while index < len(lines):
            ind, content = lines[index]
            if ind != indent or not content.startswith("- "):
                break
            body = content[2:].strip()
            if not body:
                index += 1
                if index < len(lines) and lines[index][0] > indent:
                    child, index = parse(index, lines[index][0])
                    out.append(child)
                else:
                    out.append(None)
                continue
            if re.match(r"^[^:]+:( |$)", body):
                # `- key: value` starts a map whose remaining keys sit at indent+2
                lines[index] = [indent + 2, body]
                child, index = parse_map(index, indent + 2)
                out.append(child)
                continue
            out.append(_scalar(body))
            index += 1
        return out, index

    def parse(index: int, indent: int):
        if lines[index][1].startswith("- "):
            return parse_seq(index, indent)
        return parse_map(index, indent)

    value, _ = parse(0, lines[0][0])
    return value


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def check_render() -> None:
    result = subprocess.run(
        [sys.executable, str(HERE / "render_rules.py"), "--check"],
        capture_output=True,
        text=True,
    )
    check(
        result.returncode == 0,
        "rules.yml is stale: " + (result.stderr or result.stdout).strip(),
    )


def check_rules() -> list[str]:
    path = PROM_DIR / "rules.yml"
    text = path.read_text(encoding="utf-8")
    check("${" not in text, "rules.yml still contains ${PLACEHOLDER}s")
    data = parse_yaml(text)
    groups = data.get("groups") or []
    check(bool(groups), "rules.yml has no rule groups")

    alerts = []
    for group in groups:
        for rule in group.get("rules") or []:
            if "alert" not in rule:
                continue
            name = rule["alert"]
            alerts.append(name)
            check(bool(str(rule.get("expr", "")).strip()), f"{name}: no expr")
            check(bool(rule.get("for")), f"{name}: no `for:` (fires on a single blip)")
            labels = rule.get("labels") or {}
            check(
                labels.get("severity") in ("warning", "critical"),
                f"{name}: labels.severity must be warning or critical",
            )
            summary = str((rule.get("annotations") or {}).get("summary", "")).strip()
            check(len(summary) > 40, f"{name}: annotations.summary missing or too terse")
            # "names the consequence, not just the metric" is a style rule, but a
            # summary that is only metric words is worth flagging mechanically.
            check(
                " " in summary,
                f"{name}: summary looks like a metric name, not a consequence",
            )
    for required in REQUIRED_ALERTS:
        check(required in alerts, f"rules.yml is missing the {required} alert")
    return alerts


REQUIRED_ALERTS = [
    "LLMServerDown",
    "LLMMetricsAbsent",
    "LLMScrapeFailing",
    "LLMKVCachePressureWarn",
    "LLMKVCachePressureCritical",
    "LLMQueueing",
    "LLMDecodeThroughputCollapse",
    "LLMPromptCacheCold",
    "LLMStaleTarget",
    "LLMMonitorAbsent",
    "LLMScrapeParseErrors",
]


def check_unit_tests(alerts: list[str]) -> None:
    data = parse_yaml((PROM_DIR / "unit-tests.yml").read_text(encoding="utf-8"))
    firing: set[str] = set()
    non_firing: set[str] = set()
    for group in data.get("tests") or []:
        for case in group.get("alert_rule_test") or []:
            name = case.get("alertname")
            if not name:
                continue
            if case.get("exp_alerts"):
                firing.add(name)
            else:
                non_firing.add(name)
    for name in alerts:
        check(name in firing, f"unit-tests.yml has no firing scenario for {name}")
        check(name in non_firing, f"unit-tests.yml has no non-firing scenario for {name}")


def check_prometheus_config() -> None:
    text = (PROM_DIR / "prometheus.yml").read_text(encoding="utf-8")
    data = parse_yaml(text)
    glob = data.get("global") or {}
    check(glob.get("scrape_interval") == "15s", "global.scrape_interval should be 15s")
    check(glob.get("scrape_timeout") == "5s", "global.scrape_timeout should be 5s (serve.py cap)")
    check(bool(data.get("rule_files")), "rule_files is empty")
    for rf in data.get("rule_files") or []:
        check((PROM_DIR / str(rf)).is_file(), f"rule_files entry {rf} does not exist next to prometheus.yml")

    jobs = {job.get("job_name"): job for job in data.get("scrape_configs") or []}
    for needed in ("llm-server", "llm-server-via-proxy", "prometheus"):
        check(needed in jobs, f"scrape_configs is missing the {needed} job")
    # The ADR: keep native names. Relabelling __name__ is how a rename sneaks in.
    check(
        "target_label: __name__" not in text and "target_label: \"__name__\"" not in text,
        "prometheus.yml rewrites metric names (ADR says keep them native)",
    )
    for name, job in jobs.items():
        for relabel in (job or {}).get("metric_relabel_configs") or []:
            check("target_label" not in relabel, f"{name}: metric_relabel_configs must drop, not rename")
    check("file_sd_configs" in text, "the llm-server job should use file_sd so targets are a file")
    targets = PROM_DIR / "targets" / "llm-servers.json"
    check(targets.is_file(), "targets/llm-servers.json is missing")
    if targets.is_file():
        try:
            entries = json.loads(targets.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            check(False, f"targets/llm-servers.json is not valid JSON: {exc}")
            entries = []
        check(bool(entries), "targets/llm-servers.json is empty")
        for entry in entries if isinstance(entries, list) else []:
            check(bool(entry.get("targets")), f"target entry without targets: {entry}")
            for target in entry.get("targets", []):
                check(
                    "://" not in str(target),
                    f"file_sd target must be host:port, not a URL: {target}",
                )


def check_dashboard() -> None:
    path = GRAFANA_DIR / "dashboards" / "llm-server.json"
    raw = path.read_text(encoding="utf-8")
    check(not re.search(r"https?://", raw), "dashboard JSON contains an off-box URL")
    dash = json.loads(raw)
    check(dash.get("title") == "LLM Server", "dashboard title should be 'LLM Server'")
    check(
        dash.get("uid") == "llm-server",
        "dashboard uid must be 'llm-server' — the page's deep links are /d/llm-server/...",
    )
    check(isinstance(dash.get("schemaVersion"), int), "dashboard has no schemaVersion")
    check(dash.get("refresh") == "15s", "dashboard refresh should be 15s (the scrape interval)")
    check(
        (dash.get("time") or {}).get("from") == "now-6h",
        "default time range should be the last 6h",
    )

    variables = {v.get("name"): v for v in (dash.get("templating") or {}).get("list") or []}
    for name in ("datasource", "job", "instance", "model"):
        check(name in variables, f"dashboard is missing the ${name} template variable")
    for name, var in variables.items():
        if var.get("type") == "query":
            check(
                var.get("includeAll") is True and var.get("allValue") == ".*",
                f"${name} must default to All with allValue .* so an absent label degrades, not errors",
            )

    panels = []
    for entry in dash.get("panels") or []:
        if entry.get("type") == "row":
            panels.extend(entry.get("panels") or [])
        else:
            panels.append(entry)
    check(len(panels) >= 12, f"dashboard looks too small: {len(panels)} panels")
    ids = [entry.get("id") for entry in dash.get("panels") or []]
    ids += [p.get("id") for entry in dash.get("panels") or [] if entry.get("type") == "row"
            for p in entry.get("panels") or []]
    check(
        len(ids) == len(set(ids)),
        "panel ids are not unique dashboard-wide — rows share the id space with panels, "
        "and a duplicate id is how a deep link lands on the wrong chart",
    )
    by_id = {p.get("id"): p for p in panels}
    for pid, words in RESERVED_PANELS.items():
        title = str((by_id.get(pid) or {}).get("title", "<missing>"))
        check(
            all(word in title.lower() for word in words),
            f"panel id {pid} is reserved by ADR 0001 C4 for {' + '.join(words)} but is "
            f"titled {title!r}; deep links with viewPanel={pid} would land wrong",
        )
    for p in panels:
        title = p.get("title", "<untitled>")
        if p.get("type") == "text":
            # A text panel has no targets by design; it is documentation that ships
            # with the dashboard, so it still has to say something.
            content = str((p.get("options") or {}).get("content", ""))
            check(len(content.strip()) > 20, f"text panel {title!r} has no content")
        else:
            targets = p.get("targets") or []
            check(bool(targets), f"panel {title!r} has no targets")
            for t in targets:
                check(bool(str(t.get("expr", "")).strip()), f"panel {title!r} has an empty expr")
                check(
                    bool(str(t.get("legendFormat", "")).strip()),
                    f"panel {title!r} has a target with no legendFormat (legends go blank)",
                )
        check(
            len(str(p.get("description", "").strip())) > 30,
            f"panel {title!r} has no description stating its PromQL",
        )
        ds = p.get("datasource") or {}
        check(
            ds.get("uid") == "${datasource}",
            f"panel {title!r} pins a datasource uid instead of ${{datasource}}",
        )
    # the questions the epic says Grafana must answer
    joined = json.dumps(dash).lower()
    for token in ("histogram_quantile", "$__rate_interval", "clamp_min", "llm_monitor_scrape"):
        check(token in joined, f"dashboard does not use {token}")


#: The rows the dashboard ticket specifies. The dashboard is organised around these
#: questions and nothing else (a raw per-series dump stays on the proxy page).
DASHBOARD_ROWS = ("Overview", "Latency", "Cache & KV", "Speculative decode", "Scrape health")

#: Panel types that ship with Grafana. Anything else is a plugin, and plugins are
#: disabled in compose.yaml (GF_PLUGINS_ALLOW_LOADING: "false"): a panel of another
#: type is a dashboard that renders as an error on this box.
PANEL_TYPES = {"row", "timeseries", "stat", "text"}

#: Template variable types Grafana knows. A typo here ("type": "model") makes the
#: variable vanish in the UI with no error anywhere — the quietest possible failure.
VARIABLE_TYPES = {"query", "custom", "constant", "datasource", "textbox", "interval", "group", "adhoc"}


def flatten_panels(dash: dict) -> list[dict]:
    """Every panel in the dashboard, wherever it lives.

    A collapsed row nests its children; an expanded row's children are top-level
    entries with real gridPos. Reading only one of the two shapes silently skips
    half the dashboard — which is how a panel escapes validation entirely.
    """
    out: list[dict] = []
    for entry in dash.get("panels") or []:
        out.append(entry)
        if entry.get("type") == "row":
            out.extend(entry.get("panels") or [])
    return out


def balanced(expr: str) -> bool:
    """Cheap sanity on an expression: balanced pairs and an even number of quotes.

    Not a parser (promtool does that, when it is installed) — this is the
    promtool-independent floor that catches the truncation/typo class, including a
    generator that ate half a selector.
    """
    pairs = (("(", ")"), ("{", "}"), ("[", "]"))
    return expr.count('"') % 2 == 0 and all(expr.count(a) == expr.count(b) for a, b in pairs)


def recorded_aliases() -> set[str]:
    """Every `record:` name in recording_rules.yml (the aliases panels may use)."""
    path = PROM_DIR / "recording_rules.yml"
    if not path.is_file():
        return set()
    names = set()
    for group in (parse_yaml(path.read_text(encoding="utf-8")) or {}).get("groups") or []:
        for rule in group.get("rules") or []:
            if rule.get("record"):
                names.add(str(rule["record"]))
    return names


def check_dashboard_contract() -> None:
    """The dashboard ticket's acceptance criteria, as checks with no Grafana and no promtool.

    Each one names the failure it stands in front of, because a dashboard can be
    valid JSON, valid PromQL and still lie: a filter that is declared but never
    applied, a quantile panel that prints "No data" where it should say "this server
    has no histograms", an alias no recording rule writes, a hand-copied description
    that drifted from the query it documents. All of those render happily.
    """
    path = DASH_DIR / "llm-server.json"
    raw = path.read_text(encoding="utf-8")
    try:
        dash = json.loads(raw)
    except json.JSONDecodeError as exc:
        check(False, f"dashboard JSON does not parse: {exc}")
        return
    panels = flatten_panels(dash)
    charts = [p for p in panels if p.get("type") in ("timeseries", "stat")]

    # --- structure the deep links depend on -------------------------------- #
    rows = [p for p in panels if p.get("type") == "row"]
    titles = [r.get("title") for r in rows]
    for title in DASHBOARD_ROWS:
        check(title in titles, f"dashboard has no {title!r} row")
    check(
        sum(1 for r in rows if r.get("collapsed")) >= 3,
        "most rows should ship collapsed (Overview / Latency / Cache & KV / Speculative "
        "decode / Scrape health) — an all-open dashboard opens 40 queries on first paint",
    )

    # --- nothing that needs a plugin, a CDN or a click --------------------- #
    for p in panels:
        check(
            p.get("type") in PANEL_TYPES,
            f"panel {p.get('title')!r} uses type {p.get('type')!r}: not a built-in panel, "
            "and plugins are off in compose.yaml",
        )
    check(
        not re.search(r"https?://", raw),
        "dashboard JSON references an off-box URL (no CDN assets, nothing leaves the box)",
    )

    # --- the 15s scrape is the resolution floor ---------------------------- #
    check(dash.get("interval") == "15s", "dashboard min step should be 15s (the scrape interval)")
    for p in charts:
        for t in p.get("targets") or []:
            check(
                t.get("interval") in (None, "15s"),
                f"panel {p.get('title')!r} step {t.get('interval')!r} is finer than the 15s scrape",
            )

    # --- every panel states its own PromQL, and the statement is true ------ #
    for p in charts:
        description = str(p.get("description") or "")
        title = p.get("title", "<untitled>")
        check("PromQL" in description, f"panel {title!r} description does not state its PromQL")
        for t in p.get("targets") or []:
            expr = str(t.get("expr") or "")
            check(balanced(expr), f"panel {title!r} has an unbalanced expression: {expr}")
            check(
                expr in description,
                f"panel {title!r}: its description does not contain its own query "
                f"({t.get('refId')}), so the note is stale or was hand-copied",
            )

    # --- variables: declared, well-formed, and actually used --------------- #
    variables = {v.get("name"): v for v in (dash.get("templating") or {}).get("list") or []}
    for name, variable in variables.items():
        check(
            variable.get("type") in VARIABLE_TYPES,
            f"${name} has type {variable.get('type')!r}, which Grafana silently drops",
        )
        if variable.get("type") != "query":
            continue
        query = variable.get("query")
        query = query if isinstance(query, str) else (query or {}).get("query", "")
        check(bool(query), f"${name} is a query variable with no query")
        if query.startswith("label_values("):
            inner = query[len("label_values("):-1]
            check(
                inner.count(",") == 1,
                f"${name}: label_values(<selector>, <label>) with a comma inside the "
                f"selector does not parse in Grafana (its regex cannot contain one), so "
                f"the variable silently offers nothing: {query}",
            )
    expressions = " ".join(str(t.get("expr") or "") for p in charts for t in p.get("targets") or [])
    for name in ("job", "instance", "model"):
        check(f"${name}" in expressions, f"${name} is declared but no panel filters on it")

    # --- graceful degradation, not "No data" ------------------------------- #
    for p in charts:
        exprs = " ".join(str(t.get("expr") or "") for t in p.get("targets") or [])
        if "histogram_quantile" in exprs:
            check(
                bool(((p.get("fieldConfig") or {}).get("defaults") or {}).get("noValue")),
                f"panel {p.get('title')!r} renders quantiles but has no noValue message: a "
                "server with averages only must read 'no histogram on this server'",
            )
        # 0/0 -> NaN -> dropped sample -> a hole that reads as a missing metric.
        for guard, name in (("draft_tokens_accepted", "draft acceptance"),
                            ("prompt_tokens_cached", "prompt-cache hit ratio")):
            if guard in exprs and " / " in exprs:
                check(
                    "clamp_min" in exprs,
                    f"panel {p.get('title')!r}: the {name} division is not clamp_min-guarded",
                )

    # --- the specific questions this dashboard exists to answer ------------ #
    joined = json.dumps(dash)
    coverage = {
        "decode tokens/s via rate()": "tokens_predicted_total" in joined,
        "the server's own average gauge overlaid for the migration": "predicted_tokens_seconds" in joined,
        "prompt tokens/s via rate()": "prompt_tokens_total" in joined,
        "requests in flight": "requests_processing" in joined,
        "requests deferred": "requests_deferred" in joined,
        "structured request share": "structured_requests_total" in joined,
        "le-bucketed TTFT family": "time_to_first_token_seconds_bucket" in joined,
        "time per output token": "time_per_output_token_seconds_bucket" in joined,
        "end-to-end latency": "end_to_end_response_seconds_bucket" in joined,
        "sum by (le) before the quantile": "sum by (le)" in joined,
        "kv usage ratio": "kv_cache_usage_ratio" in joined,
        "kv tokens against the pool": "kv_pool_positions" in joined,
        "kv reserved tokens": "kv_pool_reserved_tokens" in joined,
        "up per job": 'up{job=~"$job"' in expressions,
        "monitor scrape success": "llm_monitor_scrape_success_total" in joined,
        "monitor scrape latency": "llm_monitor_scrape_latency_ms" in joined,
        "repeat-by-variable for multi-instance": '"repeat"' in joined,
        "histogram_quantile": "histogram_quantile" in joined,
        "$__rate_interval windows": "$__rate_interval" in joined,
    }
    for what, holds in coverage.items():
        check(holds, f"dashboard does not cover: {what}")

    # --- KV bands at 80 % / 90 % drawn, not just mentioned ----------------- #
    kv = next((p for p in charts if p.get("id") == 4), None)
    check(kv is not None, "panel id 4 (KV-cache pressure) is missing")
    if kv:
        defaults = (kv.get("fieldConfig") or {}).get("defaults") or {}
        steps = [step.get("value") for step in defaults.get("thresholds", {}).get("steps", [])]
        check(0.8 in steps and 0.9 in steps, f"panel 4 should band at 0.8 and 0.9, found {steps}")
        style = defaults.get("custom", {}).get("thresholdsStyle", {}).get("mode")
        check(style != "off", "panel 4's threshold bands are switched off")

    # --- a subquery needs a step, and it is easy to forget ----------------- #
    for match in re.finditer(r"avg_over_time\([^)]*\[([^\]]*)\]", expressions):
        check(
            ":" in match.group(1),
            f"avg_over_time over an aggregated expression needs a subquery step "
            f"([1h:5m], not [1h]): {match.group(0)}",
        )

    # --- every llm:* alias a panel plots must actually be recorded --------- #
    # (?<![\w:]) so vLLM's own vllm:* names are not read as llm:* aliases.
    aliases = set(re.findall(r"(?<![A-Za-z0-9_])llm:[A-Za-z0-9_:]+", expressions))
    have = recorded_aliases()
    check(bool(aliases), "no dashboard expression references an llm:* recording-rule alias")
    for alias in sorted(aliases):
        check(alias in have, f"dashboard plots {alias} but recording_rules.yml does not record it")

    # --- the live page's deep links land somewhere ------------------------- #
    # monitor/static/app.js builds viewPanel=<id> links; a link to a panel that no
    # longer exists opens Grafana on the wrong chart, and only when someone clicks.
    app_js = ROOT / "monitor" / "static" / "app.js"
    if app_js.is_file():
        text = app_js.read_text(encoding="utf-8")
        block = re.search(r"const GRAFANA_PANELS = \[(.*?)\];", text, re.S)
        check(block is not None, "could not find GRAFANA_PANELS in monitor/static/app.js")
        linked = {int(n) for n in re.findall(r"id:\s*(\d+)", block.group(1))} if block else set()
        present = {p.get("id") for p in flatten_panels(dash)}
        for panel_id in sorted(linked):
            check(
                panel_id in present,
                f"monitor/static/app.js deep-links viewPanel={panel_id}, which this "
                "dashboard does not have (ADR 0001 C4 ids are a contract)",
            )


def check_dashboard_generated() -> None:
    """The committed JSON is what the generator produces (no hand-edits, no drift).

    Runs make_dashboard.py --check rather than importing it: the generator owns its
    own output, and this file only has to notice when the two disagree.
    """
    generator = GRAFANA_DIR / "make_dashboard.py"
    if not generator.is_file():
        return
    result = subprocess.run(
        [sys.executable, str(generator), "--check"], capture_output=True, text=True, cwd=ROOT
    )
    check(
        result.returncode == 0,
        "dashboards/llm-server.json is not what make_dashboard.py generates: "
        + ((result.stdout or "") + (result.stderr or "")).strip(),
    )


def check_provisioning() -> None:
    for path in sorted(GRAFANA_DIR.rglob("*.yml")) + sorted(GRAFANA_DIR.rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        check(
            not re.search(r"https?://(?!prometheus:9090)", text),
            f"{path.relative_to(ROOT)} references an outside URL",
        )
    ds = (GRAFANA_DIR / "provisioning" / "datasources" / "datasource.yml").read_text()
    check("http://prometheus:9090" in ds, "datasource must point at http://prometheus:9090")
    check("uid: prometheus" in ds, "datasource needs a stable uid for the dashboard variable")
    provider = (GRAFANA_DIR / "provisioning" / "dashboards" / "dashprovider.yml").read_text()
    check("disableDeletion: true" in provider, "dashboard provider should refuse deletion")
    check("allowUiUpdates: false" in provider, "dashboard provider should not let the UI overwrite git")


def check_compose() -> None:
    text = (HERE / "compose.yaml").read_text(encoding="utf-8")
    data = parse_yaml(text)
    services = data.get("services") or {}
    for name in ("prometheus", "grafana"):
        check(name in services, f"compose.yaml has no {name} service")
    prom = services.get("prometheus") or {}
    graf = services.get("grafana") or {}

    for name, svc in (("prometheus", prom), ("grafana", graf)):
        image = str(svc.get("image", ""))
        check(bool(image), f"{name}: no image")
        check(
            "latest" not in image and (":" in image.split("@")[0] or "@sha256:" in image),
            f"{name}: image must be pinned by tag or digest, not floating ({image})",
        )
        check("@sha256:" in image, f"{name}: also pin {image} by digest")
        check(svc.get("restart") == "unless-stopped", f"{name}: restart: unless-stopped expected")
        check(svc.get("mem_limit"), f"{name}: no mem_limit")
        check(svc.get("cpus"), f"{name}: no cpus limit")

    commands = " ".join(str(item) for item in (prom.get("command") or []))
    check("--storage.tsdb.retention.size=" in commands, "prometheus needs a retention SIZE cap")
    check("--storage.tsdb.retention.time=" in commands, "prometheus needs a retention time cap")

    for name, svc in (("prometheus", prom), ("grafana", graf)):
        ports = [str(p) for p in (svc.get("ports") or [])]
        check(bool(ports), f"{name}: no port mapping")
        for port in ports:
            check(
                port.startswith("127.0.0.1:") or port.startswith('"127.0.0.1:'),
                f"{name}: port {port} is not loopback-bound (LAN exposure is its own ticket, workspace-szw.12)",
            )

    env = graf.get("environment") or {}
    expected = {
        "GF_PLUGINS_ALLOW_LOADING": "false",
        "GF_ANALYTICS_REPORTING_ENABLED": "false",
        "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
        "GF_NEWSFEED_ENABLED": "false",
        "GF_USERS_ALLOW_SIGN_UP": "false",
        "GF_AUTH_DISABLE_LOGIN_FORM": "false",
    }
    for key, value in expected.items():
        check(
            str(env.get(key, "")).strip('"') == value,
            f"grafana {key} must be {value} in the base stack",
        )
    check(
        "GRAFANA_ADMIN_PASSWORD" in json.dumps(env),
        "grafana admin password must come from .env, never be committed",
    )
    volumes = " ".join(str(v) for v in (graf.get("volumes") or []))
    check(":ro" in volumes, "grafana provisioning/dashboards must be mounted read-only")
    check("extra_hosts" in json.dumps(prom), "prometheus needs host-gateway for the proxy job")


def check_upstream_declaration() -> None:
    """`.env`'s LLM_UPSTREAM_URL must be the server Prometheus actually scrapes.

    Prometheus cannot expand ${VAR} in scrape targets, so the upstream is named
    in three places (the declaration here, the file_sd target list, and the
    passthrough job's llm_upstream label). A silent disagreement does not error:
    it just charts the wrong server. See monitoring/README.md, "Which knobs live
    where".
    """

    def env_value(path: Path, key: str) -> str:
        if not path.is_file():
            return ""
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("#") or not line:
                continue
            name, sep, value = line.partition("=")
            if sep and name.strip() == key:
                return value.strip().strip('"').strip("'")
        return ""

    declared = env_value(HERE / ".env", "LLM_UPSTREAM_URL") or env_value(
        HERE / ".env.example", "LLM_UPSTREAM_URL"
    )
    check(bool(declared), ".env.example must declare LLM_UPSTREAM_URL")
    if not declared:
        return
    want = declared.split("://")[-1].split("/")[0].strip().lower()

    entries = json.loads(
        (PROM_DIR / "targets" / "llm-servers.json").read_text(encoding="utf-8")
    )
    scraped = {
        target.split("://")[-1].split("/")[0].strip().lower()
        for entry in entries
        for target in entry.get("targets", [])
    }
    check(
        want in scraped,
        f"LLM_UPSTREAM_URL ({want}) is not in prometheus/targets/llm-servers.json "
        f"({', '.join(sorted(scraped)) or 'empty'})",
    )

    prom = (PROM_DIR / "prometheus.yml").read_text(encoding="utf-8")
    label = re.search(r"llm_upstream:\s*\"?([^\"\n]+)\"?", prom)
    check(bool(label), "llm-server-via-proxy needs an llm_upstream label (becomes ?url=)")
    if label:
        got = label.group(1).strip().split("://")[-1].split("/")[0].strip().lower()
        check(
            got == want,
            f"prometheus.yml llm_upstream ({got}) disagrees with LLM_UPSTREAM_URL ({want})",
        )


def check_footprint_documented() -> None:
    readme = (HERE / "README.md").read_text(encoding="utf-8")
    for token in ("mem_limit", "retention.size", "footprint", "PSS"):
        check(token in readme, f"monitoring/README.md does not cover {token}")


def maybe_promtool(enabled: bool) -> None:
    if not enabled:
        return
    promtool = shutil.which("promtool")
    if not promtool:
        print("note: promtool not on PATH; skipping promtool checks (see monitoring/check.sh)")
        return
    for args in (
        ["check", "config", str(PROM_DIR / "prometheus.yml")],
        ["check", "rules", str(PROM_DIR / "rules.yml"), str(PROM_DIR / "recording_rules.yml")],
        ["test", "rules", str(PROM_DIR / "unit-tests.yml")],
    ):
        result = subprocess.run([promtool, *args], capture_output=True, text=True, cwd=ROOT)
        check(
            result.returncode == 0,
            f"promtool {' '.join(args[:2])} failed:\n{(result.stdout or '') + (result.stderr or '')}",
        )
    check_dashboard_promql(promtool)


def dashboard_expressions() -> list[tuple[str, str]]:
    """Every PromQL string in the dashboard, as (panel-id + refId, expr) pairs."""
    path = DASH_DIR / "llm-server.json"
    if not path.exists():
        return []
    dash = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for panel in flatten_panels(dash):
        for tgt in panel.get("targets") or []:
            expr = (tgt.get("expr") or "").strip()
            if expr:
                out.append((f"p{panel.get('id')}{tgt.get('refId', 'A')}", expr))
    return out


def check_dashboard_promql(promtool: str) -> None:
    """Type-check every dashboard expression by borrowing promtool's parser.

    promtool has no "check this one query" mode, but `check rules` parses any
    expression handed to it as a recording-rule `expr`. So the panel queries are
    written to a throwaway rules file — Grafana's `$__rate_interval` replaced by a
    real duration, because promtool does not know Grafana's template variables — and
    parsed by the same Prometheus parser that will run the query.

    This catches the class of dashboard bug no JSON schema can: a `rate(x)[5m]` typo,
    an unbalanced paren, a `sum by (le)` that dropped `le`, an `avg_over_time(max(...))`
    that would need a subquery step. Label *values* like $job stay as written: to
    PromQL they are ordinary regex strings, which is how Grafana sends them.
    """
    exprs = dashboard_expressions()
    check(bool(exprs), "dashboard has no panel expressions to validate")
    lines = ["groups:", "  - name: grafana_panel_expressions", "    rules:"]
    for tag, expr in exprs:
        name = "check:" + re.sub(r"[^a-zA-Z0-9_]", "_", tag).lower()
        lines.append(f"      - record: {name}")
        # JSON-quoted on one line: YAML block scalars would fold a long expression and
        # PromQL would then read a mangled query (a folded `clamp_min` is `clamp mp_min`).
        lines.append(f"        expr: {json.dumps(expr.replace("$__rate_interval", "5m"))}")
    tmp = Path(tempfile.mkdtemp(prefix="grafana-promql-")) / "panel-expressions.yml"
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = subprocess.run([promtool, "check", "rules", str(tmp)],
                            capture_output=True, text=True, cwd=ROOT)
    check(
        result.returncode == 0,
        "a dashboard panel expression is not valid PromQL "
        f"(rendered to {tmp}):\n{(result.stdout or '') + (result.stderr or '')}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--promtool", action="store_true", help="also run promtool when it is installed")
    args = parser.parse_args(argv)

    check_render()
    alerts = check_rules()
    check_unit_tests(alerts)
    check_prometheus_config()
    check_dashboard()
    check_dashboard_contract()
    check_dashboard_generated()
    check_provisioning()
    check_compose()
    check_upstream_declaration()
    check_footprint_documented()
    maybe_promtool(args.promtool)

    if FAILS:
        print(f"FAILED: {len(FAILS)} of {CHECKS} monitoring checks did not hold\n")
        for failure in FAILS:
            print(f"  - {failure}")
        return 1
    print(f"monitoring config OK ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
