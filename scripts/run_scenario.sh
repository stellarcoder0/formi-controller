#!/usr/bin/env bash
# Full local run: simulator + controller + grade.
#   scripts/run_scenario.sh scenarios/public_01_dev.yaml public_01 [port]
# Needs a baseline at runs/base_<name> (created here if missing).
set -euo pipefail
SCENARIO="$1"; NAME="$2"; PORT="${3:-8700}"
cd "$(dirname "$0")/.."
mkdir -p runs reports logs
[ -d "runs/base_$NAME" ] || python -m sim baseline --scenario "$SCENARIO" --run-dir "runs/base_$NAME"
rm -rf "state/$NAME"
python -m sim serve --scenario "$SCENARIO" --run-dir "runs/mine_$NAME" --port "$PORT" --force > "logs/sim_$NAME.log" 2>&1 &
SIM_PID=$!
trap 'kill $SIM_PID 2>/dev/null || true' EXIT
until curl -sf "http://127.0.0.1:$PORT/health" > /dev/null; do sleep 1; done
STATE_DIR="state/$NAME" ./run.sh "http://127.0.0.1:$PORT" 2>&1 | tee "logs/controller_$NAME.log"
python -m sim grade --run "runs/mine_$NAME" --baseline "runs/base_$NAME" --out "reports/$NAME.md" > /dev/null
echo "graded -> reports/$NAME.md"
