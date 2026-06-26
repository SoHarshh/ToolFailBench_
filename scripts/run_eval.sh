#!/usr/bin/env bash
# Serve (if needed), preflight, and run one model through the eval pipeline.

set -euo pipefail

MODEL_ID="${1:?Usage: $0 <model_id> [tp_size] [port]}"
TP_SIZE="${2:-1}"
PORT="${3:-8000}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
[ -d .venv ] && source .venv/bin/activate

BACKEND="$(python3 - <<PY
import sys; sys.path.insert(0, '.')
from models.registry import get_model_config
print(get_model_config("$MODEL_ID")["inference_backend"])
PY
)"

SERVE_PID=""
cleanup() {
    if [ -n "$SERVE_PID" ] && kill -0 "$SERVE_PID" 2>/dev/null; then
        echo "[run_eval] cleaning up vLLM (pid $SERVE_PID)..."
        kill "$SERVE_PID" 2>/dev/null || true
        sleep 2
        kill -9 "$SERVE_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

PREFLIGHT_FLAG=""
[ "${SKIP_PREFLIGHT:-0}" = "1" ] && PREFLIGHT_FLAG="--skip-preflight"

if [ "$BACKEND" = "api" ]; then
    echo "[run_eval] $MODEL_ID is an API model — skipping serve/preflight."
    python scripts/run_eval.py --model "$MODEL_ID" $PREFLIGHT_FLAG
else
    [ -z "${CUDA_VISIBLE_DEVICES:-}" ] && {
        echo "ERROR: CUDA_VISIBLE_DEVICES must be set for vLLM models" >&2
        exit 1
    }
    echo "[run_eval] $MODEL_ID  TP=$TP_SIZE  PORT=$PORT  GPUS=$CUDA_VISIBLE_DEVICES"

    # vLLM stability env (deep_gemm warmup, concurrent-pane port collisions, gloo/nccl iface)
    export VLLM_USE_DEEP_GEMM="${VLLM_USE_DEEP_GEMM:-0}"
    export VLLM_USE_DEEP_GEMM_E8M0="${VLLM_USE_DEEP_GEMM_E8M0:-0}"
    export VLLM_DP_MASTER_IP="${VLLM_DP_MASTER_IP:-127.0.0.1}"
    export VLLM_DP_MASTER_PORT="${VLLM_DP_MASTER_PORT:-$((29500 + PORT - 8000))}"
    export VLLM_HOST_IP="${VLLM_HOST_IP:-127.0.0.1}"
    export GLOO_SOCKET_IFNAME="${GLOO_SOCKET_IFNAME:-lo}"
    export NCCL_SOCKET_IFNAME="${NCCL_SOCKET_IFNAME:-lo}"

    SERVE_LOG="/tmp/serve_${MODEL_ID//\//_}_${PORT}.log"
    : > "$SERVE_LOG"
    echo "[run_eval] serve log: $SERVE_LOG"

    VLLM_PORT="$PORT" python scripts/serve_model.py "$MODEL_ID" -- \
        --tensor-parallel-size "$TP_SIZE" \
        --max-model-len "${MAX_MODEL_LEN:-8192}" \
        --gpu-memory-utilization "${GPU_MEM_UTIL:-0.92}" \
        ${EXTRA_VLLM_ARGS:-} >> "$SERVE_LOG" 2>&1 &
    SERVE_PID=$!
    echo "[run_eval] vLLM pid=$SERVE_PID — waiting for /v1/models on :$PORT"

    SECS=0
    TIMEOUT="${SERVE_READY_TIMEOUT:-1800}"
    until curl -sf -o /dev/null "http://localhost:$PORT/v1/models" 2>/dev/null; do
        if ! kill -0 "$SERVE_PID" 2>/dev/null; then
            echo "[run_eval] vLLM died. Tail of $SERVE_LOG:" >&2
            tail -n 60 "$SERVE_LOG" >&2; exit 1
        fi
        [ "$SECS" -ge "$TIMEOUT" ] && {
            echo "[run_eval] timeout after ${SECS}s. Tail of $SERVE_LOG:" >&2
            tail -n 60 "$SERVE_LOG" >&2; exit 1
        }
        sleep 5; SECS=$((SECS + 5))
    done
    echo "[run_eval] vLLM ready after ${SECS}s."

    if [ -z "$PREFLIGHT_FLAG" ]; then
        VLLM_BASE_URL="http://localhost:$PORT/v1" python scripts/preflight.py "$MODEL_ID"
    fi

    VLLM_BASE_URL="http://localhost:$PORT/v1" \
        python scripts/run_eval.py --model "$MODEL_ID" $PREFLIGHT_FLAG
fi

LATEST_JSON="$(ls -t results/v5/${MODEL_ID}_*.json 2>/dev/null | head -1 || true)"
[ -n "$LATEST_JSON" ] && echo "DONE: $MODEL_ID -> $LATEST_JSON"
