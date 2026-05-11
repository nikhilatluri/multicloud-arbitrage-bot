#!/usr/bin/env bash
# run_local.sh — start all multicloud-arbitrage-bot services locally (no Docker)
#
# Usage:
#   ./run_local.sh          # start everything
#   ./run_local.sh stop     # kill background services (reads .local_pids)
#
# Ports:
#   workload-a   : 8001
#   workload-b   : 8002
#   router       : 8080
#   pricefeed    : 7070
#   bot metrics  : 9101
#   bot REST API : 9102
#   prometheus   : 9090  (if installed)

set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
SERVICES="$REPO/services"
BACKENDS_CONFIG="$REPO/backends.local.json"
LOGS_DIR="$REPO/.local_logs"
PID_FILE="$REPO/.local_pids"

PYTHON="${PYTHON:-python3}"

# ── stop ────────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "stop" ]]; then
  if [[ -f "$PID_FILE" ]]; then
    echo "Stopping services..."
    while IFS= read -r pid; do
      kill "$pid" 2>/dev/null && echo "  killed $pid" || true
    done < "$PID_FILE"
    rm -f "$PID_FILE"
    echo "Done."
  else
    echo "No .local_pids file found — nothing to stop."
  fi
  exit 0
fi

# ── start ────────────────────────────────────────────────────────────────────
mkdir -p "$LOGS_DIR"
> "$PID_FILE"
# Truncate logs so each run starts clean (no stale errors from old runs)
for _log in "$LOGS_DIR"/*.log; do > "$_log" 2>/dev/null || true; done

trap 'echo; echo "Shutting down..."; kill $(cat "$PID_FILE" 2>/dev/null) 2>/dev/null; rm -f "$PID_FILE"; echo "Done."; exit 0' INT TERM

wait_for_port() {
  local name="$1" port="$2" tries=0
  printf "  Waiting for %s (:%s) " "$name" "$port"
  until nc -z 127.0.0.1 "$port" 2>/dev/null; do
    sleep 0.4
    tries=$((tries+1))
    if [[ $tries -gt 50 ]]; then
      echo " TIMEOUT — check $LOGS_DIR/$name.log"
      return 1
    fi
    printf "."
  done
  echo " ready"
}

start_uvicorn() {
  local name="$1" appmod="$2" port="$3"
  shift 3
  local extra_env=("$@")
  echo "Starting $name on :$port ..."
  (
    cd "$SERVICES/$name"
    # Prepend shared dir so config.py is importable in dev layout
    export PYTHONPATH="$SERVICES/shared:${PYTHONPATH:-}"
    for e in "${extra_env[@]}"; do export "$e"; done
    $PYTHON -m uvicorn "$appmod" --host 0.0.0.0 --port "$port" --log-level warning \
      >> "$LOGS_DIR/$name.log" 2>&1
  ) &
  echo $! >> "$PID_FILE"
}

echo "=== multicloud-arbitrage-bot (local) ==="
echo "Logs → $LOGS_DIR/"
echo

# ── workload-a ────────────────────────────────────────────────────────────────
start_uvicorn workload app:app 8001 \
  "CLOUD_ID=a" "BASE_LATENCY_MS=80" "JITTER_MS=30" "ERROR_RATE=0.01" "PAYLOAD_KB=32"
wait_for_port workload-a 8001

# ── workload-b ────────────────────────────────────────────────────────────────
start_uvicorn workload app:app 8002 \
  "CLOUD_ID=b" "BASE_LATENCY_MS=120" "JITTER_MS=40" "ERROR_RATE=0.005" "PAYLOAD_KB=64"
wait_for_port workload-b 8002

# ── pricefeed ─────────────────────────────────────────────────────────────────
echo "Starting pricefeed on :7070 ..."
(
  cd "$SERVICES/pricefeed"
  export PYTHONPATH="$SERVICES/shared:${PYTHONPATH:-}"
  export BACKENDS_CONFIG_PATH="$BACKENDS_CONFIG"
  $PYTHON -m uvicorn app:app --host 0.0.0.0 --port 7070 --log-level warning \
    >> "$LOGS_DIR/pricefeed.log" 2>&1
) &
echo $! >> "$PID_FILE"
wait_for_port pricefeed 7070

# ── router ────────────────────────────────────────────────────────────────────
echo "Starting router on :8080 ..."
(
  cd "$SERVICES/router"
  export PYTHONPATH="$SERVICES/shared:${PYTHONPATH:-}"
  export BACKENDS_CONFIG_PATH="$BACKENDS_CONFIG"
  export ROUTER_ADMIN_TOKEN="${ROUTER_ADMIN_TOKEN:-changeme}"
  export CB_FAIL_THRESHOLD=5
  export CB_OPEN_SECONDS=20
  export SHADOW_PERCENT=0.05
  export STICKY_HEADER="X-Sticky-Key"
  $PYTHON -m uvicorn app:app --host 0.0.0.0 --port 8080 --log-level warning \
    >> "$LOGS_DIR/router.log" 2>&1
) &
echo $! >> "$PID_FILE"
wait_for_port router 8080

# ── prometheus (must start before bot so bot can query it immediately) ────────
PROM_BIN="$(which prometheus 2>/dev/null || ls /opt/homebrew/bin/prometheus 2>/dev/null || echo "")"
if [[ -n "$PROM_BIN" ]]; then
  echo "Starting prometheus on :9090 ..."
  (
    "$PROM_BIN" \
      --config.file="$REPO/prometheus/prometheus.local.yml" \
      --storage.tsdb.path="$REPO/.local_prometheus_data" \
      --web.listen-address="0.0.0.0:9090" \
      >> "$LOGS_DIR/prometheus.log" 2>&1
  ) &
  echo $! >> "$PID_FILE"
  wait_for_port prometheus 9090
else
  echo "  (prometheus not found — skipping; bot will retry until Prometheus is available)"
fi

# ── bot ───────────────────────────────────────────────────────────────────────
echo "Starting bot (metrics :9101, API :9102) ..."
(
  cd "$SERVICES/bot"
  export PYTHONPATH="$SERVICES/shared:${PYTHONPATH:-}"
  export BACKENDS_CONFIG_PATH="$BACKENDS_CONFIG"
  export PROM_URL="http://localhost:9090"
  export ROUTER_URL="http://localhost:8080"
  export ROUTER_ADMIN_TOKEN="${ROUTER_ADMIN_TOKEN:-changeme}"
  export PRICEFEED_URL="http://localhost:7070"
  export DECISION_INTERVAL_SECONDS=15
  export COOLDOWN_SECONDS=60
  export WINDOW=2m
  export MAX_P95_MS=200
  export MAX_ERROR_RATE=0.02
  export MIN_TOTAL_RPS=0.5
  export MIN_SAVINGS_PER_HOUR=0.001
  export SWITCHING_PENALTY_USD=0.0002
  export RAMP_STEPS="0.10,0.25,0.50,1.00"
  export RAMP_STEP_MIN_SECONDS=45
  export REQUIRED_GOOD_WINDOWS=3
  export REQUIRED_BAD_WINDOWS=2
  export EG_COST_MODE=project_full_traffic
  export DB_PATH="$REPO/.local_bot.db"
  export BOT_METRICS_PORT=9101
  export POST_FAILOVER_HOLD_SECONDS=300
  export POST_FAILOVER_CONFIRM_WINDOWS=5
  export DRAIN_STEPS=4
  export DRAIN_STEP_SECONDS=10
  export ALERT_WEBHOOK_URL="${ALERT_WEBHOOK_URL:-}"
  export ALERT_ON_FAILOVER=true
  export ALERT_ON_ROLLBACK=true
  export ALERT_ON_BAD_WINDOWS=true
  export BOT_INSTANCE_NAME=multicloud-bot-local
  export BOT_API_PORT=9102
  export BOT_API_TOKEN="${BOT_API_TOKEN:-}"
  $PYTHON bot.py >> "$LOGS_DIR/bot.log" 2>&1
) &
echo $! >> "$PID_FILE"
wait_for_port bot-metrics 9101
wait_for_port bot-api 9102

# ── grafana ───────────────────────────────────────────────────────────────────
GRAFANA_BIN="$(which grafana-server 2>/dev/null || ls /opt/homebrew/bin/grafana 2>/dev/null || echo "")"
GRAFANA_INI="/opt/homebrew/etc/grafana/grafana.ini"
GRAFANA_HOME="/opt/homebrew/opt/grafana/share/grafana"
if [[ -n "$GRAFANA_BIN" && -f "$GRAFANA_INI" ]]; then
  # Write local provisioning (datasource → localhost:9090, dashboards → repo path)
  LOCAL_PROV="$REPO/.local_grafana_provisioning"
  mkdir -p "$LOCAL_PROV/datasources" "$LOCAL_PROV/dashboards"

  cat > "$LOCAL_PROV/datasources/prometheus.yml" <<DATASOURCE
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://localhost:9090
    isDefault: true
    editable: true
DATASOURCE

  cat > "$LOCAL_PROV/dashboards/dashboards.yml" <<DASHPROV
apiVersion: 1
providers:
  - name: "default"
    orgId: 1
    folder: ""
    type: file
    disableDeletion: false
    editable: true
    options:
      path: $REPO/grafana/provisioning/dashboards
DASHPROV

  echo "Starting grafana on :3000 ..."
  (
    export GF_SECURITY_ADMIN_PASSWORD="${GF_ADMIN_PASSWORD:-admin}"
    export GF_AUTH_ANONYMOUS_ENABLED=false
    export GF_PATHS_PROVISIONING="$LOCAL_PROV"
    "$GRAFANA_BIN" server \
      --config "$GRAFANA_INI" \
      --homepath "$GRAFANA_HOME" \
      cfg:default.paths.logs="$LOGS_DIR" \
      cfg:default.paths.data="$REPO/.local_grafana_data" \
      cfg:default.paths.plugins="$REPO/.local_grafana_plugins" \
      >> "$LOGS_DIR/grafana.log" 2>&1
  ) &
  echo $! >> "$PID_FILE"
  wait_for_port grafana 3000
else
  echo "  (grafana not found — skipping)"
fi

echo
echo "=== All services up ==="
echo "  workload-a      http://localhost:8001/health"
echo "  workload-b      http://localhost:8002/health"
echo "  router          http://localhost:8080/target"
echo "  pricefeed       http://localhost:7070/price"
echo "  bot metrics     http://localhost:9101/metrics"
echo "  bot REST API    http://localhost:9102/status"
[[ -n "$PROM_BIN" ]] && echo "  prometheus      http://localhost:9090"
[[ -n "$GRAFANA_BIN" ]] && echo "  grafana         http://localhost:3000  (admin / ${GF_ADMIN_PASSWORD:-admin})"
echo
echo "Press Ctrl+C to stop all services."
echo

# Keep script alive so trap fires on Ctrl+C
wait
