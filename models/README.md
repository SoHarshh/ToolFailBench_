# Model configs

One YAML file per model in `models/configs/<family>/<id>.yaml`, loaded by `registry.py`. To add a model, drop in a new file — no code changes needed.

## Fields

| Field | Description |
|---|---|
| `id` | Name used in `--model <id>` and result filenames |
| `hf_model_id` | Hugging Face id for open-weight models; `null` for API models |
| `family` | `qwen` / `llama` / `gemma` / `glm` / `deepseek` / `mistral` / `openai` / `anthropic` / `xai` |
| `size` | Parameter count (e.g. `32B`); `unknown` for API models |
| `tier` | Group label for batch runs (`--tier N`) |
| `category` | `base` / `instruct` / `reasoning` / `closed` |
| `inference_backend` | `vllm` (served locally) or `api` (provider API) |
| `tool_call_parser` | vLLM parser for open-weight models (e.g. `hermes`, `mistral`, `llama3_json`, `glm47`, `gemma4`) |
| `litellm_model_id` | API models: the exact provider model string litellm calls |
| `no_think` | optional: append `/no_think` to disable thinking mode |
| `recommended_gpu` | optional: GPU hint for serving |

## Example

```yaml
id: qwen2.5-32b-instruct
hf_model_id: Qwen/Qwen2.5-32B-Instruct
family: qwen
size: 32B
tier: 4
category: instruct
inference_backend: vllm
tool_call_parser: hermes
```

See the top-level README for serving open-weight models and running evaluations.
