#!/usr/bin/env bash
# loadgen.sh — send fake traffic through the router to drive bot decisions
#
# Usage:
#   ./loadgen.sh              # default: 2 req/s for 10 minutes
#   ./loadgen.sh 5 300        # 5 req/s for 300 seconds
#   ./loadgen.sh stop         # kill background load generator
#
# The script sends GET /work through the router (port 8080) so all requests
# are counted by Prometheus and visible to the bot.

set -euo pipefail

ROUTER="http://localhost:8080"
PID_FILE="/tmp/.loadgen_pid"

# ── stop ────────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "stop" ]]; then
  if [[ -f "$PID_FILE" ]]; then
    kill "$(cat "$PID_FILE")" 2>/dev/null && echo "Load generator stopped." || echo "Process already gone."
    rm -f "$PID_FILE"
  else
    echo "No load generator running."
  fi
  exit 0
fi

# ── config ───────────────────────────────────────────────────────────────────
RPS="${1:-2}"           # requests per second
DURATION="${2:-600}"    # seconds (default 10 min)
TOTAL=$(echo "$RPS * $DURATION" | bc 2>/dev/null || echo $((RPS * DURATION)))
SLEEP=$(echo "scale=4; 1/$RPS" | bc)

echo "=== Load Generator ==="
echo "  Router  : $ROUTER"
echo "  Rate    : $RPS req/s"
echo "  Duration: ${DURATION}s"
echo "  Total   : ~${TOTAL} requests"
echo "  Stop    : ./loadgen.sh stop   (or Ctrl+C)"
echo

# ── run in background ────────────────────────────────────────────────────────
(
  END_TIME=$(( $(date +%s) + DURATION ))
  OK=0; ERR=0; i=0

  while [[ $(date +%s) -lt $END_TIME ]]; do
    i=$((i+1))
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$ROUTER/work" 2>/dev/null || echo "000")
    if [[ "$STATUS" =~ ^2 ]]; then
      OK=$((OK+1))
    else
      ERR=$((ERR+1))
    fi

    # Print progress every 20 requests
    if (( i % 20 == 0 )); then
      ELAPSED=$(( $(date +%s) - (END_TIME - DURATION) ))
      REM=$(( END_TIME - $(date +%s) ))
      printf "\r  [%3ds elapsed | %3ds left]  sent=%d  ok=%d  err=%d    " \
        "$ELAPSED" "$REM" "$i" "$OK" "$ERR"
    fi

    sleep "$SLEEP"
  done

  echo ""
  echo "=== Done: $i requests sent, $OK ok, $ERR errors ==="
  rm -f "$PID_FILE"
) &

BGPID=$!
echo $BGPID > "$PID_FILE"
echo "Load generator running (PID $BGPID)"
echo "Watch the bot: http://localhost:9102"
echo "Watch traffic: http://localhost:8080/ui"
