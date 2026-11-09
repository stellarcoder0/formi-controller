#!/usr/bin/env bash
# Starts the controller against a running simulator and runs until the scenario finishes.
#   ./run.sh http://127.0.0.1:8700
# Needs Python 3.10+ with requirements.txt installed (only httpx is used by the controller).
# State lives in ./state/<scenario>.sqlite; re-running after a crash resumes from it.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
exec python -m controller --url "$SIM_URL" --state-dir "${STATE_DIR:-state}"
