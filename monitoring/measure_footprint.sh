#!/usr/bin/env bash
# Measure what the monitoring stack actually costs this box, and print the
# markdown table that belongs in monitoring/README.md ("Footprint").
#
#   monitoring/measure_footprint.sh                 # compose stack up, model loaded, idle
#   monitoring/measure_footprint.sh --before        # the same numbers with the stack DOWN
#   monitoring/measure_footprint.sh --mark /tmp/growth     # twice, a day apart => TSDB growth/day
#   monitoring/measure_footprint.sh --proc grafana=12345   # measure processes with no docker
#   monitoring/measure_footprint.sh --jsonl /tmp/rows.jsonl --quiet
#
# Why a script: ADR 0001 C5 makes the footprint a *budget* ("measured, documented
# and bounded"), and a budget nobody re-measures is a wish. Everything it reads is
# world-readable; nothing is installed, nothing is written outside --jsonl/--mark.
#
# What it records, per process group:
#   RSS  from /proc/<pid>/status       (what the kernel currently charges it)
#   PSS  from /proc/<pid>/smaps_rollup (shared pages divided by the sharers --
#        the honest "what would we free by killing this" number)
#   CPU  utime+stime over the sample window, as % of one core
#   swap from /proc/<pid>/status       (this box runs memswap_limit == mem_limit,
#        so non-zero swap here means the ceiling is being paid for in the wrong place)
# plus, for docker-managed groups, `docker stats` MEM -- which is the *cgroup*
# number (memory.current, page cache included) and therefore the number that is
# actually compared against mem_limit. RSS/PSS of the processes alone under-counts
# it; both are printed because they answer different questions.
#
# And the two things that decide the budget: the TSDB's on-disk growth per day
# (what retention.size bounds) and the go/no-go verdict against
# FOOTPRINT_BUDGET_MIB (ADR 0001 C5: 768 MiB of added memory).
#
# Native mode (--proc): the same measurement without docker, for a box where
# prometheus/grafana run as plain services, and for a dev container with no
# daemon in it. `--proc LABEL=PATTERN` matches PATTERN against the full command
# line (pgrep -f), `--proc LABEL=PID` names one pid. Repeat as often as needed.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WINDOW_SECONDS="${WINDOW_SECONDS:-30}"
BEFORE=0
NO_DOCKER=0
QUIET=0
JSONL=""
MARK=""
BUDGET_MIB="${FOOTPRINT_BUDGET_MIB:-768}"
CONTAINERS=("llm-prometheus:prometheus" "llm-grafana:grafana")
declare -a NATIVE=()
PROM_URL="${PROM_URL:-http://127.0.0.1:${PROMETHEUS_PORT:-9090}}"

usage() {
  sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --before) BEFORE=1; shift ;;
    --no-docker) NO_DOCKER=1; shift ;;
    --quiet) QUIET=1; shift ;;
    --window) WINDOW_SECONDS="$2"; shift 2 ;;
    --proc) NATIVE+=("$2"); shift 2 ;;
    --container) CONTAINERS+=("$2"); shift 2 ;;
    --jsonl) JSONL="$2"; shift 2 ;;
    --mark) MARK="$2"; shift 2 ;;
    --budget-mib) BUDGET_MIB="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

say() { [ "$QUIET" = 1 ] || printf '%s\n' "$*"; }

# --------------------------------------------------------------------------- #
# /proc plumbing
# --------------------------------------------------------------------------- #

# pids whose command line matches a pattern. pgrep -f where it exists; this box
# (and any busybox/distrobox without procps) gets a /proc scan instead, so the
# tool never silently measures nothing because a helper binary is missing.
pids_matching() {
  if command -v pgrep >/dev/null 2>&1; then
    pgrep -f "$1" 2>/dev/null || true
    return
  fi
  local c pid
  for c in /proc/[0-9]*/cmdline; do
    pid="${c#/proc/}"; pid="${pid%/cmdline}"; [ -r "$c" ] || continue
    tr '\0' ' ' < "$c" 2>/dev/null | grep -q -e "$1" && printf '%s\n' "$pid"
  done
}

# All pids inside a container: `docker top` lists the container's processes with
# host-visible pids, which is exactly the set whose RSS/PSS the cgroup adds up.
# Falls back to walking PPid from the container's main pid.
container_pids() {
  local cid pids
  cid="$(docker inspect -f '{{.State.Pid}}' "$1" 2>/dev/null)" || return 0
  [ -n "$cid" ] && [ "$cid" != "0" ] || return 0
  pids="$(docker top "$1" -eo pid 2>/dev/null | tail -n +2 | tr -d ' ')"
  if [ -z "$pids" ]; then
    local pid
    for pid in /proc/[0-9]*/status; do
      pid="${pid#/proc/}"; pid="${pid%/status}"
      [ "$(awk '/^PPid:/{print $2}' "/proc/$pid/status" 2>/dev/null)" = "$cid" ] && pids+="$pid"$'\n'
    done
    pids="$cid${pids:+$'\n'}$pids"
  fi
  printf '%s\n' "$pids" | grep -E '^[0-9]+$' || true
}

# pid -> rss_kb pss_kb swap_kb cpu_ticks
proc_stat() {
  local pid="$1" rss=0 pss=0 swap=0 cpu=0
  [ -r "/proc/$pid/status" ] || { echo "0 0 0 0"; return; }
  rss="$(awk '/^VmRSS:/{print $2}' "/proc/$pid/status" 2>/dev/null)"
  swap="$(awk '/^VmSwap:/{print $2}' "/proc/$pid/status" 2>/dev/null)"
  pss="$(awk '/^Pss:/{print $2}' "/proc/$pid/smaps_rollup" 2>/dev/null)"
  # utime(14) + stime(15); the comm field (2) can contain spaces and ')', so
  # strip up to the LAST ')' and count from there: field 1 after that is state.
  cpu="$(sed 's/^.*) //' "/proc/$pid/stat" 2>/dev/null | awk '{print $12 + $13}')"
  echo "${rss:-0} ${pss:-0} ${swap:-0} ${cpu:-0}"
}

sum_kb() { # pids... -> "rss pss swap cpu0"
  local rss=0 pss=0 swap=0 cpu=0 pid r p s c
  for pid in "$@"; do
    read -r r p s c <<<"$(proc_stat "$pid")"
    rss=$((rss + ${r:-0})); pss=$((pss + ${p:-0})); swap=$((swap + ${s:-0})); cpu=$((cpu + ${c:-0}))
  done
  echo "$rss $pss $swap $cpu"
}

mib() { awk -v k="${1:-0}" 'BEGIN{printf "%.1f", k/1024}'; }

fmt_bytes() { awk -v b="${1:-0}" 'BEGIN{
  if (b >= 1073741824) printf "%.2f GiB", b/1073741824;
  else if (b >= 1048576) printf "%.1f MiB", b/1048576;
  else if (b >= 1024) printf "%.0f KiB", b/1024;
  else printf "%d B", b }'; }

# `docker stats` MEM is memory.current: RSS plus page cache, i.e. what mem_limit
# actually bounds. Returns "used_kb limit_kb mem% cpu%" or empty.
container_stats() {
  local out
  out="$(docker stats --no-stream --format '{{.MemUsage}}|{{.MemPerc}}|{{.CPUPerc}}' "$1" 2>/dev/null)"
  [ -n "$out" ] || return 0
  printf '%s' "$out" | python3 -c '
import re, sys
UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
         "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4, "": 1}
def kb(text):
    m = re.match(r"\s*([0-9.]+)\s*([KMGT]?i?B)?", text or "")
    return float(m.group(1)) * UNITS.get(m.group(2) or "", 1) / 1024.0 if m else 0.0
mem, mempct, cpupct = (sys.stdin.read().strip().split("|") + ["", "", ""])[:3]
used, _, limit = mem.partition(" / ")
print("%.0f %.0f %s %s" % (kb(used), kb(limit), mempct.strip() or "?", cpupct.strip() or "?"))
' 2>/dev/null || true
}

# --------------------------------------------------------------------------- #
# process groups -> one shared sample window -> one row each
# --------------------------------------------------------------------------- #
# The window is taken once for every group at the same moment: measuring groups
# one after another multiplies the run time and, worse, samples the last group in
# a different world than the first (a load test that has already finished reads
# 0.1% CPU and looks like a free service).
ROWS=()          # markdown rows
JSON_ROWS=()     # one JSON object per row
TOTAL_PSS_KB=0
TOTAL_RSS_KB=0
declare -a GROUP_LABELS GROUP_KINDS GROUP_PIDS GROUP_CPU0 MISSING

cpu_sum() { local c; read -r _ _ _ c <<<"$(sum_kb "$@")"; printf '%s' "$c"; }

row() { # label kind pids...  (kind: proc|container) -- registers the group
  local label="$1" kind="$2"; shift 2
  local -a pids=()
  local p
  for p in "$@"; do [ -n "$p" ] && pids+=("$p"); done
  if [ "${#pids[@]}" -eq 0 ]; then
    MISSING+=("$label")
    say "$label: no processes found -- measured as 'not running', not as zero"
    return
  fi
  GROUP_LABELS+=("$label"); GROUP_KINDS+=("$kind"); GROUP_PIDS+=("${pids[*]}")
  GROUP_CPU0+=("$(cpu_sum "${pids[@]}")")
}

emit() { # the shared window ends here; read every group and print the table
  local i hz pct label kind cg cgused cglimit stats RSS1 PSS1 SWAP1 CPU1
  hz="$(getconf CLK_TCK)"
  for i in ${GROUP_LABELS[@]+"${!GROUP_LABELS[@]}"}; do
    label="${GROUP_LABELS[$i]}"; kind="${GROUP_KINDS[$i]}"
    local -a pids; read -ra pids <<<"${GROUP_PIDS[$i]}"
    read -r RSS1 PSS1 SWAP1 CPU1 <<<"$(sum_kb "${pids[@]}")"
    pct="$(awk -v a="$CPU1" -v b="${GROUP_CPU0[$i]}" -v hz="$hz" -v w="$WINDOW_SECONDS" \
           'BEGIN{printf "%.1f", (a-b)*100.0/(hz*w)}')"
    cg="—"; cgused="null"; cglimit="null"
    if [ "$kind" = container ]; then
      stats="$(container_stats "$label")"
      if [ -n "$stats" ]; then
        read -r CGUSED CGLIMIT CMP CPUP <<<"$stats"
        cg="$(mib "$CGUSED") MiB of $(mib "$CGLIMIT") MiB (${CMP})"
        cgused="$CGUSED"; cglimit="$CGLIMIT"
        [ -n "${CPUP:-}" ] && pct="$CPUP"   # docker's own CPU figure wins for containers
      fi
    fi
    ROWS+=("| $label | $(mib "$RSS1") MiB | $(mib "$PSS1") MiB | $(mib "$SWAP1") MiB | ${pct}% | ${cg} |")
    JSON_ROWS+=("{\"label\":\"$label\",\"kind\":\"$kind\",\"pids\":${#pids[@]},\
\"rss_kib\":$RSS1,\"pss_kib\":$PSS1,\"swap_kib\":$SWAP1,\"cpu_percent\":\"$pct\",\
\"cgroup_used_kib\":$cgused,\"cgroup_limit_kib\":$cglimit,\"window_s\":$WINDOW_SECONDS}")
    # Only the added services count against the budget; serve.py was already paid for.
    case "$label" in
      monitor/serve.py) : ;;
      *) TOTAL_PSS_KB=$((TOTAL_PSS_KB + PSS1)); TOTAL_RSS_KB=$((TOTAL_RSS_KB + RSS1)) ;;
    esac
  done
  for label in ${MISSING[@]+"${MISSING[@]}"}; do
    ROWS+=("| $label | not running | not running | not running | not running | — |")
    JSON_ROWS+=("{\"label\":\"$label\",\"status\":\"not running\"}")
  done
}

tsdb_bytes() { # on-disk TSDB size, bytes; empty if unmeasurable
  if [ -n "${TSDB_DIR:-}" ] && [ -d "$TSDB_DIR" ]; then
    du -sk "$TSDB_DIR" 2>/dev/null | awk '{print $1 * 1024}'
  elif docker inspect llm-prometheus >/dev/null 2>&1; then
    # busybox du: -s, 1K blocks (no -b in busybox).
    docker exec llm-prometheus du -s /prometheus 2>/dev/null | awk '{print $1 * 1024}'
  fi
}

prom_query() { # promql -> value
  curl -fsS -m 5 --get "$PROM_URL/api/v1/query" --data-urlencode "query=$1" 2>/dev/null |
    python3 -c 'import json,sys
try:
    r = json.load(sys.stdin)["data"]["result"]
    print(r[0]["value"][1] if r else "")
except Exception:
    print("")' 2>/dev/null
}

# --------------------------------------------------------------------------- #
say "measuring for ${WINDOW_SECONDS}s ($(date -Is)); host $(uname -srmo); python $(python3 -V 2>&1)"
say ""

if [ "$BEFORE" = 1 ]; then
  row "monitor/serve.py" proc "$(pids_matching 'monitor/serve.py' | head -1 || true)"
else
  row "monitor/serve.py" proc "$(pids_matching 'monitor/serve.py' | head -1 || true)"
  if [ "$NO_DOCKER" = 0 ]; then
    for entry in ${CONTAINERS[@]+"${CONTAINERS[@]}"}; do
      row "${entry#*:}" container $(container_pids "${entry%%:*}")
    done
  fi
fi
for entry in ${NATIVE[@]+"${NATIVE[@]}"}; do
  label="${entry%%=*}"; pat="${entry#*=}"
  if [ "$pat" = "${pat#*[!0-9]}" ] && [ -d "/proc/$pat" ]; then
    row "$label" proc "$pat"
  else
    row "$label" proc $(pids_matching "$pat" || true)
  fi
done

# One sleep, for all groups together.
sleep "$WINDOW_SECONDS"
emit

# ---- TSDB growth, measured between two runs (--mark) ----
TSDB_NOW="$(tsdb_bytes)"
GROWTH_NOTE=""
if [ -n "$TSDB_NOW" ] && [ -n "$MARK" ]; then
  if [ -s "$MARK" ]; then
    read -r T0 B0 <<<"$(head -2 "$MARK" | tr '\n' ' ')"
    GROWTH_NOTE="$(awk -v t0="$T0" -v b0="$B0" -v t1="$(date +%s)" -v b1="$TSDB_NOW" 'BEGIN{
      dt = t1 - t0;
      if (dt <= 0) { print "cannot compute growth from this mark window"; exit }
      rate = (b1 - b0) * 86400.0 / dt;
      printf "%+.1f MiB/day over %d s of measurement (%.1f MiB observed)",
             rate/1048576.0, dt, (b1-b0)/1048576.0 }')"
  fi
  printf '%s\n%s\n' "$(date +%s)" "$TSDB_NOW" > "$MARK"
fi

SERIES="$(prom_query 'count({__name__!=""})')"
HEAD_BYTES="$(prom_query 'prometheus_tsdb_storage_blocks_bytes')"
COMPACTIONS="$(prom_query 'increase(prometheus_tsdb_compactions_total[1h])')"

if [ "$QUIET" = 0 ]; then
  printf '### Footprint (%ss sample, %s, stack: %s)\n\n' "$WINDOW_SECONDS" "$(date -Is)" \
    "$([ "$BEFORE" = 1 ] && echo DOWN || echo UP)"
  printf '| process | RSS | PSS | swap | CPU | cgroup (docker stats) |\n'
  printf '| ------- | --- | --- | ---- | --- | --------------------- |\n'
  for r in ${ROWS[@]+"${ROWS[@]}"}; do printf '%s\n' "$r"; done
  printf '| **added total (PSS)** | %s MiB | **%s MiB** | — | — | — |\n' \
    "$(mib "$TOTAL_RSS_KB")" "$(mib "$TOTAL_PSS_KB")"
  echo
  if [ -n "$TSDB_NOW" ]; then
    printf -- '- TSDB on disk: %s\n' "$(fmt_bytes "$TSDB_NOW")"
  else
    printf -- '- TSDB on disk: unmeasurable (set TSDB_DIR= or run where docker can see the volume)\n'
  fi
  [ -n "$GROWTH_NOTE" ] && printf -- '- TSDB growth: %s\n' "$GROWTH_NOTE"
  printf -- '- series in TSDB: %s\n' "${SERIES:-unknown}"
  if [ -n "$HEAD_BYTES" ]; then
    printf -- '- blocks on disk: %s\n' "$(fmt_bytes "$HEAD_BYTES")"
  else
    printf -- '- blocks on disk: unknown (prometheus not reachable on %s)\n' "$PROM_URL"
  fi
  printf -- '- compactions in the last hour: %s\n' "${COMPACTIONS:-unknown}"
  echo
  VERDICT="GO: keep both services always-on"
  [ $((TOTAL_PSS_KB / 1024)) -gt "$BUDGET_MIB" ] && \
    VERDICT="NO-GO: over the ${BUDGET_MIB} MiB budget -> Prometheus stays always-on, Grafana runs on demand"
  [ "$BEFORE" = 1 ] && VERDICT="(baseline run: nothing added to the box)"
  printf -- '- budget: %s MiB added (ADR 0001 C5) -> **%s**\n' "$BUDGET_MIB" "$VERDICT"
fi

if [ -n "$JSONL" ]; then
  {
    for j in ${JSON_ROWS[@]+"${JSON_ROWS[@]}"}; do printf '%s\n' "$j"; done
    printf '{"label":"_total","added_pss_kib":%s,"added_rss_kib":%s,"budget_mib":%s,"tsdb_bytes":"%s","growth":"%s","series":"%s","ts":"%s"}\n' \
      "$TOTAL_PSS_KB" "$TOTAL_RSS_KB" "$BUDGET_MIB" "${TSDB_NOW:-}" "${GROWTH_NOTE:-}" "${SERIES:-}" "$(date -Is)"
  } >> "$JSONL"
fi
