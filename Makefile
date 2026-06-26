# ToolFailBench workflows. Run `make help` for the available targets.

MODEL ?=
TP    ?= 1
PORT  ?= 8000
EVAL  ?=

.PHONY: help setup serve preflight eval eval-detached judge models results

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-18s\033[0m %s\n", $$1, $$2}'

setup: ## Bootstrap venv + dependencies
	./setup.sh

serve: ## Serve a model via vLLM (MODEL=<id> [PORT=8000])
	@test -n "$(MODEL)" || (echo "ERROR: set MODEL=<id>" >&2; exit 1)
	VLLM_PORT=$(PORT) python scripts/serve_model.py $(MODEL)

preflight: ## Tool-call round-trip check (MODEL=<id>)
	@test -n "$(MODEL)" || (echo "ERROR: set MODEL=<id>" >&2; exit 1)
	python scripts/preflight.py $(MODEL)

eval: ## Full eval pipeline (MODEL=<id> [TP=1 PORT=8000])
	@test -n "$(MODEL)" || (echo "ERROR: set MODEL=<id>" >&2; exit 1)
	./scripts/run_eval.sh $(MODEL) $(TP) $(PORT)

eval-detached: ## Same as `eval` but nohup'd (MODEL=<id> [TP=1 PORT=8000])
	@test -n "$(MODEL)" || (echo "ERROR: set MODEL=<id>" >&2; exit 1)
	./scripts/run_eval_detached.sh $(MODEL) $(TP) $(PORT)

judge: ## Run all judges + aggregate kappa (EVAL=results/v5/<model>_<ts>.json)
	@test -n "$(EVAL)" || (echo "ERROR: set EVAL=<eval-json>" >&2; exit 1)
	./scripts/run_judge.sh $(EVAL)

models: ## List registered model ids
	@python3 -c "from models.registry import load_registry; [print(' ', m['id']) for m in load_registry()]"

results: ## Show v5 result JSONs (model -> latest file)
	@ls -t results/v5/*.json 2>/dev/null | awk -F/ '{print $$NF}' | \
	  awk -F'_2' 'BEGIN{seen[""]=1} !seen[$$1]++ {printf "  %s\n", $$0}'
