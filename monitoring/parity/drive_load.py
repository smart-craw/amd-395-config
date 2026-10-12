#!/usr/bin/env python3
"""Light, honest workload for a parity run: make the counters move.

An idle box is a bad parity subject. `llamacpp:*_tokens_seconds` are
"average since my last scrape" gauges that read 0 when nothing is running, and
`rate()` over a window reads ~0 too, so a quiet hour produces a run full of 0-vs-0
rows that proves nothing. This driver puts just enough traffic on the server that
counters advance and both views have a non-zero number to disagree about — and no
more: the default is one short completion every 12 s, which measured ~2% of the
box's decode time during the run recorded in PARITY.md.

The prompt is a long fixed prefix with a varying tail: that is what makes the
prompt-cache counters move the way they do in real traffic (a shared system
prompt, different user turns) and therefore what makes the prompt-cache hit ratio
a figure worth comparing rather than a constant.

    python3 monitoring/parity/drive_load.py --url http://llm.home:8080 --interval 12

Not part of the tests. Nothing in this repo needs load generation, only the parity
run does, and it is opt-in for exactly that reason.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

#: ~700 tokens of stable prefix: the parity run wants cache hits, not a cache miss.
PREFIX = (
    "You are a technical documentation assistant for a local LLM serving stack. "
    "The following reference notes describe the box: a Ryzen AI Max 395 (Strix Halo) "
    "with shared GPU memory, llama.cpp-style metrics exposed at /metrics in the "
    "Prometheus text exposition format, a same-origin monitor proxy in monitor/, and "
    "a Prometheus plus Grafana stack in monitoring/ that is the system of record for "
    "every derived number. Metrics named here include prompt tokens, generated "
    "tokens, KV cache usage, prompt cache hits, drafted and accepted speculative "
    "tokens, and request queue depth. "
) * 4

TAILS = [
    "Summarise the KV cache in one sentence.",
    "What does a counter reset do to a rate()?",
    "Name the two layers of this monitoring stack.",
    "Why is a lifetime ratio not a rate?",
    "Which view should an operator trust, and why?",
    "What is the fallback when /metrics answers 404?",
]


def completions(url: str, model: str, prompt: str, max_tokens: int, timeout: float) -> dict:
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/completions", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def resolve_model(url: str, timeout: float = 8.0) -> str:
    with urllib.request.urlopen(url.rstrip("/") + "/v1/models", timeout=timeout) as res:
        data = json.loads(res.read().decode("utf-8")).get("data") or []
    return (data[0].get("id") if data else "") or ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://llm.home:8080")
    ap.add_argument("--model", default="", help="default: whatever /v1/models serves first")
    ap.add_argument("--interval", type=float, default=12.0)
    ap.add_argument("--burst", type=int, default=1,
                    help="send this many requests back to back, then wait --interval. "
                         "Bursty is the point: a steady drip hides the difference "
                         "between a windowed rate and an average-since-scrape gauge, "
                         "and the idle gap between bursts is the only thing that "
                         "exercises the idle/clamp classes at all.")
    ap.add_argument("--max-tokens", type=int, default=48)
    ap.add_argument("--duration", default="", help="stop after this long, e.g. 1h (default: run forever)")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many requests")
    args = ap.parse_args()

    seconds = None
    if args.duration:
        import re
        match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", args.duration.strip())
        if not match:
            raise SystemExit(f"not a duration: {args.duration}")
        seconds = float(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]

    model = args.model or resolve_model(args.url)
    if not model:
        raise SystemExit("no model id from /v1/models")
    deadline = (time.time() + seconds) if seconds else None
    sent, failures, tokens = 0, 0, 0

    def one_request() -> bool:
        """One request; returns True when the loop should stop afterwards."""
        nonlocal sent, failures, tokens
        prompt = PREFIX + TAILS[sent % len(TAILS)]
        try:
            answer = completions(args.url, model, prompt, args.max_tokens, timeout=120)
            usage = answer.get("usage") or {}
            tokens += int(usage.get("completion_tokens") or 0)
            sent += 1
            print(f"[load] {sent} prompt={usage.get('prompt_tokens')} "
                  f"completion={usage.get('completion_tokens')} "
                  f"timings={ {k: v for k, v in (answer.get('timings') or {}).items() if k in ('prompt_ms', 'predicted_ms')} }",
                  flush=True)
        except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError) as err:
            failures += 1
            print(f"[load] failed: {err}", file=sys_stderr(), flush=True)
        if args.limit and sent >= args.limit:
            return True
        return bool(deadline and time.time() >= deadline)

    while True:
        stop = False
        for _ in range(max(1, args.burst)):
            if one_request():
                stop = True
                break
        if stop:
            break
        time.sleep(args.interval)
    print(f"[load] {sent} requests, {failures} failures, {tokens} completion tokens")
    return 0


def sys_stderr():
    import sys
    return sys.stderr


if __name__ == "__main__":
    raise SystemExit(main())
