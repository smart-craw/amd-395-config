#!/usr/bin/env bash
# Bring the monitoring stack up (and keep it up across reboots).
#
#   monitoring/up.sh              # cp .env if needed, check config, up -d
#   monitoring/up.sh --down       # stop it
#   monitoring/up.sh --purge      # stop it AND delete the TSDB volume
#   monitoring/up.sh --lan        # up -d WITH the opt-in LAN override
#
# Exists so "bring the stack up" stays one command while still doing the two
# things that are easy to forget: .env must exist (compose refuses to start
# Grafana without GRAFANA_ADMIN_PASSWORD), and the committed rules must match
# their thresholds.
#
# --lan adds `-f monitoring/compose.lan.yaml` — i.e. it publishes Grafana on the
# LAN with anonymous read-only access. Read that file's header first: it says
# what the dashboards disclose and what the alternative (nginx + basic auth)
# costs. Not publishing anything is the default, and it is the default *here* too:
# no flag, no override.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$HERE/compose.yaml"
LAN=0
rest=()
for arg in "$@"; do
  case "$arg" in
    --lan | lan) LAN=1 ;;
    *) rest+=("$arg") ;;
  esac
done
set -- ${rest[@]+"${rest[@]}"}

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is not installed (or not in PATH)" >&2
  exit 1
fi

FILES=(-f "$COMPOSE_FILE")
if [ "$LAN" = 1 ]; then
  FILES+=(-f "$HERE/compose.lan.yaml")
  cat >&2 <<'WARN'
NOTE: --lan publishes Grafana on the LAN (anonymous Viewer, read-only).
      Anyone who can route to this box can then read model ids, request rates,
      prompt-cache behaviour and host telemetry. Prometheus stays loopback.
      Composing without the flag is the way back; see monitoring/compose.lan.yaml.
WARN
fi

case "${1:-up}" in
  --down | down)
    exec docker compose "${FILES[@]}" down
    ;;
  --purge | purge)
    echo "purging the TSDB volume: every metric older than this run is gone." >&2
    docker compose "${FILES[@]}" down -v
    exit 0
    ;;
esac

if [ ! -f "$HERE/.env" ]; then
  cp "$HERE/.env.example" "$HERE/.env"
  echo "NOTE: created $HERE/.env from the example." >&2
  echo "      Set GRAFANA_ADMIN_PASSWORD before you expose anything." >&2
fi

# Read one key out of .env the way compose would, without sourcing it (an env
# file is data, not code, and this one is operator-edited). Needed only so the
# URLs printed at the end are the ones that actually work.
envget() {
  [ -f "$HERE/.env" ] || return 0
  sed -n -E "s/^[[:space:]]*$1=//p" "$HERE/.env" | tail -n 1 |
    sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

# shell precedence over .env over the shipped default — the same order compose
# uses, so the printed URL and the published socket cannot disagree.
PROM_PORT="${PROMETHEUS_PORT:-$(envget PROMETHEUS_PORT)}"
PROM_PORT="${PROM_PORT:-9090}"
GRAF_PORT="${GRAFANA_PORT:-$(envget GRAFANA_PORT)}"
GRAF_PORT="${GRAF_PORT:-3000}"
GRAF_BIND="${GRAFANA_LAN_BIND:-$(envget GRAFANA_LAN_BIND)}"
GRAF_BIND="${GRAF_BIND:-0.0.0.0}"

python3 "$HERE/render_rules.py" --check || {
  echo "error: prometheus/rules.yml does not match alert_thresholds.env;" >&2
  echo "       run: python3 monitoring/render_rules.py" >&2
  exit 1
}

docker compose "${FILES[@]}" config -q || {
  echo "error: the compose files do not resolve (see above)." >&2
  if [ "$LAN" = 1 ]; then
    echo "       If it complained about the !override tag, your docker compose is" >&2
    echo "       older than 2.14: it merges `ports` instead of replacing them, and" >&2
    echo "       would otherwise fail with 'address already in use'." >&2
  fi
  exit 1
}
docker compose "${FILES[@]}" up -d "$@"

echo
echo "prometheus  http://127.0.0.1:${PROM_PORT}"
if [ "$LAN" = 1 ]; then
  bind="$GRAF_BIND"
  [ "$bind" = "0.0.0.0" ] && bind="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "grafana     http://${bind:-<box address>}:${GRAF_PORT}  (dashboards ▸ LLM)"
  if [ "$GRAF_BIND" = "127.0.0.1" ]; then
    echo "            not published: GRAFANA_LAN_BIND is loopback, so nginx (if any)"
    echo "            is the only door — see monitoring/nginx-grafana.conf.example"
  else
    echo "            published on the LAN: anonymous Viewer, no sign-up, no admin"
  fi
else
  echo "grafana     http://127.0.0.1:${GRAF_PORT}  (dashboards ▸ LLM)"
fi
echo
echo "The model server is scraped directly; the monitor's passthrough is the"
echo "fallback. If Grafana shows No data, check 'docker compose -f $COMPOSE_FILE"
echo "ps' and the target list in prometheus/targets/llm-servers.json."
