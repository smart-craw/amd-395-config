#!/usr/bin/env bash
# Bring the monitoring stack up (and keep it up across reboots).
#
#   monitoring/up.sh              # cp .env if needed, check config, up -d
#   monitoring/up.sh --down       # stop it
#   monitoring/up.sh --purge      # stop it AND delete the TSDB volume
#
# Exists so "bring the stack up" stays one command while still doing the two
# things that are easy to forget: .env must exist (compose refuses to start
# Grafana without GRAFANA_ADMIN_PASSWORD), and the committed rules must match
# their thresholds.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$HERE/compose.yaml"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is not installed (or not in PATH)" >&2
  exit 1
fi

case "${1:-up}" in
  --down|down)
    exec docker compose -f "$COMPOSE_FILE" down
    ;;
  --purge|purge)
    echo "purging the TSDB volume: every metric older than this run is gone." >&2
    docker compose -f "$COMPOSE_FILE" down -v
    exit 0
    ;;
esac

if [ ! -f "$HERE/.env" ]; then
  cp "$HERE/.env.example" "$HERE/.env"
  echo "NOTE: created $HERE/.env from the example." >&2
  echo "      Set GRAFANA_ADMIN_PASSWORD before you expose anything." >&2
fi

python3 "$HERE/render_rules.py" --check || {
  echo "error: prometheus/rules.yml does not match alert_thresholds.env;" >&2
  echo "       run: python3 monitoring/render_rules.py" >&2
  exit 1
}

docker compose -f "$COMPOSE_FILE" config -q
docker compose -f "$COMPOSE_FILE" up -d "$@"

echo
echo "prometheus  http://127.0.0.1:${PROMETHEUS_PORT:-9090}"
echo "grafana     http://127.0.0.1:${GRAFANA_PORT:-3000}  (dashboards ▸ LLM)"
echo
echo "The model server is scraped directly; the monitor's passthrough is the"
echo "fallback. If Grafana shows No data, check 'docker compose -f $COMPOSE_FILE"
echo "ps' and the target list in prometheus/targets/llm-servers.json."
