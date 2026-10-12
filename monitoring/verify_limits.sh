#!/usr/bin/env bash
# Verify the resource limits in monitoring/compose.yaml are actually enforced.
#
#   monitoring/verify_limits.sh              # read-only: what does the daemon really apply?
#   monitoring/verify_limits.sh --overrun    # deliberately push prometheus past its ceiling
#
# `mem_limit` in a compose file is a *claim*. Two things can quietly make it a
# false claim, and neither shows up on a dashboard:
#
#   * the .env override never made it into the container (someone sets
#     PROMETHEUS_MEM_LIMIT in a shell instead of monitoring/.env, or edits the
#     compose file and forgets `docker compose up -d`), so the running container
#     carries the old number; and
#   * `memswap_limit` is left above `mem_limit`, which turns the ceiling into a
#     soft one and moves the overflow into swap -- on this box, straight into the
#     page cache the model weights are starved of (root README: amdgpu.gttsize /
#     ttm.pages_limit).
#
# So: read what the daemon applied (`docker inspect`), read what it reports
# (`docker stats`), read the kernel's own number (`memory.max` under the
# container's cgroup), and then -- with --overrun -- drive real memory demand at
# the container until the ceiling bites, and show the OOM kill.
#
# --overrun deliberately OOM-kills prometheus. History survives (the WAL replays;
# this is the same restart the ceiling will cause on its own one day, which is
# exactly why it is worth watching once on purpose). It feeds the demand through
# prometheus' own file_sd target list -- the only knob that takes effect without
# a restart, since the config is mounted read-only and the reload endpoint is
# off -- and it restores that file from git on the way out, including on Ctrl-C.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROM_PORT="${PROMETHEUS_PORT:-9090}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
PROM_MEM_DEFAULT_MIB=512
GRAFANA_MEM_DEFAULT_MIB=256
OVERRUN=0
SERIES="${FLOOD_SERIES:-400000}"
HISTOGRAMS="${FLOOD_HISTOGRAMS:-2000}"
MAX_MINUTES="${OVERRUN_MAX_MINUTES:-12}"
FLOOD_PORT="${FLOOD_PORT:-9199}"

while [ $# -gt 0 ]; do
  case "$1" in
    --overrun) OVERRUN=1; shift ;;
    --series) SERIES="$2"; shift 2 ;;
    --minutes) MAX_MINUTES="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v docker >/dev/null 2>&1 || { echo "error: docker not found (this check runs on the box)" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found" >&2; exit 1; }

fails=0
ok()   { printf '  ok   %s\n' "$*"; }
bad()  { printf '  FAIL %s\n' "$*"; fails=$((fails + 1)); }
info() { printf '       %s\n' "$*"; }
env_mib() { # KEY DEFAULT -> MiB, read from monitoring/.env the same way compose does
  local key="$1" def="$2" v=""
  for f in "$HERE/.env" "$HERE/.env.example"; do
    [ -f "$f" ] || continue
    v="$(grep -E "^${key}=" "$f" | tail -1 | cut -d= -f2- | tr -d '"' || true)"
    [ -n "$v" ] && break
  done
  [ -n "$v" ] || v="${def}m"
  python3 -c '
import re,sys
t = sys.argv[1].strip().upper()
m = re.match(r"^([0-9.]+)\s*([KMGT]?I?B)?$", t)
u = {"":1,"B":1,"K":"1024","M":"1048576","G":"1073741824","T":"1099511627776",
     "KB":1000,"MB":1000**2,"GB":1000**3,"KIB":1024,"MIB":1024**2,"GIB":1024**3}[m.group(2) or ""]
print(int(float(m.group(1)) * float(u) / 1048576))' "$v"
}

mem_mib() { docker inspect -f '{{.HostConfig.Memory}}' "$1" | awk '{printf "%d", $1/1048576}'; }
swap_mib() { docker inspect -f '{{.HostConfig.MemorySwap}}' "$1" | awk '{printf "%d", $1/1048576}'; }
# `docker stats` prints "25.3MiB / 512MiB"; take the used side, in MiB.
stats_used_mib() {
  docker stats --no-stream --format '{{.MemUsage}}' "$1" 2>/dev/null | cut -d/ -f1 |
    python3 -c '
import re, sys
UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
         "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4, "": 1}
m = re.match(r"\s*([0-9.]+)\s*([KMGT]?i?B)?", sys.stdin.read())
print(int(float(m.group(1)) * UNITS.get(m.group(2) or "", 1) / 1048576) if m else 0)' 2>/dev/null || echo 0
}

inspect_one() { # name expected_mib
  local name="$1" want="$2"
  echo
  echo "== $name"
  if ! docker inspect "$name" >/dev/null 2>&1; then
    bad "$name is not running (\`docker compose -f monitoring/compose.yaml up -d\`)"
    return
  fi
  local mem swap cpus pids restart
  mem="$(mem_mib "$name")"; swap="$(swap_mib "$name")"
  cpus="$(docker inspect -f '{{.HostConfig.NanoCpus}}' "$name" | awk '{printf "%.2f", $1/1e9}')"
  pids="$(docker inspect -f '{{.HostConfig.PidsLimit}}' "$name")"
  restart="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$name")"
  info "memory.max=${mem} MiB  memory.memsw.max=${swap} MiB  cpu.max=${cpus} cores  pids.max=${pids}  restart=${restart}"
  [ "$mem" = "$want" ] && ok "mem_limit ${mem} MiB matches the budget (${want} MiB)" \
    || bad "mem_limit is ${mem} MiB, expected ${want} MiB (set PROMETHEUS_MEM_LIMIT/GRAFANA_MEM_LIMIT in monitoring/.env and re-run 'docker compose up -d')"
  if [ "$swap" = "$mem" ]; then ok "memswap_limit == mem_limit: no swap escape into the model's page cache"
  else bad "memswap_limit (${swap} MiB) != mem_limit (${mem} MiB): overflow goes to swap, i.e. off the box's RAM budget"; fi
  [ "$restart" = "unless-stopped" ] && ok "restart=unless-stopped (an OOM-killed ceiling self-heals)" \
    || bad "restart policy is '$restart'"

  # docker stats is the operator's view; keep it next to the kernel's.
  docker stats --no-stream --format 'stats {{.Name}}: mem={{.MemUsage}} ({{.MemPerc}}) cpu={{.CPUPerc}} pids={{.PIDs}}' "$name" |
    sed 's/^/       /'

  # The kernel's own numbers, if this host uses cgroup v2 under systemd (it does).
  local cid cg base
  cid="$(docker inspect -f '{{.Id}}' "$name")"
  for base in /sys/fs/cgroup/system.slice /sys/fs/cgroup; do
    cg="$base/docker-$cid.scope"
    [ -r "$cg/memory.max" ] && { info "kernel cgroup: memory.max=$(cat "$cg/memory.max") memory.current=$(cat "$cg/memory.current") ($(cat "$cg/memory.stat" 2>/dev/null | awk '/^file /{print "file="$2}') $(cat "$cg/memory.stat" 2>/dev/null | awk '/^anon /{print "anon="$2}'))"; break; }
  done
  [ -n "${cg:-}" ] && [ -r "$cg/memory.max" ] || info "(cgroup files not readable here; docker stats is the next best thing)"
}

want_prom="$(env_mib PROMETHEUS_MEM_LIMIT $PROM_MEM_DEFAULT_MIB)"
want_graf="$(env_mib GRAFANA_MEM_LIMIT $GRAFANA_MEM_DEFAULT_MIB)"
echo "verifying limits against the budget in monitoring/compose.yaml (prometheus ${want_prom} MiB, grafana ${want_graf} MiB)"
inspect_one llm-prometheus "$want_prom"
inspect_one llm-grafana "$want_graf"

if [ "$OVERRUN" = 0 ]; then
  echo
  echo "read-only checks done. To prove the ceiling bites: $0 --overrun"
  [ "$fails" = 0 ] && exit 0 || exit 1
fi

# --------------------------------------------------------------------------- #
echo
echo "== deliberate overrun: feeding prometheus ~$SERIES synthetic series until the ceiling bites"
echo "   (this is expected to OOM-kill llm-prometheus; it restarts by itself)"

TARGETS="$HERE/prometheus/targets/llm-servers.json"
BACKUP="$(mktemp -t llm-servers.json.XXXXXX)"
cp "$TARGETS" "$BACKUP"
cleanup() {
  cp "$BACKUP" "$TARGETS"
  [ -n "${FLOOD_PID:-}" ] && kill -TERM -"$FLOOD_PID" 2>/dev/null
  if diff -q "$BACKUP" "$TARGETS" >/dev/null 2>&1; then
    echo "   (target list restored)"
  else
    echo "   RESTORE FAILED -- run: git checkout monitoring/prometheus/targets/llm-servers.json"
  fi
  rm -f "$BACKUP"
}
trap cleanup EXIT INT TERM

setsid nohup python3 "$HERE/flood_target.py" --port "$FLOOD_PORT" --series "$SERIES" \
  --histograms "$HISTOGRAMS" > /tmp/flood_target.log 2>&1 &
FLOOD_PID=$!
sleep 2
cat /tmp/flood_target.log

python3 - "$TARGETS" "host.docker.internal:$FLOOD_PORT" <<'PY'
import json, sys
path, target = sys.argv[1], sys.argv[2]
entries = json.load(open(path))
entries.append({"targets": [target], "labels": {"model": "flood", "env": "limit-test"}})
json.dump(entries, open(path, "w"), indent=2)
print("   target list now also includes", target, "-- file_sd picks it up within 60s")
PY

limit="$(mem_mib llm-prometheus)"
restart0="$(docker inspect -f '{{.RestartCount}}' llm-prometheus)"
peak=0
deadline=$(( $(date +%s) + MAX_MINUTES * 60 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  sleep 15
  used="$(stats_used_mib llm-prometheus)"
  peak=$(( used > peak ? used : peak ))
  series="$(curl -fsS -m 5 --get "http://127.0.0.1:$PROM_PORT/api/v1/query" \
      --data-urlencode 'query=count({__name__!=""})' 2>/dev/null |
    python3 -c 'import json,sys;r=json.load(sys.stdin)["data"]["result"];print(r[0]["value"][1] if r else "?")' 2>/dev/null)"
  restart="$(docker inspect -f '{{.RestartCount}}' llm-prometheus)"
  oom="$(docker inspect -f '{{.State.OOMKilled}}' llm-prometheus)"
  printf '       mem=%s/%s MiB (peak %s) series=%s restarts=%s oomkilled=%s\n' \
    "$used" "$limit" "$peak" "${series:-?}" "$restart" "$oom"
  if [ "$restart" != "$restart0" ] || [ "$oom" = "true" ]; then
    echo
    echo "PASS: the ceiling bit -- llm-prometheus was OOM-killed at ~${limit} MiB (peak seen ${peak} MiB)"
    echo "      after $series series; it restarted (RestartCount $restart0 -> $restart) and the"
    echo "      box kept serving. That is the behaviour the budget depends on."
    [ "$fails" = 0 ] && exit 0 || exit 1
  fi
done
echo
echo "INCONCLUSIVE: after ${MAX_MINUTES} min the container peaked at ${peak} MiB of ${limit} MiB"
echo "            without being OOM-killed. Either it fits comfortably (good) or the ceiling is"
echo "            not being applied (bad) -- re-run the read-only checks above and raise --series."
exit 1
