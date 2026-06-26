#!/usr/bin/env bash
# Run all configured judges over one eval JSON, then aggregate to the rule + judge ensemble (Cohen's/Fleiss' kappa).

set -euo pipefail

EVAL_JSON="${1:?Usage: $0 <eval-json>}"
[ -f "$EVAL_JSON" ] || { echo "ERROR: eval JSON not found: $EVAL_JSON" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
[ -d .venv ] && source .venv/bin/activate
[ -f .env ] && { set -a; source .env; set +a; }

JUDGES="${JUDGES:-qwen35_397b glm47_fp8}"
MODEL_ID="$(python3 -c "import json; print(json.load(open('$EVAL_JSON'))[0]['model_id'])")"
JUDGE_DIR="results/v5/judge"

AGG_ARGS=(--eval "$EVAL_JSON")

for judge in $JUDGES; do
    echo "[run_judge] $judge over $EVAL_JSON"
    python scripts/run_judge.py \
        --judge-config "$judge" \
        --results-file "$EVAL_JSON"

    LATEST="$(ls -t ${JUDGE_DIR}/${MODEL_ID}_judge_${judge}_*.json 2>/dev/null | head -1)"
    [ -z "$LATEST" ] && { echo "ERROR: no judge output for $judge" >&2; exit 1; }
    AGG_ARGS+=(--judge "${judge}:${LATEST}")
done

echo "[run_judge] aggregating: ${AGG_ARGS[*]}"
python evaluation/judges/aggregate.py "${AGG_ARGS[@]}"
