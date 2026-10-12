#!/usr/bin/env bash
# Does the monitoring stack cost the model anything? Measure it. (workspace-szw.9)
#
#   monitoring/bench_headroom.sh --model ~/models/qwen3.8-27B/Qwen3.8-27B-UD-Q4_K_M.gguf
#   monitoring/bench_headroom.sh --http http://llm.home:8080 --http-only
#
# ADR 0001 C5 has a second half that is easy to forget once the dashboard is
# pretty: the stack may not take anything from the model. The box deliberately
# starves itself of RAM for weights (amdgpu.gttsize / ttm.pages_limit in the root
# README), so "768 MiB of telemetry" is only affordable if it does not arrive out
# of the pool the GPU is already living in.
#
# Two probes, because they answer different halves of that:
#
# 1. llama-bench, the documented invocation (root README "Run benchmarks"), run
#    with the stack DOWN and then UP. Same binary, same flags, same model, so the
#    difference is the stack. Tolerance: --tolerance percent (default 3) on the
#    worst test row.
#
#    CAVEAT, repeated from the root README because it matters here: llama-bench
#    does NOT exercise MTP / speculative decode. A speculative-decode regression
#    would be invisible in it. That is what probe 2 is for.
#
# 2. An HTTP probe against the live server: a fixed prompt, N repetitions, timing
#    measured client-side, plus the server's own counters (draft tokens offered vs
#    accepted, prompt/predict seconds) sampled around it. That does go through the
#    MTP drafter, so acceptance rate and decode speed are compared on the real
#    serving path -- the one llama-bench cannot see. It also needs no GPU on this
#    machine, so it can be re-run from anywhere that can reach the box.
#
# What it prints is meant to be pasted into monitoring/README.md ("Model
# headroom"). Exit 0 = within tolerance, 1 = regression or inconclusive.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL="${MODEL:-$HOME/models/qwen3.8-27B/Qwen3.8-27B-UD-Q4_K_M.gguf}"
BENCH="${LLAMA_BENCH:-$(command -v llama-bench || true)}"
HTTP=""
HTTP_ONLY=0
PROBE_ONLY=0
LABEL="probe"
REPS="${REPS:-5}"
TOLERANCE="${TOLERANCE:-3}"
PROMPT="${PROMPT:-Explain in two sentences why a time series database needs a size-based retention cap as well as a time-based one.}"
OUT="${OUT:-/tmp/headroom-$(date +%Y%m%d-%H%M%S)}"
COMPOSE="$HERE/compose.yaml"

while [ $# -gt 0 ]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --http) HTTP="$2"; shift 2 ;;
    --http-only) HTTP_ONLY=1; shift ;;
    --probe-only) PROBE_ONLY=1; shift ;;
    --label) LABEL="$2"; shift 2 ;;
    --reps) REPS="$2"; shift 2 ;;
    --tolerance) TOLERANCE="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
mkdir -p "$OUT"
echo "artifacts: $OUT"

stack_is_up() { docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^llm-prometheus$'; }

stack_down() {
  if command -v docker >/dev/null 2>&1 && stack_is_up; then
    echo "  stopping the stack (docker compose -f monitoring/compose.yaml stop)"
    docker compose -f "$COMPOSE" stop >/dev/null 2>&1
  fi
}
stack_up() {
  if command -v docker >/dev/null 2>&1; then
    echo "  starting the stack"
    docker compose -f "$COMPOSE" up -d >/dev/null 2>&1
    sleep 20
  fi
}

snapshot_host() { # label -> free/RSS numbers, the "did the pool shrink" side
  {
    echo "## host memory ($1, $(date -Is))"
    awk '/^MemTotal|^MemAvailable|^SwapTotal|^SwapFree/{printf "%s %s\n", $1, $2}' /proc/meminfo
    for p in /sys/module/amdgpu/parameters/gttsize /sys/kernel/debug/dri/0/mem_info_vram_used; do
      [ -r "$p" ] && echo "amdgpu $(basename "$p") $(cat "$p" 2>/dev/null)"
    done
    for pat in llama-server llama-bench halogen; do
      for pid in $(pgrep -f "$pat" 2>/dev/null || true); do
        printf '%s pid=%s ' "$pat" "$pid"
        awk '/^VmRSS|^VmSwap/{printf "%s=%s ", $1, $2}' "/proc/$pid/status" 2>/dev/null
        printf 'pss=%s\n' "$(awk '/^Pss:/{print $2}' "/proc/$pid/smaps_rollup" 2>/dev/null)"
      done
    done
  } >> "$OUT/host-$1.txt" 2>&1
}

# ---------------------------------------------------------------- HTTP probe --
http_probe() { # label
  local label="$1" f="$OUT/http-$1.txt"
  command -v curl >/dev/null 2>&1 || { echo "no curl; skipping http probe" >&2; return 1; }
  {
    echo "## http probe ($label, $(date -Is))"
    echo "endpoint: $HTTP  reps: $REPS  prompt: ${#PROMPT} chars"
    counter() { # metric -> value
      curl -fsS -m 8 "$HTTP/metrics" 2>/dev/null | awk -v m="$1" '$1==m{print $2; exit}'
    }
    local p0 d0 a0 t0
    p0="$(counter llamacpp:tokens_predicted_seconds_total)"
    d0="$(counter halogen:draft_tokens_total)"
    a0="$(counter halogen:draft_tokens_accepted_total)"
    local i best=0 worst=999 t1 t2 dt total=0
    for i in $(seq "$REPS"); do
      t1="$(python3 -c 'import time;print(time.time())')"
      curl -fsS -m 120 -o /dev/null -X POST "$HTTP/v1/chat/completions" \
        -H 'Content-Type: application/json' \
        -d "$(python3 -c 'import json,sys; print(json.dumps({"model":"default","messages":[{"role":"user","content":sys.argv[1]}],"max_tokens":96,"stream":False,"temperature":0}))' "$PROMPT")" \
        || { echo "request $i FAILED"; continue; }
      t2="$(python3 -c 'import time;print(time.time())')"
      dt="$(python3 -c "print('%.3f' % ($t2-$t1))")"
      echo "  rep $i: ${dt}s"
      total="$(python3 -c "print('%.3f' % ($total + $dt))")"
    done
    local p1 d1 a1
    p1="$(counter llamacpp:tokens_predicted_seconds_total)"
    d1="$(counter halogen:draft_tokens_total)"
    a1="$(counter halogen:draft_tokens_accepted_total)"
    echo "  total ${total}s over $REPS reps"
    python3 - "$p0" "$p1" "$d0" "$d1" "$a0" "$a1" <<'PY'
import sys
p0, p1, d0, d1, a0, a1 = [float(x or 0) for x in sys.argv[1:7]]
if d1 > d0:
    print("  MTP acceptance: %.4f of %d draft tokens accepted (llama-bench cannot see this)"
          % ((a1 - a0) / (d1 - d0), int(d1 - d0)))
if p1 > p0:
    print("  server-side decode seconds per wall second: %.3f" % ((p1 - p0)))
PY
  } > "$f" 2>&1
  sed 's/^/  /' "$f"
}

# ------------------------------------------------------------ llama-bench ----
run_bench() { # label
  local f="$OUT/bench-$1.txt"
  [ -n "$BENCH" ] || { echo "  llama-bench not found (set LLAMA_BENCH=); probe skipped"; return 1; }
  [ -f "$MODEL" ] || { echo "  model $MODEL not found; probe skipped"; return 1; }
  echo "  running: $BENCH -pg 512,128 -m $MODEL -b 2048 -ub 512 -ngl 99 --flash-attn on --load-mode none"
  "$BENCH" -pg 512,128 -m "$MODEL" -b 2048 -ub 512 -ngl 99 --flash-attn on --load-mode none \
    > "$f" 2>&1 || { echo "  llama-bench failed, see $f"; return 1; }
  sed 's/^/    /' "$f"
}

compare_bench() {
  python3 - "$OUT/bench-down.txt" "$OUT/bench-up.txt" "$TOLERANCE" <<'PY'
import re, sys
def rows(path):
    out = {}
    try:
        lines = [l for l in open(path) if l.strip()]
    except OSError:
        return out
    for line in lines[1:]:
        cols = line.split('|')
        if len(cols) < 3:
            continue
        test = cols[-2].strip()
        m = re.search(r'([0-9.]+)\s*(\+/-\s*[0-9.]+)?\s*t/s', cols[-1])
        if m and test and test != 'test':
            out[test] = float(m.group(1))
    return out
a, b, tol = rows(sys.argv[1]), rows(sys.argv[2]), float(sys.argv[3])
if not a or not b:
    print("  INCONCLUSIVE: llama-bench output missing or unparseable"); sys.exit(1)
worst = 0.0
for test in sorted(set(a) & set(b)):
    d = (b[test] - a[test]) / a[test] * 100.0
    worst = min(worst, d)
    print("  %-16s down=%8.2f t/s  up=%8.2f t/s  %+.2f%%" % (test, a[test], b[test], d))
if worst < -tol:
    print("  FAIL: worst regression %.2f%% exceeds the %.1f%% tolerance" % (worst, tol)); sys.exit(1)
print("  PASS: worst change %.2f%% is within the %.1f%% tolerance" % (worst, tol))
PY
}

# --------------------------------------------------------------------- run ----
rc=0
if [ "$PROBE_ONLY" = 1 ]; then
  [ -n "$HTTP" ] || { echo "--http is required with --probe-only" >&2; exit 2; }
  echo "== single probe: $LABEL (stack state is whatever it is; drive it from outside)"
  snapshot_host "$LABEL"
  http_probe "$LABEL"
  echo "done ($OUT)"
  exit 0
fi
if [ "$HTTP_ONLY" = 0 ]; then
  [ -n "$BENCH" ] || echo "note: llama-bench not on PATH (LLAMA_BENCH=/path/to/llama-bench). Run this on the box."
  echo "== stack DOWN"; stack_down; snapshot_host down; run_bench down
  [ "$HTTP" ] && http_probe down
  echo "== stack UP"; stack_up; snapshot_host up; run_bench up
  [ "$HTTP" ] && http_probe up
  echo "== comparison"
  compare_bench || rc=1
else
  [ -n "$HTTP" ] || { echo "--http is required with --http-only" >&2; exit 2; }
  echo "== stack DOWN (scrape off)"; stack_down; http_probe down
  echo "== stack UP (scrape on)"; stack_up; http_probe up
fi
echo "done ($OUT)"
exit $rc
