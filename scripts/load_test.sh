#!/usr/bin/env bash
# Load test: one simulated day of public_03_load (12 workflows, ~650k dials/day) + 1 day of history.
#   scripts/load_test.sh [scenario] [port]
# Prints wall-clock for the run and the loop's own per-day metrics (python -m controller.audit health).
set -euo pipefail
SCENARIO="${1:-scripts/load/public_03_load_1day.yaml}"; PORT="${2:-8705}"; NAME=load
cd "$(dirname "$0")/.."
mkdir -p logs runs
rm -rf "state/$NAME"
python -m sim serve --scenario "$SCENARIO" --run-dir "runs/mine_$NAME" --port "$PORT" --force > "logs/sim_$NAME.log" 2>&1 &
SIM_PID=$!
trap 'kill $SIM_PID 2>/dev/null || true' EXIT
until curl -sf "http://127.0.0.1:$PORT/health" > /dev/null; do sleep 1; done
echo "simulator ready: $(tail -1 logs/sim_$NAME.log)"
START=$(date +%s)
STATE_DIR="state/$NAME" /usr/bin/time -l ./run.sh "http://127.0.0.1:$PORT" > "logs/controller_$NAME.log" 2> "logs/time_$NAME.txt" \
  || STATE_DIR="state/$NAME" /usr/bin/time -v ./run.sh "http://127.0.0.1:$PORT" > "logs/controller_$NAME.log" 2> "logs/time_$NAME.txt"
END=$(date +%s)
echo "controller wall-clock: $((END - START)) s (includes the simulator's own time to simulate the day)"
grep -E "maximum resident|Maximum resident" "logs/time_$NAME.txt" || true
python -m controller.audit --db "state/$NAME/$(ls state/$NAME | grep sqlite$ | head -1)" health
