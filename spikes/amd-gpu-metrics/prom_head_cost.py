#!/usr/bin/env python3
"""What the GPU series cost *Prometheus*, not just the exporter.

The footprint budget in ADR 0001 C5 is about the whole stack, and the cheap
claim "13 more series is nothing" should be measured rather than asserted:
Prometheus head memory scales with the number of series. This runs the pinned
Prometheus (version read out of `monitoring/compose.yaml`) twice — once scraping
the repo's own mock model server, once scraping that **plus** a GPU exporter —
and reports the difference in head series and resident memory:

    python3 prom_head_cost.py --prometheus /path/to/prometheus \
        --node-exporter /path/to/node_exporter --sysfs-root fixtures/sysfs-gfx1151

Every component is the repo's own double: `monitor/tests/mock_upstream.py` for
the model server, the sysfs fixture for the GPU. Nothing needs a GPU box, and
the numbers are the same order as the box's because the series *shapes* are the
box's shapes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
from measure_cost import descendants, sample        # noqa: E402  (same /proc arithmetic)

CONFIG = """
global:
  scrape_interval: {interval}s
scrape_configs:
  - job_name: llm-server
    static_configs: [{{targets: ['127.0.0.1:{mock_port}']}}]
  - job_name: prometheus
    static_configs: [{{targets: ['127.0.0.1:{prom_port}']}}]
{extra}
"""

GPU_JOB = """  - job_name: host-gpu
    static_configs: [{{targets: ['127.0.0.1:{ne_port}']}}]
"""


def query(port: int, expr: str) -> float | None:
    url = f"http://127.0.0.1:{port}/api/v1/query?query={urllib.parse.quote(expr)}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.load(response)
    except Exception:                                              # noqa: BLE001 - still warming up
        return None
    result = payload.get("data", {}).get("result", [])
    return float(result[0]["value"][1]) if result else None


def pinned_prometheus_version() -> str:
    try:
        text = (REPO / "monitoring" / "compose.yaml").read_text()
        return re.search(r"prom/prometheus:v([0-9.]+)", text).group(1)
    except Exception:                                              # noqa: BLE001
        return "unknown"


def run_case(label, prom, node_exporter, sysfs_root, mock_port, prom_port, ne_port,
             interval, warmup, window, mode) -> dict:
    """mode: "none" | "full" (default collectors) | "gpu" (drm+hwmon only)."""
    procs = []
    mock = REPO / "monitor" / "tests" / "mock_upstream.py"
    procs.append(subprocess.Popen([sys.executable, str(mock), "--port", str(mock_port),
                                   "--mode", "healthy"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    if mode != "none":
        ne_cmd = [node_exporter, f"--web.listen-address=127.0.0.1:{ne_port}",
                  f"--path.sysfs={sysfs_root}", "--collector.drm", "--collector.hwmon"]
        if mode == "gpu":
            ne_cmd.insert(1, "--collector.disable-defaults")
        procs.append(subprocess.Popen(ne_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    cfg = CONFIG.format(interval=interval, mock_port=mock_port, prom_port=prom_port,
                        extra=GPU_JOB.format(ne_port=ne_port) if mode != "none" else "")
    data = Path(f"/tmp/prom-head-{prom_port}")
    shutil.rmtree(data, ignore_errors=True)
    conf = Path(f"/tmp/prom-head-{prom_port}.yml")
    conf.write_text(cfg)
    procs.append(subprocess.Popen([prom, f"--config.file={conf}", f"--storage.tsdb.path={data}",
                                   f"--web.listen-address=127.0.0.1:{prom_port}",
                                   "--storage.tsdb.retention.time=5m"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    prom_pids = []
    deadline = time.monotonic() + warmup
    while time.monotonic() < deadline:
        prom_pids = descendants(procs[-1].pid)
        time.sleep(1)
    series = None
    ticks = os.sysconf("SC_CLK_TCK") or 100
    before = sample(prom_pids)
    for _ in range(int(window)):
        series = query(prom_port, "prometheus_tsdb_head_series")
        time.sleep(1)
    after = sample(prom_pids)
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
    rss, pss, _, _ = after
    cpu = (after[3] - before[3]) / ticks / window * 100
    return dict(label=label, series=series, pss_mib=pss / 1024, rss_mib=rss / 1024, cpu_pct=cpu)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prometheus", required=True)
    ap.add_argument("--node-exporter", required=True)
    ap.add_argument("--sysfs-root", default=str(HERE / "fixtures" / "sysfs-gfx1151"))
    ap.add_argument("--interval", type=int, default=5, help="scrape interval (s); the box uses 15")
    ap.add_argument("--warmup", type=float, default=30)
    ap.add_argument("--window", type=float, default=30)
    args = ap.parse_args()

    print(f"prometheus {pinned_prometheus_version()} (pin from monitoring/compose.yaml), "
          f"scrape interval {args.interval}s, window {args.window:.0f}s\n")
    print("| run | head series | prometheus PSS | prometheus RSS | CPU |")
    print("| --- | --- | --- | --- | --- |")
    base = run_case("model server only", args.prometheus, args.node_exporter, args.sysfs_root,
                    8121, 9091, 9121, args.interval, args.warmup, args.window, mode="none")
    gpu_only = run_case("+ GPU only (node_exporter --collector.disable-defaults drm+hwmon)",
                        args.prometheus, args.node_exporter, args.sysfs_root,
                        8122, 9092, 9122, args.interval, args.warmup, args.window, mode="gpu")
    full = run_case("+ whole host (node_exporter default collectors: cpu, mem, disk, net + GPU)",
                    args.prometheus, args.node_exporter, args.sysfs_root,
                    8123, 9093, 9123, args.interval, args.warmup, args.window, mode="full")
    for row in (base, gpu_only, full):
        print("| {label} | {series:.0f} | {pss_mib:.1f} MiB | {rss_mib:.1f} MiB | {cpu_pct:.1f} % |"
              .format(**row))
    for tag, row in (("GPU only", gpu_only), ("whole host", full)):
        delta_series = (row["series"] or 0) - (base["series"] or 0)
        delta_mib = row["pss_mib"] - base["pss_mib"]
        print(f"{tag}: {delta_series:+.0f} series, {delta_mib:+.1f} MiB Prometheus PSS "
              f"(single-run noise is a few MiB — repeat before quoting).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
