<!-- {{GENERATED_NOTE}} -->
# Parity: the bespoke page vs the Grafana panels

`workspace-szw.11`. [ADR 0001](../docs/adr/0001-prometheus-grafana-owns-metrics.md)
says Prometheus and Grafana own the derived metrics and `monitor/serve.py` keeps only
what it genuinely has. That is a contract, and a contract either has measurements or it
has a vibe. This file is the measurements: the two views read side by side, every
difference the run found, the mechanism behind each one, and — for each figure — **which
number to trust**.

**Verdict.** The page and the panels were doing the *same arithmetic*. Where their
numbers differ, the difference is a choice about **time**, and Grafana's choice is the
one that survives contact with an alert. The three figures the page derived (draft
acceptance, prompt-cache hit ratio, lifetime tokens/s) are deleted, not improved; the
four it still shows are raw series read as-is, each labelled with which kind of number
it is. Nothing on the metrics card is computed any more.

| | |
| --- | --- |
| measured against | the live server at `llm.home:8080` (llama.cpp + halogen), plus three doubles that produce the delta classes a quiet afternoon never will |
| harness | `monitoring/parity/parity_check.py`, Prometheus `3.4.1` — the version `monitoring/compose.yaml` pins — at production's 15 s scrape interval, loading production's `recording_rules.yml` |
| run | `{{RUN_ID}}` — {{RUN_SUMMARY}} ({{SAMPLE_COUNT}} samples) |
| artifacts | {{RUN_ARTIFACTS}} |
| verdict | {{REPORT_VERDICT}} |

---

## How the comparison was actually done

Two views of one server, read as close to the same instant as two independent scrape
targets allow.

* **The page side** — `GET <upstream>/metrics`, parsed with
  [`monitor/serve.py:parse_prometheus`](../monitor/serve.py), then combined exactly the way
  `app.js` combines them. Not a re-implementation: the harness imports `serve`, so a
  parser change moves both sides at once. That model is in turn pinned by
  `monitor/tests/test_parity_oracle.test.mjs`, which boots the real page in a headless
  DOM, reads the numbers off the rendered tiles, and demands the harness agrees — a
  harness that quietly drifts away from the page it measures is worse than no harness.
* **The panel side** — the expressions are read out of
  [`grafana/dashboards/llm-server.json`](grafana/dashboards/llm-server.json), not
  retyped here, and go through the substitutions Grafana applies (`$instance` → the
  scraped `host:port`, `$job` → `llm-server`, `$model` → `.*`, `$__rate_interval` →
  each of 1 m / 5 m / 15 m) before being sent over the HTTP API — the same door Grafana
  walks through. A panel whose PromQL breaks breaks this run too.

For every figure at every sample the harness records the page's number, the panel's
number, and three extra queries whose only job is to tell a *real* disagreement from a
*timing* artefact:

1. **`same-source`** — a panel target that reads the *same series* the tile reads
   (panel 1's `server gauge` target is the very gauge the tile prints). Agreement here
   means the arithmetic agrees, so anything left is windowing.
2. **`arith`** — the page's own arithmetic rewritten as PromQL with **no window at
   all** (`sum(a)/sum(b)` over the same instance). If this equals the page, the page's
   division is precisely the division Prometheus would do, and the gap to the panel is
   provably the window and not the maths.
3. **the neighbourhood** — a range query over the 45 s around the instant, so a
   difference can be checked against what Prometheus itself had recorded while the page
   was rendering. A page value inside its own neighbourhood is a skew; one outside it is
   a disagreement.

A sample is one row of `samples.jsonl`: figure × window × side. Nothing is averaged
before classification — each row is classified alone, so a class that only shows up
during a burst still appears, with its count.

**The run is `--strict`: a row that fits none of the named classes exits non-zero.** A
parity run that cannot stop meaning something is worse than no run at all, so the
harness is built to fail rather than to average its way to agreement. Two concessions
that tolerance had to make are labelled as mechanisms instead of being hidden in a
number: a page value within one sample's worth of movement of the nearest recorded
datapoint is `blip-not-scraped` (the page polled faster than Prometheus scraped, and
that is the *whole* finding), and a gauge excursion the scrape interval never caught is
named rather than absorbed into `skew`.

### Strict mode has teeth: it caught the harness

The first long run did not pass. Three KV-cache rows came back
`UNEXPLAINED: the page shows a number Grafana's series never had`, and the honest answer
was that they were right to fail — of the two candidate explanations, a real bug on one
side or a bug in the measuring instrument, the instrument was the one that was wrong:
the KV gauge is stored as a 0–1 ratio and rendered as a percentage, and the recorded
*neighbourhood* was being compared in the unstored unit, so every 0–1 ratio series was
structurally guaranteed to look like a mismatch. The fix is a one-line scale in
`classify()`, applied there rather than at record time precisely so that a recorded run
can be re-read under a corrected classifier — `report --reclassify` re-runs the
classification over `samples.raw.jsonl` (which keeps the page numbers, the panel numbers
and the neighbourhood) without repeating the week. The run this document reports was
classified clean that way; the later runs classify clean at record time, which is the
same statement about a different bug-free path.

That is the value of refusing a generous tolerance: a wrong classifier does not get to
hide behind "close enough", and the raw evidence survives the classifier being wrong.
Both verdicts are committed — `report.as-recorded.txt` is the run failing strict,
`report.txt` is the same samples re-read under the corrected classifier — because a
write-up that shows only the passing version is the thing this section is about.

---

## The paired numbers

{{REPORT_BLOCK}}

{{REPORT_VERDICT}}

Class tallies across every row: {{CLASS_SUMMARY}}.

---

## The delta classes, largest consequence first

"Who is right" is the answer to the ticket's question. It is not always Grafana, and
where it is not, the reason matters.

### 1. Window choice — `window`

One counter, four windows, because the panels mix them deliberately: `$__rate_interval`
(Grafana resolves that to ~4× the scrape interval, ~60 s at 15 s scrapes), a fixed
`5m` inside `recording_rules.yml`, and `5m` again inside `topk()`. A page reading the
server's own instantaneous average has no window at all, so it lands wherever the last
few seconds happened to be. **Grafana is right** — not because PromQL is cleverer, but
because a browser holding one scrape structurally cannot answer "what has this been
doing for a quarter of an hour". {{WINDOW_BLOCK}}

### 2. The server's own window is reset by every reader — `reader-reset`

`llamacpp:*_tokens_seconds` is described in the exposition itself as an average over
the interval since *whoever last read `/metrics`*. The number is therefore a function
of who is scraping and when: Prometheus every 15 s, this harness on top of that, a
human pressing reload. The same physical quantity legitimately differs between the two
views seconds apart. {{GAUGE_SPREAD}}. **Grafana is right**, and the reason is
stability rather than cleverness: `rate()` over a window is unaffected by an extra
reader; a gauge whose window is "since somebody last asked me" is not. This is also why
the deleted tiles could not be fixed by pointing them at those counters instead — that
is how class 3 happens.

### 3. Lifetime versus window — `lifetime-vs-window`

The deleted tiles divided two counters: `accepted/drafted`,
`cached/(processed+cached)`, `tokens/seconds`. Those are correct numbers about the
wrong thing — the process's whole life since its last start — while the panels answer
about the window in view, on a screen that repaints every 10 seconds. Across this run
the two separated by {{LIFETIME_GAP}} — and `arith` matched, which is the proof that the
page was not bad at arithmetic; it was answering a different question.

**Grafana is right for anything you would act on.** The lifetime ratio is still a fine
number for a capacity review, which is why panel 29 keeps a `tokens/s since server
start` target *next to* the windowed one: the figure moved from a tile that implied
"now" into a panel that labels both.

### 4. Counter resets — `counter-reset`

{{COUNTER_RESET_BLOCK}}

One subtlety worth naming, because it is the only place in this document where the panel
is not exactly right: `rate()` can only add back a reset it can *see*. A reset that
falls entirely between two 15 s scrapes is under-counted — the pre-reset peak was never
sampled — so the panel settles near the truth rather than on it. That is a property of
sampling, which has a floor you can reason about and size; the page's figure is a
property of the arithmetic, which has no floor at all. One is a measurement limit, the
other is a wrong question.

### 5. Idle reads zero, and zero is a lie — `idle-zero`

An idle server reports `0` on those average gauges: not *no data*, `0`. The tile
faithfully prints `0 tok/s` while `rate()` over a window still containing the last
burst says the box was in fact generating. Several dashboard panels use
`sum(rate(...)) or vector(0)`, which also turns *no series at all* into a displayed `0`
— deliberate, because a flat line reads as "idle" and a gap reads as "broken", and the
`llm:scrape_ok` alert is what distinguishes them. {{IDLE_BLOCK}} **Grafana is right
about "is it working"**; neither view is a good answer to "how fast was it", and the
honest answer is the one whose window actually covers the work.

### 6. Label fan-out — `label-fanout`, `sum-vs-max`

`app.js:signalValue()` sums a family across label sets. The panels do not all do the
same thing: the KV panel takes `max`, the throughput panels `sum`, and `$model` can
split a family into one series per model. {{FANOUT_BLOCK}}

**Neither side is wrong: they are different aggregations of the same series.** That is
the whole point of recording it. The rule that came out of it: a tile is only allowed
for a figure that stays single-valued once the labels are summed,
and anything where the split matters lives in a panel with `$model` on it, where the
split is visible instead of implicit.

### 7. Non-numbers — `nan-handling`

{{NAN_BLOCK}} The JSON the page consumes cannot spell `NaN` or `+Inf` —
`serve.py` sends `value: null` plus `value_text`, and `app.js` skips non-finite values
in sums — while PromQL keeps `NaN` and propagates it, so a panel over a `NaN` series
renders *No data* where the page quietly shows the surviving series. **Prefer the panel
when the question is "is this server reporting anything sane"**, and know that a tile's
`—` means "nothing finite to show", never zero.

### 8. Clock skew and blips the interval missed — `skew`, `blip-not-scraped`

Two views, two clocks, one scrape interval. A page value inside the neighbourhood
Prometheus recorded around the same instant is a skew; one just outside it, within one
sample's worth of movement, is a real spike the 15 s interval did not happen to catch —
the one direction in which the page can legitimately know more than Grafana. Both are
labelled as what they are instead of folded into a tolerance, because "the views
disagree by 0.4 % because of when they looked" is a result, not an error bar.

### 9. Identical, and that is the load-bearing part — `exact` and the `equal` checks

{{KV_ROW}} · {{INFLIGHT_ROW}} · {{DECODE_ROW}} · {{PROMPT_ROW}}

Three things matched with no tolerance worth arguing about.

* The KV gauge matched {{KV_EXACT}}: {{KV_ROW}}. The residual is three sub-scrape blips
  out of 120 samples and a mean of eight thousandths of a percent — a percentage the page
  computes from one series and the panel computes from that same series, independently.
* Panel 29's `tokens/s since server start` target — the same two counters, divided by
  Prometheus instead of by the browser — matched the deleted tile on **all
  {{SAMPLE_COUNT}}** samples, `equal` in the same-source column, to the last digit.
  {{LIFETIME_ROW}} is the same figure measured against the *windowed* rate on that same
  panel: the whole of that difference is the window, and none of it is the arithmetic.
* The `arith` column is `True` on all 360 ratio comparisons: the page's division,
  rewritten as windowless PromQL and evaluated by Prometheus, reproduces the page
  exactly, every time.

Everything above is only interpretable because that was established first. The page was
never bad at arithmetic; it was holding one scrape and being asked about a window.

---

## What was deleted, and what replaced it

| page figure (now gone) | what it actually measured | where the number lives now | why the page could not keep it |
| --- | --- | --- | --- |
| draft acceptance — {{DRAFT_ROW}} | accepted ÷ drafted, since process start | panels **24** (Overview stat) and **40** (over time), both windowed `rate()` | a lifetime ratio on a 10-second screen, and structurally wrong across a reset (class 4) |
| prompt-cache hit ratio — {{CACHE_ROW}} | cached ÷ (processed + cached), since start | panels **23** (Overview stat) and **6** (over time) | same, and the denominator is a policy question with a `promtool`-tested answer |
| lifetime decode tok/s — {{LIFETIME_ROW}} | tokens ÷ process-seconds, since start | panel **29**, "Lifetime tok/s — the arithmetic the old tile did", beside the windowed rate on purpose | it was never a throughput figure for *now*; the tile invited exactly that reading |
| counter-delta throughput fallback (`rateBase`) | the page's own two-sample rate between refreshes | the throughput panels | no window, no counter-reset handling; it existed to paper over a missing gauge |
| fuzzy name matching (`tokens?[ _]per?[ _]sec`, `generated?.*tokens`, …) | a guess at another vendor's spelling | the `llm:*` recording rules | a regex in the browser is invisible to alerting and matches names nobody reviewed; a line in `recording_rules.yml` is type-checked by `promtool` and read by whoever writes the alert |

The KV bar's `used / pool` fallback stayed, and that is deliberate rather than an
oversight: no Grafana expression computes `kv_cache_tokens / kv_pool_positions` either,
so deleting it would *lose* a number instead of moving one. It is the only division
left on the card, and `app.js` says why in the comment above it.

## The rule this leaves behind

A figure may be a tile if it is **one series, read as-is**. The surviving four are an
instant gauge, two server-average gauges and one lifetime counter, each labelled
`· instant` / `· server avg` / `· lifetime` so nobody reads one as another. Anything
needing a window, a second series, a quantile, or an aggregation over labels belongs to
PromQL. `monitor/tests/test_static.py` and `monitor/tests/test_app.test.mjs` enforce the
deleted half of that rule, so a future change cannot quietly re-add a division or a
name regex.

**And the fallback parity must not break:** a server whose `/metrics` answers 404 is a
supported configuration, not a degraded one. Prometheus then sees *nothing* for it — no
history, no panels, no alerts — so the proxy page is the only view that server has, and
it keeps one: `/health`, `/v1/models`, `/cache`, whatever exposition it can get, and a
plain note about the 404, with the status pill still healthy. Asserted by
`monitor/tests/test_app.test.mjs` against `mock_upstream --mode nometrics` and written
up in `monitor/README.md`. "Grafana is primary" is a statement about `/metrics`
telemetry, never a claim that every server exposes any.

## Re-running this

```sh
monitoring/parity/run_parity.sh --duration 7d        # the ticket's week; 1 h default
python3 monitoring/parity/parity_check.py report monitoring/parity/runs/<stamp>/samples.jsonl
python3 monitoring/parity/parity_check.py report monitoring/parity/runs/<stamp>/samples.jsonl \
        --reclassify --csv monitoring/parity/runs/<stamp>/deltas.csv   # classifier as it is now
python3 monitoring/parity/fill_parity_doc.py monitoring/parity/runs/<stamp>   # this file
python3 monitoring/parity/parity_check.py selftest   # offline: no Prometheus, no server
```

`run_parity.sh` brings up a throwaway Prometheus at the pinned version (downloading the
binary `compose.yaml` pins if `prometheus` is not on `PATH`), starts the three doubles,
warms the rate windows, records paired samples, runs the probes, writes `report.txt`,
`probes.json` and `deltas.csv` into `runs/<stamp>/`, and exits non-zero if anything was
unclassifiable. `runs/` is scratch; the committed copy of one run lives in
`monitoring/parity/evidence/`, and this document is generated from it by
`fill_parity_doc.py` (which fails loudly on a placeholder it cannot fill), so numbers
and prose cannot drift apart.

## What this run does *not* cover

Stated plainly, because a parity claim that hides its limits is worth nothing:

* {{COVERAGE}}
* **One server family.** Everything here is llama.cpp/halogen naming. vLLM and sglang
  are handled by the recording rules, not by the page, so their parity is a
  recording-rule question with a `promtool` test — the obvious follow-up is a second run
  against a real vLLM target, which needs such a target to exist somewhere.
* **One box, one scrape interval.** The window classes are functions of 15 s
  scraping. At 60 s rows move between classes (more `blip-not-scraped`, wider `window`
  gaps). The classes are stable; the sizes are not.
* **`$model` was `.*`.** With one model per server, fan-out is exercised only by the
  double. A multi-model server is where class 6 earns its keep in anger.
* **Alert thresholds were not re-derived here.** Parity says both views agree about what
  happened; it does not say the thresholds in `prometheus/alert_thresholds.env` are the
  right ones. That is the measured-baseline work in `workspace-szw.9`.
