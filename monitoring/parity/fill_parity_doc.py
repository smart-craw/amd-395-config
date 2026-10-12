#!/usr/bin/env python3
"""Fill monitoring/PARITY.md's placeholders from a parity run's artifacts.

The write-up is prose by hand and numbers by generation: every figure the document
quotes is injected from a run's `report.txt` / `probes.json` / `samples.meta.json`, so
the prose cannot drift from the measurement it claims to describe. Run it after a run:

    python3 monitoring/parity/fill_parity_doc.py monitoring/parity/runs/<stamp>

The template is `PARITY.template.md` (next to this file) and the document is
`monitoring/PARITY.md`: prose by hand in the template, numbers injected here, so the
generated file can be re-generated forever and never drift. Placeholders are
`{{TOKEN}}`; one left unfilled is a hard failure with a non-zero exit — a document
quoting numbers it did not read from the run would be worse than no document.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "PARITY.template.md"
DOC = HERE.parent / "PARITY.md"

# 8 figures x 3 windows: how many jsonl rows one sample contributes.
ROWS_PER_SAMPLE = 24


# --------------------------------------------------------------------------- #
# reading report.txt                                                           #
# --------------------------------------------------------------------------- #

def report_lines(path: Path) -> list[str]:
    return path.read_text().splitlines()


def report_block(lines: list[str]) -> list[str]:
    """The per-figure table: header row through the blank line after it."""
    start = next(i for i, line in enumerate(lines) if line.startswith("figure "))
    end = next((i for i in range(start + 1, len(lines)) if not lines[i].strip()),
               len(lines))
    return [line.rstrip() for line in lines[start:end]]


def markdown_table(lines: list[str]) -> str:
    """The generated table as markdown, not as an aligned text dump.

    The report's own output is for a terminal; this document is for a diff and a
    browser, and a 200-character-wide column is neither.
    """
    rows = rows_of(lines)
    if not rows:
        return "| (the run produced no rows) |"
    out = [
        "| figure | win | n | page | panel | Δ mean | Δ worst | classes | same-source | arith |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    order = {"1m": 0, "5m": 1, "15m": 2, "1h": 3}
    for row in sorted(rows, key=lambda r: (r["figure"], order.get(r["window"], 9))):
        out.append(
            "| {figure} | {window} | {n} | {page} | {panel} | {delta_mean} | "
            "{delta_worst}% | {classes} | {same} | {arith} |".format(**row)
        )
    return "\n".join(out)


def verdict_line(lines: list[str]) -> str:
    for line in lines:
        if line.startswith("No unexplained deltas") or line.startswith("UNEXPLAINED"):
            return line.lstrip("# ").strip()
    return "the report printed no verdict, which is itself a failure"


def parse_row(line: str) -> dict:
    """One table row.

    Columns are separated by two or more spaces and padded, and no cell is ever empty
    (the report prints an em-dash), so a wide split is exact even though several cells
    contain single spaces of their own.
    """
    cells = [cell.strip() for cell in re.split(r"\s{2,}", line.strip()) if cell.strip()]
    if len(cells) < 9:
        return {}
    figure, window, count, classes, page, panel, delta, same, arith = cells[:9]
    if not count.isdigit():          # the header row
        return {}
    mean_delta, _, worst = delta.partition(" / ")
    return {
        "figure": figure,
        "window": window,
        "n": int(count),
        "classes": classes,
        "page": page,
        "panel": panel,
        "delta_mean": mean_delta,
        "delta_worst": worst.replace("%", "").strip(),
        "same": same,
        "arith": arith,
    }


def rows_of(lines: list[str]) -> list[dict]:
    out = []
    for line in lines:
        row = parse_row(line)
        if row and row["figure"] != "figure":
            out.append(row)
    return out


def class_counts(rows: list[dict]) -> Counter:
    counts: Counter = Counter()
    for row in rows:
        if row["classes"] == "class tally":
            continue
        for name, hits in re.findall(r"([a-z][a-z0-9-]+)\u00d7(\d+)", row["classes"]):
            counts[name] += int(hits)
    return counts


def pick(rows: list[dict], title: str, window: str = "5m") -> dict:
    for row in rows:
        if row["figure"] == title and row["window"] == window:
            return row
    return {}


def worst_percent(rows: list[dict], titles) -> str:
    """The largest worst-case delta among the figures named, with its label."""
    best = None
    for row in rows:
        if row["figure"] not in titles:
            continue
        try:
            value = abs(float(row["delta_worst"]))
        except (TypeError, ValueError):
            continue
        if best is None or value > best[0]:
            best = (value, row)
    if best is None:
        return "n/a"
    _, row = best
    return (f"{row['delta_mean']} mean, {row['delta_worst']}% worst "
            f"({row['figure'].replace(' [deleted]', '')}, {row['window']})")


def figure_row_text(row: dict) -> str:
    if not row:
        return "n/a"
    return (f"**{row['figure']}** ({row['window']}): page `{row['page']}` vs panel "
            f"`{row['panel']}`, {row['delta_mean']} mean — {row['classes']}")


# --------------------------------------------------------------------------- #
# prose fragments built from the probes                                        #
# --------------------------------------------------------------------------- #

def counter_reset_text(reset: dict, observed: dict) -> str:
    if not observed:
        return ("Probe `P3 counter-reset` did not observe it in this pass: "
                f"{reset.get('note', 'no observation')}. The double is started by "
                "`run_parity.sh`; point `--fixture-reset` at it and re-run `probe`.")
    return (
        "Measured, not argued. The double `monitor/tests/mock_upstream.py --mode reset` "
        "publishes `draft_tokens_accepted_total` restarting every 45 s under a "
        "`draft_tokens_total` that keeps climbing: the truth is a constant "
        f"**{fmt(reset.get('truth_pct'), 0)} %** by construction and does not move. "
        f"From {fmt(observed.get('fixture_age_s'), 0)} s into the fixture's own life, "
        "on one Prometheus at one instant, the page's lifetime division read "
        f"**{pct(observed.get('page_lifetime_pct'))}** while the dashboard's own "
        f"expression read **{pct(observed.get('panel_windowed_pct'))}**. The prompt "
        "cache pair behaved the same way: "
        f"{pct(observed.get('page_cache_hit_pct'))} against "
        f"{pct(observed.get('panel_cache_hit_pct'))}, truth "
        f"{fmt(observed.get('truth_cache_hit_pct'), 0)} %. The page's figure keeps "
        "decaying towards zero and never recovers — a division of two lifetimes has no "
        "window to forget into — while `rate()` counts the dropped tokens back and "
        "lands within reach of the truth instead (the caveat below is about the reach, "
        "not the direction). **Grafana is right, and the page is not approximately "
        "wrong but structurally wrong**, which is exactly the class of bug a browser "
        "must never own."
    )


def fanout_text(fanout: dict) -> str:
    series = fanout.get("families_with_several_label_sets") or {}
    if not series:
        return "Probe `P4 label-fanout` did not observe it in this pass."
    names = "; ".join(f"`{name}` across {len(hits)} label sets"
                      for name, hits in sorted(series.items())[:4])
    out = [f"Observed on the `--mode fanout` double: {names}".rstrip(".")]
    rows = {row.get("figure"): row for row in fanout.get("classes") or []}
    kv = rows.get("kv_usage_pct")
    if kv and kv.get("page") is not None and kv.get("panel") is not None:
        out.append(
            f"the KV family came out as **{fmt(kv['page'])} % on the page** (which sums "
            f"label sets) against **{fmt(kv['panel'])} % in the panel** (which takes "
            "`max` over them) — the same series, two defensible aggregations, no bug on "
            "either side, and exactly the kind of difference a tolerance would have "
            "hidden"
        )
    clamped = [row for row in rows.values() if row.get("class") == "clamp"]
    if clamped:
        sample = clamped[0]
        out.append(
            f"a family that fans out *and* sits still is the mirror image of class 5: "
            f"`rate()` over a window on counters nobody is moving is 0, the panel's "
            f"`clamp_min()` turns that 0/0 into a displayed 0 %, and the page's "
            f"lifetime division still reports {fmt(sample.get('page'))} % — the "
            f"`clamp` class, on `{sample.get('figure')}`".rstrip(".")
        )
    return ". ".join(out) + "."


def nan_text(nan: dict) -> str:
    hits = nan.get("non_finite_series") or []
    if not hits:
        return "Probe `P5 nan-handling` did not observe it in this pass."
    names = "; ".join(f"`{h['name']}` as `{h.get('value_text')}`" for h in hits[:4])
    return f"Observed on the `--mode nan` double: {names}."


def window_text(window: dict) -> str:
    panels = window.get("panel") or {}
    if not panels:
        return "Probe `P1 window-choice` did not observe it in this pass."
    order = {"1m": 0, "5m": 1, "15m": 2, "1h": 3}
    parts = ", ".join(f"{w} `{fmt(v, 1)}`"
                      for w, v in sorted(panels.items(),
                                         key=lambda kv: order.get(kv[0], 9)))
    return (f"Observed on the live server — one counter, four windows: {parts} tok/s, "
            f"against the tile's `{fmt(window.get('page_gauge'), 1)}` tok/s.")


def idle_text(idle: dict) -> str:
    if "page_gauge" not in idle:
        return "Probe `P6 idle-zero` did not observe it in this pass."
    panel = idle.get("panel")
    value = panel.get("5m") if isinstance(panel, dict) else panel
    return (f"Observed on the quiet box: the tile read `{fmt(idle.get('page_gauge'), 1)}` "
            f"tok/s while `rate()` over 5 m still reported `{fmt(value, 1)}` tok/s.")


def gauge_spread_text(probe: dict) -> str:
    factor = probe.get("spread_factor")
    if factor:
        return (f"the same gauge ranged `{fmt(factor, 1)}\u00d7` across "
                f"{len(probe.get('gauge_samples') or [])} one-second reads of one "
                "continuous decode")
    return "The probe needed a live decode and did not get one in this pass"


def coverage_text(meta: dict, samples: int) -> str:
    hours = (meta.get("planned_duration_s") or 0) / 3600.0
    return (
        f"**Compressed time, not seven days.** The ticket asked for a week; this is "
        f"{hours:.2f} h of paired sampling — {samples} samples, "
        f"{ROWS_PER_SAMPLE} rows each. What a week buys is the slow stuff: a counter "
        "reset, a weekend of idle, a model swap. The reset case is exactly the one a "
        "week might never show, so it is produced deterministically by a double instead "
        "of waited for. Everything else in the table is a mechanism rather than an "
        "incident, and a mechanism does not need a week to be visible — the sample count "
        "only bounds how much each row's tally is worth."
    )


def fmt(value, places: int = 1) -> str:
    return "n/a" if value is None else f"{float(value):.{places}f}"


def pct(value) -> str:
    return "n/a" if value is None else f"{float(value):.1f} %"


# --------------------------------------------------------------------------- #

def substitutions(run_dir: Path) -> dict:
    lines = report_lines(run_dir / "report.txt")
    rows = rows_of(lines)
    probes = json.loads((run_dir / "probes.json").read_text())
    meta = json.loads((run_dir / "samples.meta.json").read_text())
    sample_rows = sum(1 for line in open(run_dir / "samples.jsonl") if line.strip())
    samples = sample_rows // ROWS_PER_SAMPLE
    reset = probes.get("counter_reset", {})
    observed = reset.get("observed") or {}

    draft = pick(rows, "draft acceptance [deleted]")
    cache = pick(rows, "prompt-cache hit ratio [deleted]")
    lifetime = pick(rows, "lifetime decode tok/s [deleted]")
    decode = pick(rows, "decode tok/s")
    prompt = pick(rows, "prompt tok/s")
    kv = pick(rows, "KV cache usage")
    inflight = pick(rows, "requests in flight")

    minutes = max(1, round((meta.get("planned_duration_s") or 0) / 60))
    return {
        "RUN_ID": run_dir.name,
        "RUN_SUMMARY": (f"{minutes} min of paired samples, one every "
                        f"{meta.get('interval_s', 15):.0f} s, against the live server "
                        f"`{meta.get('upstream', '')}` with light completions alongside"),
        "GENERATED_NOTE": (
            f"Generated by `monitoring/parity/fill_parity_doc.py` from the run "
            f"`{run_dir.name}` — every number in this file was injected from that "
            f"run's artifacts. Edit `monitoring/parity/PARITY.template.md`, never "
            f"this file; a placeholder the generator cannot fill is a build failure, "
            f"not a TODO."),
        "RUN_ARTIFACTS": (
            f"[`monitoring/parity/evidence/{run_dir.name}/`](parity/"
            f"evidence/{run_dir.name}/) — `report.txt` (this table), `probes.json` "
            "(the seven probes), `deltas.csv.gz` (every single delta, one row per "
            "figure × window × sample — the ticket asked for every delta to be "
            "recorded, so they are all here), `samples.meta.json` (the exact panel "
            "expressions the run compared against) and `report.as-recorded.txt` (the "
            "report as the run itself printed it, before the classifier fix below). "
            "Generated from them by `monitoring/parity/fill_parity_doc.py`."
        ),
        "REPORT_BLOCK": markdown_table(lines),
        "REPORT_VERDICT": verdict_line(lines),
        "CLASS_SUMMARY": ", ".join(f"{name} ×{n}"
                                   for name, n in class_counts(rows).most_common()),
        "SAMPLE_COUNT": str(samples),
        "LIFETIME_GAP": worst_percent(rows, {"draft acceptance [deleted]",
                                             "prompt-cache hit ratio [deleted]",
                                             "lifetime decode tok/s [deleted]"}),
        "DRAFT_ROW": figure_row_text(draft),
        "CACHE_ROW": figure_row_text(cache),
        "LIFETIME_ROW": figure_row_text(lifetime),
        "DECODE_ROW": figure_row_text(decode),
        "PROMPT_ROW": figure_row_text(prompt),
        "KV_ROW": figure_row_text(kv),
        "INFLIGHT_ROW": figure_row_text(inflight),
        "KV_EXACT": "exactly" if kv.get("same", "").startswith("equal") else "about",
        "COUNTER_RESET_BLOCK": counter_reset_text(reset, observed),
        "FANOUT_BLOCK": fanout_text(probes.get("label_fanout", {})),
        "NAN_BLOCK": nan_text(probes.get("nan_handling", {})),
        "WINDOW_BLOCK": window_text(probes.get("window_choice", {})),
        "IDLE_BLOCK": idle_text(probes.get("idle_zero", {})),
        "GAUGE_SPREAD": gauge_spread_text(probes.get("gauge_window_reset", {})),
        "COVERAGE": coverage_text(meta, samples),
    }


def main(run_dir: Path) -> int:
    subs = substitutions(run_dir)
    text = TEMPLATE.read_text()
    for token, value in subs.items():
        text = text.replace("{{%s}}" % token, str(value))
    DOC.write_text(text)
    left = sorted(set(re.findall(r"\{\{([A-Z_]+)\}\}", text)))
    print(f"[fill] {DOC} refreshed from {run_dir.name}")
    if left:
        print(f"[fill] UNFILLED placeholders: {left}")
    return 1 if left else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    raise SystemExit(main(Path(sys.argv[1])))
