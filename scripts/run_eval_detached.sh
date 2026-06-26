#!/usr/bin/env bash
# nohup wrapper around run_eval.sh so an eval survives SSH disconnect.

set -euo pipefail

MODEL_ID="${1:?Usage: $0 <model_id> [tp_size] [port]}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Propagate API keys (XAI, ANTHROPIC, …) into the detached child.
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

TS="$(date +%Y%m%d_%H%M%S)"
LOG="/tmp/eval_${MODEL_ID//\//_}_${TS}.log"

echo "[run_eval_detached] launching $MODEL_ID -> $LOG"
nohup ./scripts/run_eval.sh "$@" > "$LOG" 2>&1 < /dev/null &
PID=$!
disown

echo "[run_eval_detached] PID=$PID"
echo "Monitor:  tail -f $LOG"
echo "Stop:     kill $PID"
