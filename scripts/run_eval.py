"""
Main evaluation runner for ToolFailBench.

Loads the v5 tasks, runs one or more registry models through the two-call
tool-use exchange (call → inject mock tool return → final answer), classifies
each result with the rule classifier, and writes per-model JSON to results/v5/.

Usage:
  python scripts/run_eval.py --model qwen2.5-32b-instruct
  python scripts/run_eval.py --model grok-4.3 --domains finance legal
  python scripts/run_eval.py --tier 1 2
"""
import argparse
import json
import os
import sys
import time
import traceback
import yaml
from pathlib import Path
from datetime import datetime
from tqdm import tqdm
from dotenv import load_dotenv

# Allow importing local modules from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from evaluation.detect import classify_failure_mode
from evaluation.metrics import compute_all_metrics
from evaluation.report import generate_summary_table, save_results_json
from evaluation.data import load_tasks_v5, V5_DOMAINS
from models.registry import load_registry, get_model_config, get_models_for_tier
from scripts.preflight import preflight as run_preflight  # noqa: E402

load_dotenv()

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "configs" / "default.yaml"


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path) as f:
        config = yaml.safe_load(f)
    if not isinstance(config, dict):
        raise ValueError(f"Config at {path} is empty or invalid — expected a YAML mapping.")
    if "inference" not in config:
        raise ValueError(f"Config at {path} is missing required 'inference' section.")
    required_inference_keys = ["temperature", "max_tokens", "seed", "tool_choice"]
    missing = [k for k in required_inference_keys if k not in config["inference"]]
    if missing:
        raise ValueError(f"Config 'inference' section is missing keys: {missing}")
    null_keys = [k for k in required_inference_keys if config["inference"][k] is None]
    if null_keys:
        raise ValueError(f"Config 'inference' keys must not be null: {null_keys}")
    return config


def _build_litellm_model_str(model_cfg: dict, config: dict) -> tuple[str, dict]:
    """Return (litellm_model_string, extra_kwargs) for the given model config.

    vLLM models are called via the local OpenAI-compatible server; API models go
    directly through their provider.
    """
    extra = {}

    if model_cfg["inference_backend"] == "vllm":
        base_url = (
            config.get("vllm", {}).get("base_url")
            or os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1")
        )
        # litellm uses "openai/<model_name>" for custom OpenAI-compatible endpoints
        litellm_model = f"openai/{model_cfg['hf_model_id']}"
        extra["api_base"] = base_url
        extra["api_key"] = "vllm"  # vLLM ignores the key but litellm requires one

    elif model_cfg["inference_backend"] == "api":
        # Prefer an explicit litellm_model_id; otherwise fall back to family conventions.
        if model_cfg.get("litellm_model_id"):
            litellm_model = model_cfg["litellm_model_id"]
        elif model_cfg["family"] in ("openai", "anthropic"):
            litellm_model = model_cfg["id"]
        elif model_cfg["family"] == "deepseek":
            litellm_model = f"deepseek/{model_cfg['id']}"
        elif model_cfg["family"] == "xai":
            litellm_model = f"xai/{model_cfg['id']}"
        else:
            raise ValueError(
                f"family={model_cfg['family']!r} on API model {model_cfg['id']} "
                "has no default litellm route — set litellm_model_id in the YAML."
            )

    else:
        raise ValueError(f"Unknown inference backend for model: {model_cfg['id']}")

    return litellm_model, extra


def _build_tools_payload(task: dict) -> list[dict]:
    """v5 task tools are already in OpenAI function-calling form; pass them through."""
    return task.get("available_tools") or []


def _raw_http_call(messages, tools, inf, base_url, model_id):
    """Raw HTTP call bypassing client-side pydantic validation.

    Some vLLM tool-call parsers (e.g. mistral) return arguments as a dict instead
    of a JSON string, which breaks both the litellm and openai clients.
    """
    import requests

    url = f"{base_url}/chat/completions"
    payload = {
        "model": model_id,
        "messages": messages,
        "tools": tools,
        "tool_choice": inf["tool_choice"],
        "temperature": inf["temperature"],
        "max_tokens": inf["max_tokens"],
        "seed": inf["seed"],
    }
    resp = requests.post(url, json=payload, timeout=120)
    if not resp.ok:
        body = (resp.text or "")[:2000]
        raise requests.exceptions.HTTPError(
            f"{resp.status_code} {resp.reason} for {url} — body: {body}",
            response=resp,
        )
    return resp.json()


def _parse_tool_calls_from_dict(choice_dict: dict) -> tuple[list[dict], list[dict]]:
    """Extract tool calls from a raw JSON response choice dict.
    Returns (parsed_tool_calls, raw_tool_calls_for_followup)."""
    tool_calls = []
    raw_tcs = []
    msg = choice_dict.get("message", {})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, str):
            args = json.loads(args)
        tool_calls.append({"name": fn.get("name", ""), "arguments": args})
        raw_tcs.append(tc)
    return tool_calls, raw_tcs


def _parse_tool_calls(choice) -> tuple[list[dict], None]:
    """Extract tool calls from a litellm/openai response choice object."""
    tool_calls = []
    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            args = tc.function.arguments
            if isinstance(args, str):
                args = json.loads(args)
            tool_calls.append({"name": tc.function.name, "arguments": args})
    return tool_calls, None


def _extract_raw_response(response) -> dict:
    """Extract key fields from a litellm response for archival."""
    try:
        choice = response.choices[0]
        raw = {
            "content": choice.message.content,
            "finish_reason": choice.finish_reason,
            "tool_calls": None,
        }
        if choice.message.tool_calls:
            raw["tool_calls"] = [
                {
                    "id": tc.id,
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in choice.message.tool_calls
            ]
        if hasattr(response, "usage") and response.usage:
            raw["usage"] = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        return raw
    except Exception:
        return {"error": "failed to extract raw response"}


# Exception markers that indicate a transient error worth retrying (connection
# resets, rate limits, short timeouts). Real logic bugs should not be retried.
_TRANSIENT_ERROR_MARKERS = (
    "RateLimitError", "APIConnectionError", "APITimeoutError",
    "ReadTimeout", "ConnectionError", "ServiceUnavailable",
    "ServerError", "503", "502", "504", "timeout",
)


def _is_transient(exc: Exception) -> bool:
    s = f"{type(exc).__name__}:{exc}"
    return any(m.lower() in s.lower() for m in _TRANSIENT_ERROR_MARKERS)


def _with_retry(fn, *, max_attempts: int = 3, base_delay: float = 2.0):
    """Run `fn()` with exponential backoff on transient errors only."""
    last = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            last = e
            if attempt == max_attempts or not _is_transient(e):
                raise
            delay = base_delay * (2 ** (attempt - 1))
            print(f"    [retry {attempt}/{max_attempts-1}] {type(e).__name__}: {e!s:.120s} — sleeping {delay:.1f}s", flush=True)
            time.sleep(delay)
    raise last  # unreachable


def run_single_task(task: dict, model_cfg: dict, config: dict) -> dict:
    """Run one task against a model. Returns result dict."""
    try:
        import litellm

        # Anthropic's API doesn't accept `seed`; litellm raises UnsupportedParamsError
        # unless we tell it to drop provider-unsupported params. This is the litellm-
        # documented fix and only drops params the provider rejects (vLLM + OpenAI keep seed).
        litellm.drop_params = True

        litellm_model, extra_kwargs = _build_litellm_model_str(model_cfg, config)
        inf = config.get("inference", {})
        tools = _build_tools_payload(task)

        system_prompt = task["system_prompt"]
        if model_cfg.get("no_think"):
            system_prompt = system_prompt + " /no_think"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task["user_message"]},
        ]

        call_kwargs = dict(
            model=litellm_model,
            messages=messages,
            tools=tools,
            tool_choice=inf["tool_choice"],
            temperature=inf["temperature"],
            max_tokens=inf["max_tokens"],
            seed=inf["seed"],
            **extra_kwargs,
        )

        # Try litellm first; fall back to raw HTTP if response parsing fails
        use_raw = False
        tool_calls = []
        raw_tcs = []
        agent_answer = ""
        raw_responses = []  # archive all raw API responses

        try:
            response = _with_retry(lambda: litellm.completion(**call_kwargs))
            choice = response.choices[0]
            tool_calls, _ = _parse_tool_calls(choice)
            raw_responses.append({"step": "initial", **_extract_raw_response(response)})
        except Exception as litellm_err:
            err_str = str(litellm_err)
            if "FunctionCall" in err_str or "arguments" in err_str or "validation error" in err_str:
                base_url = extra_kwargs.get("api_base", "http://localhost:8000/v1")
                resp_json = _with_retry(lambda: _raw_http_call(messages, tools, inf, base_url, model_cfg["hf_model_id"]))
                choice_dict = resp_json["choices"][0]
                tool_calls, raw_tcs = _parse_tool_calls_from_dict(choice_dict)
                use_raw = True
                raw_responses.append({"step": "initial_raw", "response": choice_dict})
            else:
                raise

        if tool_calls:
            # Build follow-up messages with mock tool return injected
            if use_raw:
                # Build assistant message from raw dict
                msg_dict = resp_json["choices"][0].get("message", {})
                assistant_msg = {"role": "assistant", "content": msg_dict.get("content") or ""}
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.get("id", f"call_{i}"),
                        "type": "function",
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"] if isinstance(tc["function"]["arguments"], str)
                            else json.dumps(tc["function"]["arguments"]),
                        },
                    }
                    for i, tc in enumerate(raw_tcs)
                ]
                tool_messages = messages + [assistant_msg]
                for tc in raw_tcs:
                    tool_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", f"call_0"),
                        "content": json.dumps(task["mock_tool_return"]),
                    })
                base_url = extra_kwargs.get("api_base", "http://localhost:8000/v1")
                followup_inf = {**inf, "tool_choice": "none"}
                follow_json = _raw_http_call(tool_messages, tools, followup_inf, base_url, model_cfg["hf_model_id"])
                agent_answer = follow_json["choices"][0].get("message", {}).get("content") or ""
                raw_responses.append({"step": "followup_raw", "response": follow_json["choices"][0]})
            else:
                tool_messages = messages + [choice.message]
                for tc in choice.message.tool_calls:
                    tool_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(task["mock_tool_return"]),
                    })
                follow_up = _with_retry(lambda: litellm.completion(
                    model=litellm_model,
                    messages=tool_messages,
                    tools=tools,
                    tool_choice="none",
                    temperature=inf["temperature"],
                    max_tokens=inf["max_tokens"],
                    seed=inf["seed"],
                    **extra_kwargs,
                ))
                agent_answer = follow_up.choices[0].message.content or ""
                raw_responses.append({"step": "followup", **_extract_raw_response(follow_up)})
        else:
            if use_raw:
                agent_answer = resp_json["choices"][0].get("message", {}).get("content") or ""
            else:
                agent_answer = choice.message.content or ""

        agent_trace = {"tool_calls": tool_calls}
        classification = classify_failure_mode(task, agent_trace, agent_answer)

        return {
            "task": task,
            "model_id": model_cfg["id"],
            "agent_trace": agent_trace,
            "agent_answer": agent_answer,
            "classification": classification,
            "raw_responses": raw_responses,
        }

    except Exception as e:
        # Record the exception (type, message, traceback) on the result rather than
        # silently collapsing it to "other_error", so failures stay auditable.
        err_detail = {
            "type": type(e).__name__,
            "message": str(e),
            "traceback": traceback.format_exc(),
            "transient": _is_transient(e),
        }
        return {
            "task": task,
            "model_id": model_cfg["id"],
            "agent_trace": {"tool_calls": []},
            "agent_answer": f"ERROR: {type(e).__name__}: {str(e)[:500]}",
            "classification": "other_error",
            "raw_responses": [],
            "error": err_detail,
        }


def run_model(model_cfg: dict, tasks: list[dict], config: dict, output_dir: str, skip_preflight: bool = False):
    print(f"\n{'='*60}")
    print(f"  Model: {model_cfg['id']} (Tier {model_cfg['tier']}, {model_cfg['category']})")
    print(f"{'='*60}")

    if not skip_preflight:
        if not run_preflight(model_cfg["id"]):
            raise RuntimeError(
                f"Preflight failed for {model_cfg['id']}. See stderr above. Fix the "
                "underlying issue (tool-call parser, API key, or server state), or re-run "
                "with --skip-preflight if you've already verified the round-trip by hand."
            )

    results = []
    for task in tqdm(tasks, desc=model_cfg["id"]):
        results.append(run_single_task(task, model_cfg, config))

    print("\n" + generate_summary_table(results, model_cfg["id"]))

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"{output_dir}/{model_cfg['id']}_{timestamp}.json"
    save_results_json(results, out_path)
    print(f"  Results saved to {out_path}")
    return results


def main():
    parser = argparse.ArgumentParser(description="Run ToolFailBench evaluation")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--model", help="Model id from the registry (e.g. qwen2.5-32b-instruct)")
    group.add_argument("--tier", nargs="+", type=int, help="Run all models in tier(s) (e.g. --tier 1 2)")

    parser.add_argument("--domains", nargs="+", default=None,
                        help=f"Domains to run (default: all five — {', '.join(V5_DOMAINS)}).")
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--max-tasks", type=int, default=None, help="Cap the number of tasks (for quick smoke tests).")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Path to YAML config")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="Skip the pre-run parser/env/round-trip check. Only use if you've verified by hand.")
    args = parser.parse_args()

    config = load_config(Path(args.config))

    if args.model:
        models_to_run = [get_model_config(args.model)]
    else:
        models_to_run = []
        for tier in args.tier:
            tier_models = get_models_for_tier(tier)
            if not tier_models:
                print(f"  Warning: no models found for tier {tier}")
            models_to_run.extend(tier_models)

    print(f"Models to run: {[m['id'] for m in models_to_run]}")

    domains = args.domains if args.domains is not None else V5_DOMAINS
    print(f"Loading tasks for domains: {domains}")
    tasks = load_tasks_v5(domains)
    if args.max_tasks:
        tasks = tasks[:args.max_tasks]
    print(f"Total tasks: {len(tasks)}")

    output_dir = str(Path(args.output_dir) / "v5")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    all_results = {}
    for model_cfg in models_to_run:
        results = run_model(model_cfg, tasks, config, output_dir, skip_preflight=args.skip_preflight)
        all_results[model_cfg["id"]] = results

    if len(models_to_run) > 1:
        print(f"\n{'='*60}")
        print(f"  Completed {len(models_to_run)} models")
        for model_id, results in all_results.items():
            metrics = compute_all_metrics(results)
            print(f"  {model_id}: CTUR={metrics['ctur']:.2%} TSR={metrics['tsr']:.2%} RIR={metrics['rir']:.2%} OFR={metrics['ofr']:.2%}")


if __name__ == "__main__":
    main()
