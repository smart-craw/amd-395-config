#!/usr/bin/env bash
# Bring up a throwaway Prometheus (the pinned version, not a copy of the config),
# record paired page/Grafana samples against a live model server, probe the delta
# classes that a quiet window cannot show, and write the artifacts PARITY.md is
# built from. See monitoring/PARITY.md.
#
#   monitoring/parity/run_parity.sh                         # 1 h with light load
#   monitoring/parity/run_parity.sh --duration 7d           # the ticket's week
#   monitoring/parity/run_parity.sh --upstream http://host:8080 --no-load
#
# Needs: python3, a model server that answers /metrics, and network access the
# first time (it downloads the Prometheus version pinned in monitoring/compose.yaml
# into runs/ if prometheus is not already on PATH). Nothing it starts is a service:
# everything it starts, it kills.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
RUNS="$HERE/runs"
mkdir -p "$RUNS"

UPSTREAM="${UPSTREAM:-http://llm.home:8080}"
DURATION="1h"
LOAD=1
LOAD_INTERVAL=12
LOAD_BURST=3
PORT="${PARITY_PROM_PORT:-9090}"
FANOUT_PORT=8111
NAN_PORT=8112
RESET_PORT=8113
PROM_BIN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --upstream) UPSTREAM="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --interval) PARITY_INTERVAL="$2"; shift 2 ;;
    --no-load) LOAD=0; shift ;;
    --load-interval) LOAD_INTERVAL="$2"; shift 2 ;;
    --load-burst) LOAD_BURST="$2"; shift 2 ;;
    --prometheus-bin) PROM_BIN="$2"; shift 2 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done
PARITY_INTERVAL="${PARITY_INTERVAL:-15}"

STAMP="$(date +%Y%m%d-%H%M%S)"
RUN="$RUNS/$STAMP"
mkdir -p "$RUN/tsdb"

log() { printf '[parity] %s\n' "$*"; }
cleanup() {
  local pids
  pids="$(jobs -pr)"
  # shellcheck disable=SC2086
  for pid in $pids; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ---- the Prometheus binary, at the version production pins ------------------
if [[ -z "$PROM_BIN" ]]; then
  if command -v prometheus >/dev/null 2>&1; then
    PROM_BIN="$(command -v prometheus)"
  else
    PINNED="$(grep -oE 'prom/prometheus:v[0-9]+\.[0-9]+\.[0-9]+' "$REPO/monitoring/compose.yaml" | head -1 | cut -d: -f2)"
    PROM_DIR="$RUNS/prometheus-${PINNED}"
    if [[ ! -x "$PROM_DIR/prometheus" ]]; then
      log "downloading prometheus ${PINNED} (the version monitoring/compose.yaml pins)"
      curl -fsSL -o "$RUNS/prom.tar.gz" \
        "https://github.com/prometheus/prometheus/releases/download/v${PINNED}/prometheus-${PINNED}.linux-amd64.tar.gz"
      tar -xzf "$RUNS/prom.tar.gz" -C "$RUNS"
      mv "$RUNS/prometheus-${PINNED}.linux-amd64" "$PROM_DIR"
    fi
    PROM_BIN="$PROM_DIR/prometheus"
  fi
fi
log "prometheus: $("$PROM_BIN" --version 2>&1 | head -1)"

HOST="${UPSTREAM#*://}"; HOST="${HOST%%/*}"

# ---- the fixture doubles (label fan-out, NaN/+Inf, counter reset) -----------
python3 "$REPO/monitor/tests/mock_upstream.py" --port "$FANOUT_PORT" --mode fanout >/dev/null 2>&1 &
python3 "$REPO/monitor/tests/mock_upstream.py" --port "$NAN_PORT" --mode nan >/dev/null 2>&1 &
python3 "$REPO/monitor/tests/mock_upstream.py" --port "$RESET_PORT" --mode reset >/dev/null 2>&1 &

# ---- prometheus, rendering the parity config's placeholders -----------------
sed -e "s|@HOST@|$HOST|" -e "s|@FANOUT@|127.0.0.1:$FANOUT_PORT|" \
    -e "s|@NAN@|127.0.0.1:$NAN_PORT|" -e "s|@RESET@|127.0.0.1:$RESET_PORT|" \
    "$HERE/prometheus.parity.yml" > "$RUN/prometheus.yml"
"$PROM_BIN" --config.file="$RUN/prometheus.yml" \
            --storage.tsdb.path="$RUN/tsdb" \
            --web.listen-address="127.0.0.1:$PORT" \
            --storage.tsdb.retention.time=30d > "$RUN/prometheus.log" 2>&1 &
log "waiting for prometheus on :$PORT"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/-/ready" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/-/ready" >/dev/null || {
  log "prometheus did not come up; see $RUN/prometheus.log"; exit 1; }

# ---- optional light load -----------------------------------------------------
if [[ "$LOAD" == "1" ]]; then
  log "light load driver: ${LOAD_BURST} short completion(s) every ${LOAD_INTERVAL}s (see drive_load.py)"
  python3 "$HERE/drive_load.py" --url "$UPSTREAM" --interval "$LOAD_INTERVAL" \
      --burst "$LOAD_BURST" --duration "$DURATION" > "$RUN/load.log" 2>&1 &
fi

# ---- warm the rate windows: a rate() needs two samples to be a rate ---------
log "warming the rate windows (${PARITY_INTERVAL}s scrape interval)"
sleep "$(( PARITY_INTERVAL * 6 ))"

# ---- record ------------------------------------------------------------------
log "recording paired samples for $DURATION every ${PARITY_INTERVAL}s"
set +e
python3 "$HERE/parity_check.py" record \
  --upstream "$UPSTREAM" --prometheus "http://127.0.0.1:$PORT" \
  --interval "$PARITY_INTERVAL" --duration "$DURATION" \
  --out "$RUN/samples" --strict 2>&1 | tee "$RUN/record.log"
RECORD_RC=${PIPESTATUS[0]}
set -e

# ---- probes and report -------------------------------------------------------
python3 "$HERE/parity_check.py" probe \
  --upstream "$UPSTREAM" --prometheus "http://127.0.0.1:$PORT" \
  --fixture-fanout "http://127.0.0.1:$FANOUT_PORT" \
  --fixture-nan "http://127.0.0.1:$NAN_PORT" \
  --fixture-reset "http://127.0.0.1:$RESET_PORT" \
  --out "$RUN/probes.json" > "$RUN/probes.log" 2>&1

python3 "$HERE/parity_check.py" report "$RUN/samples.jsonl" --csv "$RUN/deltas.csv" \
  | tee "$RUN/report.txt"

cp "$HERE/prometheus.parity.yml" "$RUN/prometheus.parity.yml.used"
log "artifacts in $RUN"
log "then: python3 $HERE/parity_check.py report $RUN/samples.jsonl"
# Non-zero when the run found a delta it could not classify: that is the ticket's
# stop condition, and the caller (CI, a human, this script's exit code) should see it.
exit "$RECORD_RC"
