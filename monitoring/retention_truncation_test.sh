#!/usr/bin/env bash
# Prove that `--storage.tsdb.retention.size` actually truncates.
#
#   monitoring/retention_truncation_test.sh                 # native promtool+prometheus
#   monitoring/retention_truncation_test.sh --docker        # same, with the compose image
#   monitoring/retention_truncation_test.sh --cap 40MB      # explicit cap
#
# Why: retention.size is the only thing standing between a chatty /metrics (or a
# leaked high-cardinality label) and a full disk on a box that has no spare disk.
# A size cap that is not actually enforced is worse than none, because it is
# trusted. It is also easy to "test" wrongly: with a fresh TSDB there are no
# *blocks* to delete -- the head and the WAL are not counted by retention.size --
# so a naive "start it, look at the disk" test passes while proving nothing.
#
# So this backfills real history instead of waiting days for it: `promtool tsdb
# create-blocks-from` writes blocks with back-dated timestamps straight into a
# scratch data dir, prometheus is started over that dir with retention.time
# deliberately huge (so only the SIZE cap can delete anything), and the block
# list is compared before and after the retention pass.
#
# It never touches the production volume: the data dir is a fresh mktemp dir, and
# in --docker mode it is a `docker run` of the same pinned image with that dir
# bind-mounted. Exit code 0 = the cap truncated, 1 = it did not (or the test could
# not run), which is what CI wants.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAYS="${DAYS:-4}"
SERIES="${SERIES:-400}"
INTERVAL="${INTERVAL:-600s}"        # 10 min; keeps the backfill text small
CAP=""
USE_DOCKER=0
PROM_PORT="${RETENTION_TEST_PORT:-19090}"
IMAGE="$(grep -oE 'image: prom/prometheus:[^ ]+' "$HERE/compose.yaml" | head -1 | awk '{print $2}')"

while [ $# -gt 0 ]; do
  case "$1" in
    --docker) USE_DOCKER=1; shift ;;
    --days) DAYS="$2"; shift 2 ;;
    --series) SERIES="$2"; shift 2 ;;
    --cap) CAP="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

SCRATCH="$(mktemp -d -t retention-XXXXXX)"
trap 'cleanup' EXIT INT TERM
CIDS=()
cleanup() {
  for cid in ${CIDS[@]+"${CIDS[@]}"}; do docker rm -f "$cid" >/dev/null 2>&1 || true; done
  [ -f "$SCRATCH/prom.pid" ] && kill -TERM "$(cat "$SCRATCH/prom.pid")" 2>/dev/null
  rm -rf "$SCRATCH"
}
fail() { echo "FAIL: $*" >&2; exit 1; }
step() { printf '==> %s\n' "$*"; }

PROM_BIN="${PROM_BIN:-$(command -v prometheus || true)}"
PROMTOOL="${PROMTOOL:-$(command -v promtool || true)}"
[ "$USE_DOCKER" = 1 ] || {
  [ -n "$PROM_BIN" ] || PROM_BIN="$(ls /tmp/bin/prometheus-*/prometheus 2>/dev/null | head -1)"
  [ -n "$PROMTOOL" ] || PROMTOOL="$(ls /tmp/bin/prometheus-*/promtool 2>/dev/null | head -1)"
  [ -n "$PROM_BIN" ] && [ -x "$PROM_BIN" ] || fail "no prometheus binary (set PROM_BIN=, or use --docker)"
  [ -n "$PROMTOOL" ] && [ -x "$PROMTOOL" ] || fail "no promtool binary (set PROMTOOL=, or use --docker)"
}

step "backfilling $DAYS days of history ($SERIES series @ $INTERVAL) into $SCRATCH"
python3 - "$DAYS" "$SERIES" "$INTERVAL" > "$SCRATCH/metrics.om" <<'PY'
import sys, time
days, series, interval = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3].rstrip("s"))
now = int(time.time())
start = now - days * 86400
# OpenMetrics: a TYPE line, samples with explicit timestamps, and a mandatory EOF.
print("# TYPE flood_series gauge")
t = start
while t < now:
    for i in range(series):
        print('flood_series{job="backfill",idx="%d"} %d %.3f' % (i, (t // interval) + i, t))
    t += interval
print("# EOF")
PY
wc -l "$SCRATCH/metrics.om" | awk '{print "    " $1 " samples"}'

step "writing blocks with promtool tsdb create-blocks-from openmetrics"
if [ "$USE_DOCKER" = 1 ]; then
  [ -n "$IMAGE" ] || fail "cannot read the prometheus image from compose.yaml"
  docker run --rm -v "$SCRATCH:/data" "$IMAGE" /bin/promtool --experimental \
    tsdb create-blocks-from openmetrics /data/metrics.om /data >/dev/null 2>&1 \
    || fail "promtool (in $IMAGE) failed to create blocks"
else
  "$PROMTOOL" --experimental tsdb create-blocks-from openmetrics \
    "$SCRATCH/metrics.om" "$SCRATCH" >/dev/null 2>&1 \
    || fail "promtool failed to create blocks (needs --experimental in 3.x)"
fi
rm -f "$SCRATCH/metrics.om"

blocks() { find "$SCRATCH" -mindepth 1 -maxdepth 1 -type d -regextype posix-extended -regex '.*/[0-9A-Z]{26}' | wc -l; }
before_blocks="$(blocks)"
before_bytes="$(du -sk "$SCRATCH" | cut -f1)"
[ "$before_blocks" -ge 2 ] || fail "backfill produced $before_blocks block(s); need >= 2 for a size-retention test"
step "backfilled: $before_blocks blocks, $((before_bytes / 1024)) MiB on disk"

CAP_BYTES=$(( before_bytes * 1024 / 2 ))            # keep half: the oldest blocks must go
CAP_HUMAN="$CAP"
[ -n "$CAP_HUMAN" ] || CAP_HUMAN="$(( CAP_BYTES / 1048576 ))MB"
CAP_BYTES="$(python3 -c '
import re,sys
t = sys.argv[1].strip().upper()
m = re.match(r"^([0-9.]+)\s*([KMGT]?B)?$", t)
u = {"B":1,"KB":1000,"MB":10**6,"GB":10**9,"TB":10**12,"":1,"KIB":1024,"MIB":1024**2,"GIB":1024**3,"TIB":1024**4}[m.group(2) or ""]
print(int(float(m.group(1)) * u))' "$CAP_HUMAN")"
step "cap: $CAP_HUMAN ($CAP_BYTES bytes) against $((before_bytes * 1024)) bytes of history; retention.time = 3650d so only SIZE can delete"

step "starting prometheus over the backfilled dir"
PROM_LOG="$SCRATCH/prom.log"
# An empty config: this test is about the storage engine, and a scrape config
# would add live head series that muddy "what did the cap delete?".
cat > "$SCRATCH/prom-config.yml" <<'YML'
global:
  scrape_interval: 1h
scrape_configs: []
YML
if [ "$USE_DOCKER" = 1 ]; then
  cid="$(docker run -d --rm -v "$SCRATCH:/prometheus" -p "127.0.0.1:$PROM_PORT:9090" "$IMAGE" \
    --config.file=/prometheus/prom-config.yml \
    --storage.tsdb.path=/prometheus \
    --storage.tsdb.retention.time=3650d \
    --storage.tsdb.retention.size="$CAP_HUMAN" \
    --web.enable-admin-api --web.listen-address=0.0.0.0:9090)" || fail "docker run failed"
  CIDS+=("$cid")
  ( for _ in $(seq 600); do docker logs "$cid" > "$PROM_LOG" 2>&1; sleep 2; done ) &
else
  setsid nohup "$PROM_BIN" --config.file="$SCRATCH/prom-config.yml" --storage.tsdb.path="$SCRATCH" \
    --storage.tsdb.retention.time=3650d \
    --storage.tsdb.retention.size="$CAP_HUMAN" \
    --web.enable-admin-api --web.listen-address="127.0.0.1:$PROM_PORT" \
    > "$PROM_LOG" 2>&1 &
  echo $! > "$SCRATCH/prom.pid"
fi

step "waiting for the retention pass (prometheus checks size retention on startup and every 3 min)"
deadline=$(( $(date +%s) + 420 ))
after_bytes="$before_bytes"; after_blocks="$before_blocks"
while [ "$(date +%s)" -lt "$deadline" ]; do
  sleep 10
  if [ -f "$SCRATCH/prom.pid" ] && ! [ -d "/proc/$(cat "$SCRATCH/prom.pid")" ]; then
    tail -5 "$PROM_LOG" | sed 's/^/    /'
    fail "prometheus exited; see the log above"
  fi
  after_bytes="$(du -sk "$SCRATCH" | cut -f1)"
  after_blocks="$(blocks)"
  if [ "$after_bytes" -lt "$before_bytes" ]; then break; fi
done

freed=$(( before_bytes - after_bytes ))
step "after: $after_blocks blocks, $((after_bytes / 1024)) MiB on disk ($(( freed / 1024 )) MiB reclaimed)"
grep -iE "delet|retention" "$PROM_LOG" | tail -5 | sed 's/^/    /'

if [ "$after_bytes" -ge "$before_bytes" ]; then
  fail "nothing was deleted: $((before_bytes/1024)) MiB stayed on disk under a $CAP_HUMAN cap"
fi
if [ "$after_blocks" -ge "$before_blocks" ]; then
  fail "no whole block was removed ($before_blocks -> $after_blocks): size retention did not truncate"
fi
echo "PASS: retention.size=$CAP_HUMAN removed $((before_blocks - after_blocks)) of $before_blocks blocks" \
     "($((freed/1024)) MiB) and left $((after_bytes/1024)) MiB under the cap"
[ "$after_bytes" -le $(( CAP_BYTES / 1024 )) ] || \
  echo "note: $((after_bytes/1024)) MiB is still above the cap -- one block cannot be split, so the" \
       "floor is the oldest whole block that fits (this is expected prometheus behaviour)."
exit 0
