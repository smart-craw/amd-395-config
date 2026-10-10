#!/usr/bin/env bash
# Measure what the monitoring stack actually costs this box, and print the
# markdown table that belongs in monitoring/README.md#footprint.
#
#   monitoring/measure_footprint.sh            # stack up, model loaded and idle
#   monitoring/measure_footprint.sh --before   # the same numbers with the stack DOWN
#
# Why a script: the ADR makes the footprint a *budget* ("measured, documented and
# bounded"), and a budget nobody re-measures is a wish. Everything it reads is
# world-readable; nothing is installed.
#
# What it records, per process:
#   RSS  from /proc/<pid>/status   (what the kernel currently charges it)
#   PSS  from /proc/<pid>/smaps_rollup (shared pages divided by the sharers —
#        the honest number for "what would we free by killing this")
#   CPU  from /proc/<pid>/stat utime+stime over the sample window
#   swap from /proc/<pid>/status (this box runs memswap_limit == mem_limit, so
#        non-zero swap here means the ceiling is being paid for in the wrong place)
# plus the TSDB's on-disk growth per day, which is what retention.size bounds.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WINDOW_SECONDS="${WINDOW_SECONDS:-30}"
BEFORE=0
[ "${1:-}" = "--before" ] && BEFORE=1

command -v docker >/dev/null 2>&1 || { echo "error: docker not found" >&2; exit 1; }

# All descendant pids of a container's main process (prometheus spawns helpers).
container_pids() {
  local cid
  cid="$(docker inspect -f '{{.State.Pid}}' "$1" 2>/dev/null)" || return 0
  [ "$cid" = "0" ] && return 0
  local pid
  for pid in $(grep -l "^Ppid:\|^PPid:" /proc/[0-9]*/status 2>/dev/null | cut -d/ -f3); do
    if [ "$(awk '/^PPid:/{print $2}' "/proc/$pid/status" 2>/dev/null)" = "$cid" ] || [ "$pid" = "$cid" ]; then
      echo "$pid"
    fi
  done
}

proc_stat() { # pid -> rss_kb pss_kb swap_kb cpu_ticks
  local pid="$1" rss=0 pss=0 swap=0 cpu=0
  [ -r "/proc/$pid/status" ] || return 0
  rss="$(awk '/^VmRSS:/{print $2}' "/proc/$pid/status" 2>/dev/null)"
  swap="$(awk '/^VmSwap:/{print $2}' "/proc/$pid/status" 2>/dev/null)"
  pss="$(awk '/^Pss:/{print $2}' "/proc/$pid/smaps_rollup" 2>/dev/null)"
  cpu="$(awk '{print $14 + $15}' "/proc/$pid/stat" 2>/dev/null)"
  echo "${rss:-0} ${pss:-0} ${swap:-0} ${cpu:-0}"
}

summarise() { # label pid...
  local label="$1"; shift
  local rss=0 pss=0 swap=0 cpu0=0 pid
  local -a pids=()
  for pid in "$@"; do [ -n "$pid" ] && pids+=("$pid"); done
  [ ${#pids[@]} -eq 0 ] && { printf '| %s | — | — | — | — |\n' "$label"; return; }
  for pid in "${pids[@]}"; do read -r rss pss swap cpu0 <<<"$(proc_stat "$pid")"; break; done
  local total_rss=0 total_pss=0 total_swap=0
  for pid in "${pids[@]}"; do
    read -r r p s c <<<"$(proc_stat "$pid")"
    total_rss=$((total_rss + ${r:-0})); total_pss=$((total_pss + ${p:-0})); total_swap=$((total_swap + ${s:-0}))
  done
  local cpu_now=0
  for pid in "${pids[@]}"; do read -r _ _ _ c <<<"$(proc_stat "$pid")"; cpu_now=$((cpu_now + ${c:-0})); done
  sleep "$WINDOW_SECONDS"
  local cpu_after=0
  for pid in "${pids[@]}"; do read -r _ _ _ c <<<"$(proc_stat "$pid")"; cpu_after=$((cpu_after + ${c:-0})); done
  local hz
  hz="$(getconf CLK_TCK)"
  local pct
  pct="$(awk -v a="$cpu_after" -v b="$cpu_now" -v hz="$hz" -v w="$WINDOW_SECONDS" 'BEGIN{printf "%.1f", (a-b)*100/(hz*w)}')"
  printf '| %s | %s | %s | %s | %s%% |\n' "$label" \
    "$((total_rss / 1024)) MB" "$((total_pss / 1024)) MB" "$((total_swap / 1024)) MB" "$pct"
}

tsdb_bytes() {
  docker run --rm -v monitoring_prometheus-tsdb:/p:ro alpine du -sb /p 2>/dev/null | awk '{print $1}'
}

echo "### Footprint (${WINDOW_SECONDS}s sample, $(date -Is), stack: $([ "$BEFORE" = 1 ] && echo DOWN || echo UP))"
echo
echo "| process | RSS | PSS | swap | CPU |"
echo "| ------- | --- | --- | ---- | --- |"

if [ "$BEFORE" = 1 ]; then
  # The "before" column of the table: the monitor page alone.
  MON_PID="$(pgrep -f "monitor/serve.py" | head -1 || true)"
  summarise "monitor/serve.py" "$MON_PID"
  echo
  echo "Bring the stack up with monitoring/up.sh and re-run without --before."
  exit 0
fi

summarise "monitor/serve.py" "$(pgrep -f 'monitor/serve.py' | head -1 || true)"
summarise "prometheus" $(container_pids llm-prometheus)
summarise "grafana" $(container_pids llm-grafana)

TSDB="$(tsdb_bytes)"
START_TSDB="${START_TSDB:-0}"
echo
if [ -n "$TSDB" ] && [ "$START_TSDB" != 0 ]; then
  echo "TSDB: $((TSDB / 1024 / 1024)) MB now; growth observed over this session."
fi
echo "TSDB on disk: ${TSDB:-unknown} bytes (volume monitoring_prometheus-tsdb)"
docker exec llm-prometheus wget -qO- 'http://localhost:9090/api/v1/status/tsdb' 2>/dev/null | \
  python3 -c "
import json,sys
try:
    d = json.load(sys.stdin)['data']['stats']
    print('series count:', d.get('numSeries'), '| HEAD chunks:', d.get('headChunksInMemory'))
except Exception as exc:
    print('tsdb status unavailable:', exc)
" 2>/dev/null || echo "(prometheus /status/tsdb not reachable)"
echo
echo "Retention cap: docker exec llm-prometheus wget -qO- http://localhost:9090/api/v1/status/flags | python3 -c \"import json,sys; f=json.load(sys.stdin)['data']; print(f['storage.tsdb.retention.size'], f['storage.tsdb.retention.time'])\""
