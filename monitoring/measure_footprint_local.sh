#!/usr/bin/env bash
# The footprint measurement, without Docker.
#
#   monitoring/measure_footprint_local.sh start      # bring up serve.py + prometheus + grafana
#   monitoring/measure_footprint_local.sh measure    # the table (delegates to measure_footprint.sh)
#   monitoring/measure_footprint_local.sh stop       # tear it all down
#
# Why: `measure_footprint.sh` reads /proc and `docker stats`, so it needs the
# compose stack. The same numbers are worth having on a machine with no Docker
# daemon — a dev container, CI, or anyone running prometheus as a plain systemd
# service — and they are the only way to rehearse the measurement (and catch a
# flag the pinned binary rejects, which is how this script found
# `--no-web.enable-lifecycle`) before the box is available.
#
# It runs the SAME versions and the SAME config as compose.yaml: prometheus and
# grafana binaries (or their install dirs) are located on PATH, the prometheus
# config is monitoring/prometheus/ copied to the work dir and rewritten for a
# host-local topology, the retention/limit flags are read out of compose.yaml, and
# grafana gets the same GF_* environment compose.yaml sets.
#
# WHAT IT CANNOT REPRODUCE: the cgroup ceiling. With no daemon there is no
# mem_limit, so every number it prints is the process's *natural* demand — which
# is the right thing to compare against the limit, but is not proof the limit is
# enforced. That proof needs `monitoring/verify_limits.sh` on a Docker host.
#
#   PROM_BIN=/path/to/prometheus GRAFANA_DIR=/path/to/grafana-v11.6.1 \
#     monitoring/measure_footprint_local.sh start --upstream http://llm.home:8080
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${FOOTPRINT_WORK:-${TMPDIR:-/tmp}/llm-monitoring-footprint}"
UPSTREAM="${LLM_UPSTREAM_URL:-http://llm.home:8080}"
PROXY_PORT="${PROXY_PORT:-8090}"
PROM_PORT="${PROMETHEUS_PORT:-9090}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
WINDOW_SECONDS="${WINDOW_SECONDS:-30}"

UPSTREAM_HOSTPORT=""   # filled by resolve_binaries/config

die() { echo "error: $*" >&2; exit 1; }

locate_binaries() {
  PROM_BIN="${PROM_BIN:-$(command -v prometheus || true)}"
  [ -n "$PROM_BIN" ] || for c in /usr/local/bin/prometheus /opt/prometheus/prometheus \
      /tmp/bin/prometheus-*/prometheus; do [ -x "$c" ] && PROM_BIN="$c" && break; done
  [ -n "$PROM_BIN" ] && [ -x "$PROM_BIN" ] || die "no prometheus binary (set PROM_BIN=)"
  GRAFANA_BIN="${GRAFANA_BIN:-}"
  if [ -z "$GRAFANA_BIN" ]; then
    for c in "$(command -v grafana || true)" /usr/share/grafana/bin/grafana \
        /tmp/bin/grafana-v*/bin/grafana; do [ -x "${c:-}" ] && GRAFANA_BIN="$c" && break; done
  fi
  [ -n "$GRAFANA_BIN" ] || die "no grafana binary (set GRAFANA_BIN= or GRAFANA_DIR=)"
  GRAFANA_HOME="$(cd "$(dirname "$GRAFANA_BIN")/.." && pwd)"
  UPSTREAM_HOSTPORT="${UPSTREAM#*://}"; UPSTREAM_HOSTPORT="${UPSTREAM_HOSTPORT%%/*}"
}

# The compose flags, read from git so this can never drift from what ships.
compose_flag() { # regex -> first match
  grep -oE "$1" "$HERE/compose.yaml" | head -1 || true
}

start() {
  locate_binaries
  local retention_time retention_size
  retention_time="$(compose_flag 'storage\.tsdb\.retention\.time=\$\{[A-Z_]+:-[^}]+\}')"
  retention_time="${retention_time##*:-}"; retention_time="${retention_time%\}}"
  retention_size="$(compose_flag 'storage\.tsdb\.retention\.size=\$\{[A-Z_]+:-[^}]+\}')"
  retention_size="${retention_size##*:-}"; retention_size="${retention_size%\}}"

  mkdir -p "$WORK/tsdb" "$WORK/grafana-data/logs"
  rm -rf "$WORK/prom-conf" "$WORK/grafana-provisioning" "$WORK/grafana-data/dashboards"
  cp -r "$HERE/prometheus" "$WORK/prom-conf"
  # Two topology rewrites, and nothing else: the compose file's prometheus lives in
  # a bridge network and reaches the host through host-gateway, and its target list
  # names the production port, which is not always the port running today.
  sed -i "s/host\.docker\.internal:${PROXY_PORT}/localhost:${PROXY_PORT}/" "$WORK/prom-conf/prometheus.yml"
  # ...and the passthrough job's ?url= label, which names the production server too.
  sed -i "s#llm_upstream: \"http://[^\"]*\"#llm_upstream: \"http://$UPSTREAM_HOSTPORT\"#" "$WORK/prom-conf/prometheus.yml"
  python3 - "$WORK/prom-conf" "$UPSTREAM_HOSTPORT" <<'PY'
import json, sys
conf, hostport = sys.argv[1], sys.argv[2]
path = conf + "/targets/llm-servers.json"
entries = json.load(open(path))
for entry in entries:
    entry["targets"] = [hostport]
json.dump(entries, open(path, "w"), indent=2)
PY
  # Grafana's provisioning is written for the container: the datasource reaches
  # prometheus over the compose network name and the dashboard provider reads the
  # image's /var/lib/grafana/dashboards. Copy, rewrite the two paths that encode
  # that, and point the provider back at the same git files the container mounts.
  cp -r "$HERE/grafana/provisioning" "$WORK/grafana-provisioning"
  ln -sfn "$HERE/grafana/dashboards" "$WORK/grafana-data/dashboards"
  sed -i "s#http://prometheus:9090#http://localhost:$PROM_PORT#;
        s#/var/lib/grafana/dashboards#$WORK/grafana-data/dashboards#" \
    "$WORK/grafana-provisioning/datasources/datasource.yml" \
    "$WORK/grafana-provisioning/dashboards/dashprovider.yml"
  echo "work dir: $WORK"
  echo "scraping: $UPSTREAM_HOSTPORT (from --upstream)"
  echo "retention (from compose.yaml): time=$retention_time size=$retention_size"

  [ -f "$WORK/serve.pid" ] || {
    setsid nohup python3 "$HERE/../monitor/serve.py" --url "$UPSTREAM" --port "$PROXY_PORT" \
      --prometheus-url "http://127.0.0.1:$PROM_PORT" \
      --grafana-url "http://127.0.0.1:$GRAFANA_PORT/" > "$WORK/serve.log" 2>&1 &
    echo $! > "$WORK/serve.pid"; }
  [ -f "$WORK/prom.pid" ] || {
    setsid nohup "$PROM_BIN" \
      --config.file="$WORK/prom-conf/prometheus.yml" \
      --storage.tsdb.path="$WORK/tsdb" \
      --web.listen-address="127.0.0.1:$PROM_PORT" \
      --storage.tsdb.retention.time="$retention_time" \
      --storage.tsdb.retention.size="$retention_size" \
      --no-web.enable-lifecycle > "$WORK/prometheus.log" 2>&1 &
    echo $! > "$WORK/prom.pid"; }
  [ -f "$WORK/grafana.pid" ] || {
    # The GF_* block from compose.yaml, applied to a native process.
    ( cd "$GRAFANA_HOME" && env \
      GF_PLUGINS_ALLOW_LOADING=false \
      GF_ANALYTICS_REPORTING_ENABLED=false \
      GF_ANALYTICS_CHECK_FOR_UPDATES=false \
      GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false \
      GF_NEWSFEED_ENABLED=false \
      GF_INSTALL_PLUGINS="" \
      GF_USERS_ALLOW_SIGN_UP=false \
      GF_USERS_ALLOW_ORG_CREATE=false \
      GF_AUTH_DISABLE_LOGIN_FORM=false \
      GF_AUTH_ANONYMOUS_ENABLED=true \
      GF_SECURITY_ADMIN_USER=admin \
      GF_SECURITY_ADMIN_PASSWORD=footprint-local-not-a-secret \
      GF_SECURITY_DISABLE_GRAVATAR=true \
      GF_SERVER_HTTP_ADDR=127.0.0.1 \
      GF_SERVER_HTTP_PORT="$GRAFANA_PORT" \
      GF_SERVER_ROOT_URL="http://localhost:$GRAFANA_PORT/" \
      GF_PATHS_PROVISIONING="$WORK/grafana-provisioning" \
      GF_PATHS_DASHBOARDS="$WORK/grafana-data/dashboards" \
      GF_PATHS_DATA="$WORK/grafana-data" \
      GF_PATHS_LOGS="$WORK/grafana-data/logs" \
      GF_DASHBOARDS_MIN_REFRESH_INTERVAL=15s \
      GF_NEWSFEED_URL="" \
      setsid nohup "$GRAFANA_BIN" server > "$WORK/grafana.log" 2>&1 &
      echo $! > "$WORK/grafana.pid" )
  }
  echo "waiting for endpoints..."
  for _ in $(seq 30); do
    curl -fsS -m 2 "http://127.0.0.1:$PROM_PORT/-/ready" >/dev/null 2>&1 \
      && curl -fsS -m 2 "http://127.0.0.1:$GRAFANA_PORT/api/health" >/dev/null 2>&1 \
      && curl -fsS -m 3 "http://127.0.0.1:$PROXY_PORT/metrics" >/dev/null 2>&1 && break
    sleep 2
  done
  status
  echo
  echo "next: monitoring/measure_footprint_local.sh measure   (and: stop)"
}

status() {
  echo
  printf 'serve.py   pid=%s  %s\n' "$(cat "$WORK/serve.pid" 2>/dev/null)" \
    "$([ -d "/proc/$(cat "$WORK/serve.pid" 2>/dev/null)" ] && echo up || echo down)"
  printf 'prometheus pid=%s  %s\n' "$(cat "$WORK/prom.pid" 2>/dev/null)" \
    "$(curl -fsS -m 2 "http://127.0.0.1:$PROM_PORT/-/ready" >/dev/null 2>&1 && echo ready || echo not-ready)"
  printf 'grafana    pid=%s  %s\n' "$(cat "$WORK/grafana.pid" 2>/dev/null)" \
    "$(curl -fsS -m 2 "http://127.0.0.1:$GRAFANA_PORT/api/health" >/dev/null 2>&1 && echo ok || echo not-ready)"
  curl -fsS -m 5 "http://127.0.0.1:$PROM_PORT/api/v1/targets" 2>/dev/null |
    python3 -c '
import json, sys
try:
    ts = json.load(sys.stdin)["data"]["activeTargets"]
except Exception:
    sys.exit(0)
for t in ts:
    print("  target %-28s %-6s series=%s %s" % (t["labels"].get("job"), t["health"],
          t.get("scrapeSeries"), t.get("lastError") or ""))'
}

measure() {
  [ -d "$WORK/tsdb" ] || die "not started (no $WORK)"
  locate_binaries
  # Match on the command line rather than the recorded pid: a restarted or
  # re-parented process (setsid forks; grafana re-execs) must still be found,
  # because a footprint measured on a dead pid reads 0 MiB and looks like a win.
  TSDB_DIR="$WORK/tsdb" "$HERE/measure_footprint.sh" \
    --window "${WINDOW_SECONDS:-30}" --no-docker \
    --proc "prometheus=--config.file=$WORK/prom-conf/prometheus.yml" \
    --proc "grafana=$GRAFANA_BIN server" \
    ${_ARGS[@]+"${_ARGS[@]}"}
}

stop() {
  local f pid
  for f in serve prom grafana; do
    pid="$(cat "$WORK/$f.pid" 2>/dev/null || true)"
    if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
      # setsid made each of these a session leader, so the negative pid takes the
      # whole group (grafana forks helpers); plain kill is the fallback.
      kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
    fi
    rm -f "$WORK/$f.pid"
  done
  local pat
  for pat in "--config.file=$WORK/prom-conf/prometheus.yml" "$GRAFANA_BIN server" \
             "monitor/serve.py --url $UPSTREAM --port $PROXY_PORT"; do
    for pid in $(pids_matching "$pat" 2>/dev/null || true); do kill -TERM "$pid" 2>/dev/null; done
  done
  echo "stopped (data left in $WORK)"
}

# pgrep is not everywhere; same fallback as measure_footprint.sh's.
pids_matching() {
  if command -v pgrep >/dev/null 2>&1; then pgrep -f "$1" 2>/dev/null || true; return; fi
  local c pid
  for c in /proc/[0-9]*/cmdline; do
    pid="${c#/proc/}"; pid="${pid%/cmdline}"; [ -r "$c" ] || continue
    tr '\0' ' ' < "$c" 2>/dev/null | grep -q -e "$1" && printf '%s\n' "$pid"
  done
}

_ARGS=()
cmd="${1:-start}"; shift || true
while [ $# -gt 0 ]; do
  case "$1" in
    --upstream) UPSTREAM="$2"; shift 2 ;;
    --window) WINDOW_SECONDS="$2"; shift 2 ;;
    *) _ARGS+=("$1"); shift ;;
  esac
done
case "$cmd" in
  start) start ;;
  status) locate_binaries; status ;;
  measure) measure ;;
  stop) stop ;;
  *) sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
