#!/usr/bin/env python3
"""Measure what a candidate exporter actually costs, in the units the budget uses.

`workspace-szw.9` made the monitoring stack's footprint a *budget* (ADR 0001 C5:
prometheus 512 MiB + grafana 256 MiB, total added RSS ≤ 768 MiB), so a GPU
exporter proposal has to be quoted against that budget in the same units.
`monitoring/measure_footprint.sh` does it for containers; this does it for a
loose process, which is what an exporter is:

    python3 measure_cost.py --label "node_exporter + drm" --window 20 \
        --scrape http://127.0.0.1:9101/metrics -- node_exporter --web.listen-address=...

    python3 measure_cost.py --label "probe via textfile (cron)" --repeat 20 \
        -- amdgpu_sysfs_probe.py --sysfs-root ... textfile /tmp/tf

Numbers it reports, per process (children included), matching the footprint
table in monitoring/README.md:

* **RSS**   `/proc/<pid>/status` VmRSS   — what the kernel charges it now
* **PSS**   `/proc/<pid>/smaps_rollup` Pss — shared pages divided by the sharers,
  i.e. what killing it would actually free (the honest number when several
  processes share an interpreter or a libc)
* **swap**  VmSwap — non-zero on this box means the memswap ceiling is being
  paid for in the wrong place
* **CPU**   utime+stime delta over the window, as % of one core
* **series** distinct metric lines at `--scrape`, because Prometheus head memory
  scales with series, not with history

Nothing here is magic: it is /proc arithmetic, so it can be checked by hand.
"""

from __future__ import annotations

import argparse
import os
import resource
import subprocess
import time
import urllib.request
from pathlib import Path


def descendants(pid: int) -> list[int]:
    """pid plus every descendant (exporters do fork helpers)."""
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry.name))
    out, stack = [], [pid]
    while stack:
        now = stack.pop()
        out.append(now)
        stack.extend(children.get(now, []))
    return out


def _field(path: Path, key: str) -> float:
    try:
        for line in path.read_text().splitlines():
            if line.startswith(key):
                return float(line.split()[1])
    except OSError:
        pass
    return 0.0


def sample(pids: list[int]) -> tuple[float, float, float, float]:
    """(rss_kib, pss_kib, swap_kib, cpu_ticks) summed over the given pids."""
    rss = pss = swap = cpu = 0.0
    for pid in pids:
        rss += _field(Path(f"/proc/{pid}/status"), "VmRSS:")
        pss += _field(Path(f"/proc/{pid}/smaps_rollup"), "Pss:")
        swap += _field(Path(f"/proc/{pid}/status"), "VmSwap:")
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            cpu += float(fields[11]) + float(fields[12])     # utime + stime, clock ticks
        except (OSError, IndexError):
            pass
    return rss, pss, swap, cpu


def count_series(url: str) -> int:
    """Distinct samples a scrape would ingest (comment/blank lines excluded)."""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read().decode("utf-8", "replace")
    except Exception:                                            # noqa: BLE001 - reporting, not failing
        return -1
    return sum(1 for line in body.splitlines() if line and not line.startswith("#"))


def row(label: str, rss_kib: float, pss_kib: float, swap_kib: float,
        cpu_text: str, series: int, note: str = "") -> str:
    mib = lambda k: f"{k / 1024:.1f} MiB"                       # noqa: E731
    return (f"| {label} | {mib(rss_kib)} | {mib(pss_kib)} | {mib(swap_kib)} | "
            f"{cpu_text} | {'—' if series < 0 else series} | {note or 'measured'} |")


def measure_resident(cmd: list[str], label: str, window: float, scrape: str | None,
                     env: dict[str, str] | None = None) -> int:
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={**os.environ, **(env or {})})
    time.sleep(min(5.0, window / 2))                        # let it settle / first-scrape
    pids = descendants(proc.pid)
    if not pids:
        print(f"| {label} | — | — | — | — | — | **not running** |")
        proc.kill()
        return 1
    before = sample(pids)
    scrapes = 0
    deadline = time.monotonic() + window
    series = -1
    while time.monotonic() < deadline:
        if scrape:
            series = count_series(scrape)
            scrapes += 1
        time.sleep(1.0)
    after = sample(pids)
    ticks = os.sysconf("SC_CLK_TCK") or 100
    cpu_pct = (after[3] - before[3]) / ticks / window * 100
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
    print(row(label, after[0], after[1], after[2], f"{cpu_pct:.1f} %", series,
              f"measured over {window:.0f} s, {scrapes} scrapes"))
    return 0


def measure_repeated(cmd: list[str], label: str, repeat: int) -> int:
    """One-shot mode: what a cron/unit-driven textfile writer costs per run."""
    peak_rss = 0
    usage0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    wall0 = time.monotonic()
    for _ in range(repeat):
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        peak_rss = max(peak_rss, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_ms = ((usage.ru_utime - usage0.ru_utime) + (usage.ru_stime - usage0.ru_stime)) / repeat * 1000
    wall_ms = (time.monotonic() - wall0) / repeat * 1000
    print(row(label, peak_rss, 0, 0, f"{cpu_ms:.1f} ms/run", -1,
              f"peak RSS of one run; {cpu_ms:.1f} ms CPU, {wall_ms:.1f} ms wall per run "
              f"× {repeat}; resident cost 0"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--label", required=True, help="row label for the markdown table")
    ap.add_argument("--window", type=float, default=20, help="sample window, seconds")
    ap.add_argument("--scrape", default=None, help="URL to scrape during the window (counts series)")
    ap.add_argument("--repeat", type=int, default=0, help="one-shot mode: run the command N times")
    ap.add_argument("--env", action="append", default=[], help="EXTRA=VALUE for the child")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    cmd = [a for a in args.cmd if a != "--"]
    if not cmd:
        ap.error("no command to measure (use -- <cmd> ...)")
    env = dict(kv.split("=", 1) for kv in args.env if "=" in kv)
    if args.repeat:
        return measure_repeated(cmd, args.label, args.repeat)
    return measure_resident(cmd, args.label, args.window, args.scrape, env)


if __name__ == "__main__":
    raise SystemExit(main())
