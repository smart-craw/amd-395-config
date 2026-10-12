#!/usr/bin/env bash
# One command on the GPU box: collect everything workspace-szw.13's acceptance
# criteria ask for, in the shape they are supposed to be pasted into.
#
#   monitoring/collect_host_baseline.sh                 # read-only pass
#   monitoring/collect_host_baseline.sh --window 24h    # widen the baseline window
#   monitoring/collect_host_baseline.sh --footprint     # also run measure_footprint.sh
#   monitoring/collect_host_baseline.sh --json /tmp/b.json
#   monitoring/collect_host_baseline.sh --fixture spikes/amd-gpu-metrics/fixtures/sysfs-gfx1151
#
# --fixture repoints the probe at a sysfs tree instead of /sys: it proves the plumbing
# (permissions, selectors, this script) on a machine with no GPU, and nothing else. The
# matching exporter flag is node_exporter --path.sysfs=<same tree>.
#
# Why a script and not a checklist in the runbook: the findings note
# (spikes/amd-gpu-metrics/FINDINGS.md §7) left five questions that can only be
# answered on the box, and a threshold guessed from a development container is the
# failure mode the note warns about — an alert that either never fires or pages at
# 02:00. So the questions become commands, the commands become output, and the
# output is the evidence.
#
# Read-only: it scrapes, queries and reads /proc. It changes nothing, and it prints
# `no data` / `not running` instead of a plausible-looking zero.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PROM_URL="${PROM_URL:-http://127.0.0.1:${PROMETHEUS_PORT:-9090}}"
EXPORTER_URL="${EXPORTER_URL:-http://127.0.0.1:9100}"
WINDOW="${WINDOW:-24h}"
STEP="${STEP:-15m}"
FOOTPRINT=0
JSON=""
FIXTURE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --window) WINDOW="$2"; shift 2 ;;
    --step) STEP="$2"; shift 2 ;;
    --footprint) FOOTPRINT=1; shift ;;
    --json) JSON="$2"; shift 2 ;;
    --fixture) FIXTURE="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# prom_query <promql> -> first value, or the empty string.
prom_query() {
  curl -fsS -m 5 --get "$PROM_URL/api/v1/query" --data-urlencode "query=$1" 2>/dev/null |
    python3 -c 'import json,sys
try:
    r = json.load(sys.stdin)["data"]["result"]
    print(r[0]["value"][1] if r else "")
except Exception:
    print("")' 2>/dev/null
}

# prom_range <promql> -> "min mean max n" over the window, or empty.
prom_range() {
  local start end
  end="$(date +%s)"
  start="$(python3 -c "import re,sys,time
w='$WINDOW'.strip()
m=re.fullmatch(r'([0-9]+)([smhdw])', w)
mult={'s':1,'m':60,'h':3600,'d':86400,'w':604800}[m.group(2)] if m else 3600
print(int(time.time()) - int(m.group(1))*mult)" 2>/dev/null)"
  curl -fsS -m 20 --get "$PROM_URL/api/v1/query_range" \
    --data-urlencode "query=$1" --data-urlencode "start=$start" \
    --data-urlencode "end=$end" --data-urlencode "step=$STEP" 2>/dev/null |
    python3 -c 'import json,sys
try:
    res = json.load(sys.stdin)["data"]["result"]
except Exception:
    print(""); raise SystemExit
vals=[float(v[1]) for _, v in sum((s["values"] for s in res), []) if v[1] not in ("NaN",)]
print("%.4f %.4f %.4f %d" % (min(vals), sum(vals)/len(vals), max(vals), len(vals)) if vals else "")' 2>/dev/null
}

row() { printf '| %s | %s | %s |\n' "$1" "${2:-—}" "${3:-}"; }
row2() { printf '| %s | %s |\n' "$1" "${2:-—}"; }
q() { # label, promql -> bullet with the instant value, or — when there is none
  local value; value="$(prom_query "$2")"
  printf -- '- `%s` → **%s**\n' "$2" "${value:-—}"; }

echo "## Host + GPU telemetry: the box's own answers"
echo
echo "collected $(date -Is) on $(uname -srmo); Prometheus at $PROM_URL, exporter at $EXPORTER_URL;"
echo "baseline window $WINDOW (step $STEP)."
echo

# --------------------------------------------------------------------------- #
echo "### 1. Is the exporter deployed, and at the pin?"
echo
systemctl --user is-active node-exporter >/dev/null 2>&1 \
  && echo "- node-exporter.service: **active**" \
  || echo "- node-exporter.service: **not active** (\`systemctl --user status node-exporter\`)"
"$(command -v node_exporter || echo "$HOME/.local/bin/node_exporter")" --version 2>/dev/null |
  awk '/^node_exporter, version/ {print "- binary: node_exporter " $3 " (revision " $5 ")"; exit}'
bash "$HERE/install_node_exporter.sh" --check 2>&1 | sed 's/^/- pin check: /'
echo "- last log lines:"
journalctl --user -u node-exporter --no-pager -n 5 2>/dev/null | sed 's/^/  /' ||
  echo "  (journalctl unavailable here — run it as the service's user)"
echo

# --------------------------------------------------------------------------- #
echo "### 2. The probe's capability matrix (findings note §7 item 1)"
echo
echo "Which amdgpu attributes the kernel answers, and which it refuses — this is the"
echo "table the spike could not fill in from a container with no GPU. **Paste it in**:"
if [ -n "$FIXTURE" ]; then
  echo
  echo "(against \`$FIXTURE\`, i.e. a fixture: it proves the plumbing, **not** the hardware)"
fi
echo
echo '```'
PROBE=(python3 "$REPO/spikes/amd-gpu-metrics/amdgpu_sysfs_probe.py")
[ -n "$FIXTURE" ] && PROBE=(python3 "$REPO/spikes/amd-gpu-metrics/amdgpu_sysfs_probe.py"
                            --sysfs-root "$FIXTURE")
PROBE_OUT="$("${PROBE[@]}" probe 2>&1)"
printf '%s\n' "$PROBE_OUT"
[ -n "$PROBE_OUT" ] || echo "probe failed — run it by hand as the service's user"
echo '```'
echo
echo "hwmon sensor names node_exporter actually publishes (what the temp alerts' \`chip\`/"
echo "sensor selectors must match):"
echo
echo '```'
curl -fsS -m 5 "$EXPORTER_URL/metrics" 2>/dev/null | grep -E '^node_hwmon' | head -20 ||
  echo "(exporter not reachable on $EXPORTER_URL)"
echo '```'
echo
echo "Kernel side (names the GC/SMU IP versions this kernel bound — check the findings"
echo "note's source citations against these):"
echo
echo '```'
{ dmesg 2>/dev/null || journalctl -k --no-pager 2>/dev/null; } | grep -i amdgpu | head -12 \
  || echo "(needs root: sudo dmesg | grep -i amdgpu | head)"
echo '```'
echo

# --------------------------------------------------------------------------- #
echo "### 3. Acceptance: are the series actually in Prometheus?"
echo
if ! curl -fsS -m 5 "$PROM_URL/-/ready" >/dev/null 2>&1; then
  echo "Prometheus is not reachable on $PROM_URL — everything below reads —."
fi
echo "| series | now | note |"
echo "| --- | --- | --- |"
row 'up{job="host"}' "$(prom_query 'up{job="host"}')" '1 = the container reaches the host exporter'
row 'node_scrape_collector_success{collector="drm"}' \
    "$(prom_query 'node_scrape_collector_success{collector="drm"}')" 'must be **1**, not 0'
row 'node_drm_memory_gtt_used_bytes' "$(prom_query 'node_drm_memory_gtt_used_bytes')" 'acceptance series'
row 'node_drm_memory_gtt_size_bytes' "$(prom_query 'node_drm_memory_gtt_size_bytes')" 'the amdgpu.gttsize budget'
row 'node_drm_gpu_busy_percent' "$(prom_query 'node_drm_gpu_busy_percent')" 'acceptance series'
row 'max(node_hwmon_temp_celsius{chip=~"amdgpu.*"})' \
    "$(prom_query 'max(node_hwmon_temp_celsius{chip=~"amdgpu.*"})')" 'hottest GPU sensor; — = no such sensor'
row 'count({job="host"})' "$(prom_query 'count({job="host"})')" 'series the host job costs'
row 'node_exporter_build_info' "$(prom_query 'node_exporter_build_info')" 'version as Prometheus sees it'
echo
echo "Up-for-24h is a question about history, not about this moment:"
echo
echo "- longest continuous stretch this job has been up: \`avg_over_time(up{job=\"host\"}[24h])\` = **$(prom_query 'avg_over_time(up{job="host"}[24h])')** (1.0 = up the whole day)"
echo

# --------------------------------------------------------------------------- #
echo "### 4. Steady state, so the thresholds are measured (findings note §7 item 2)"
echo
echo "The point of this section: \`GTT_PRESSURE_WARN\`, \`GPU_TEMP_*\` and \`PSI_MEMORY_*\`"
echo "in monitoring/prometheus/alert_thresholds.env are supposed to be set from these"
echo "numbers, not from a guess. min / mean / max over $WINDOW:"
echo
echo "| signal | min mean max (n) |"
echo "| --- | --- |"
row2 'GTT used / size' "$(prom_range 'node_drm_memory_gtt_used_bytes / node_drm_memory_gtt_size_bytes')"
row2 'GTT free, GiB' "$(prom_range '(node_drm_memory_gtt_size_bytes - node_drm_memory_gtt_used_bytes) / 2**30')"
row2 'GPU busy %' "$(prom_range 'node_drm_gpu_busy_percent')"
row2 'hottest GPU sensor, °C' "$(prom_range 'max(node_hwmon_temp_celsius{chip=~"amdgpu.*"})')"
row2 'memory PSI (stalled fraction)' "$(prom_range 'rate(node_pressure_memory_waiting_seconds_total[5m])')"
row2 'KV cache used (ratio)' "$(prom_range 'llamacpp:kv_cache_usage_ratio')"
row2 'MemAvailable, GiB' "$(prom_range 'node_memory_MemAvailable_bytes / 2**30')"
echo
echo "Recorded baselines (the \`host:*\` rules; empty for the first 15-minute step, meaningful after 2 days):"
echo
q 'host:gtt_usage_ratio:max_2d' 'host:gtt_usage_ratio:max_2d'
q 'host:gtt_usage_ratio:mean_2d' 'host:gtt_usage_ratio:mean_2d'
q 'host:psi_memory_waiting_ratio:max_2d' 'host:psi_memory_waiting_ratio:max_2d'
q 'host:gpu_temp_celsius:max_2d' 'host:gpu_temp_celsius:max_2d'
echo
echo "To apply them: edit prometheus/alert_thresholds.env, then \`python3 monitoring/render_rules.py\`,"
echo "\`promtool test rules monitoring/prometheus/unit-tests.yml\`, restart Prometheus, commit both files."
echo

# --------------------------------------------------------------------------- #
echo "### 5. The payoff panel over a real window"
echo
echo "The overlay is only worth having if GTT and KV actually move together, so ask the"
echo "TSDB rather than the panel. min/mean/max over $WINDOW:"
echo
echo "| series | min mean max (n) |"
echo "| --- | --- |"
row2 'GTT used (fraction of budget)' "$(prom_range 'node_drm_memory_gtt_used_bytes / node_drm_memory_gtt_size_bytes')"
row2 'KV cache (fraction of pool)' "$(prom_range 'llm:kv_cache_usage_ratio')"
echo
python3 - "$PROM_URL" "$WINDOW" "$STEP" <<'PY' 2>/dev/null || true
import json, re, subprocess, sys, time
url, window, step = sys.argv[1], sys.argv[2], sys.argv[3]
mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[window[-1]]
span = int(window[:-1]) * mult
def get(expr):
    out = subprocess.run(["curl", "-fsS", "-m", "20", "--get", f"{url}/api/v1/query_range",
                          "--data-urlencode", f"query={expr}",
                          "--data-urlencode", f"start={int(time.time()) - span}",
                          "--data-urlencode", f"end={int(time.time())}",
                          "--data-urlencode", f"step={step}"],
                         capture_output=True, text=True).stdout
    try:
        res = json.loads(out)["data"]["result"]
    except Exception:
        return []
    return {float(v[1]) for s in res for _, v in s["values"]}
gtt, kv = get("node_drm_memory_gtt_used_bytes / node_drm_memory_gtt_size_bytes"), get("llm:kv_cache_usage_ratio")
print()
if not gtt or not kv:
    print("**Cannot answer yet: one of the two series has no samples in the window.** Fix that first —")
    print("the panel is not legible, it is empty, which is a different problem (see the runbook).")
else:
    print(f"- GTT moved over {len(gtt)} distinct values in the window, KV over {len(kv)};")
    print("  if one of the two is a flat line, the correlation the panel promises is not")
    print("  testable from this window — widen --window, or generate traffic and re-run.")
PY
echo
echo "Paste-ready check of the panel's own expressions (the same range query Grafana sends):"
echo
echo '```sh'
echo "curl -sG --data-urlencode 'query=node_drm_memory_gtt_used_bytes / node_drm_memory_gtt_size_bytes' \\"
echo "     --data-urlencode \"start=\$(date -d '-$WINDOW' +%s)\" --data-urlencode \"end=\$(date +%s)\" \\"
echo "     --data-urlencode 'step=$STEP' http://127.0.0.1:9090/api/v1/query_range"
echo '```'
echo

# --------------------------------------------------------------------------- #
echo "### 6. Cost, measured on the box (findings note §7 item 4)"
echo
if [ "$FOOTPRINT" = 1 ]; then
  echo '```'
  bash "$HERE/measure_footprint.sh" --window "${WINDOW_SECONDS:-30}" 2>&1
  echo '```'
else
  echo "Not run (pass \`--footprint\`). It prints the rows for the footprint table in"
  echo "monitoring/README.md, including the exporter's own RSS/PSS/CPU. Then the"
  echo "benchmark from the root README, once with the exporter up and once with it"
  echo "stopped: the GTT ceiling is shared with the model, so 'the monitor stole the"
  echo "model's memory' is a real if unlikely failure and one run settles it."
fi
echo
echo "---"
echo
echo "Findings note open questions answered above: §7 item 1 sensors (§2), §7 item 2 steady state"
echo "(§4), §7 item 3 labels + collector success (§3), §7 item 4 cost (§6). §7 item 5 (per-process"
echo "fdinfo panel) stays deferred: it needs the v2 timer + textfile collector, and the"
echo "fdinfo visibility question it depends on is answerable here in one command:"
echo
echo '```sh'
echo "python3 spikes/amd-gpu-metrics/amdgpu_sysfs_probe.py metrics | grep amdgpu_process_memory || echo 'no per-process GPU memory visible from this namespace'"
echo '```'

if [ -n "$JSON" ]; then
  {
    printf '{"ts":"%s","prom":"%s","window":"%s"\n' "$(date -Is)" "$PROM_URL" "$WINDOW"
    printf ',"up_host":"%s"\n' "$(prom_query 'up{job="host"}')"
    printf ',"drm_collector":"%s"\n' "$(prom_query 'node_scrape_collector_success{collector=\"drm\"}')"
    printf ',"gtt_ratio":"%s"\n' "$(prom_query 'node_drm_memory_gtt_used_bytes / node_drm_memory_gtt_size_bytes')"
    printf ',"host_series":"%s"}\n' "$(prom_query 'count({job=\"host\"})')"
  } > "$JSON"
  echo "(wrote $JSON)"
fi
