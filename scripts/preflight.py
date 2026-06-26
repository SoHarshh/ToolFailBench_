"""Pre-run checks (env vars, model availability, and a tool-call round-trip) for a ToolFailBench model."""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from models.registry import get_model_config, get_models_for_tier, load_registry, KNOWN_VLLM_PARSERS

load_dotenv()

def _ok(msg: str) -> None:
    print(f"  [ OK ] {msg}", flush=True)

def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}", flush=True)

def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}", flush=True)

def _vllm_base_url() -> str:
    return os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1").rstrip("/")

def check_env(model_cfg: dict) -> list[str]:
    """Returns list of problem strings; empty list = pass."""
    problems = []
    if model_cfg["inference_backend"] == "vllm":
        base = _vllm_base_url()
        if not base.startswith(("http://", "https://")):
            problems.append(f"VLLM_BASE_URL={base!r} is not a valid URL")
        else:
            _ok(f"VLLM_BASE_URL={base}")
    elif model_cfg["family"] == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            problems.append("OPENAI_API_KEY is not set")
        else:
            _ok("OPENAI_API_KEY is set")
    elif model_cfg["family"] == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            problems.append("ANTHROPIC_API_KEY is not set")
        else:
            _ok("ANTHROPIC_API_KEY is set")
    return problems


def check_parser_name(model_cfg: dict) -> list[str]:
    """Non-fatal: flag unknown parser names."""
    if model_cfg["inference_backend"] != "vllm":
        return []
    parser = model_cfg.get("tool_call_parser")
    if parser and parser not in KNOWN_VLLM_PARSERS:
        _warn(
            f"tool_call_parser={parser!r} is not in stock vLLM's set. "
            f"If the server started without a plugin, the live round-trip check "
            f"below will fail."
        )
    elif parser:
        _ok(f"tool_call_parser={parser} (stock vLLM)")
    else:
        _warn("no tool_call_parser set — tool calls will NOT be extracted")
    return []


def check_model_loaded(model_cfg: dict) -> list[str]:
    """For vLLM: GET /v1/models and confirm hf_model_id is served."""
    if model_cfg["inference_backend"] != "vllm":
        return []
    import requests

    base = _vllm_base_url()
    want = model_cfg["hf_model_id"]
    try:
        r = requests.get(f"{base}/models", timeout=10)
        r.raise_for_status()
    except Exception as e:
        return [f"GET {base}/models failed: {e}"]

    served = [m.get("id") for m in r.json().get("data", [])]
    if want in served:
        _ok(f"vLLM is serving {want}")
        return []
    return [f"vLLM is reachable but not serving {want!r} (has: {served})"]


def check_tool_call_roundtrip(model_cfg: dict, verbose: bool = False) -> list[str]:
    # Checks vLLM tool-call parser by sending a dummy tool call and verifying server emits 'message.tool_calls'. Only runs for vLLM backend.
    if model_cfg["inference_backend"] != "vllm":
        _ok("tool-call round-trip: skipped (API backend)")
        return []
    import requests

    base = _vllm_base_url()
    url = f"{base}/chat/completions"
    payload = {
        "model": model_cfg["hf_model_id"],
        "messages": [
            {"role": "system", "content": "You are a helpful assistant. When the user asks about weather, call the get_weather tool."},
            {"role": "user", "content": "What's the weather in Tokyo right now?"},
        ],
        "tools": [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Return the current weather for a city.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }],
        "tool_choice": "auto",
        "temperature": 0.0,
        "max_tokens": 256,
        "seed": 42,
    }
    try:
        r = requests.post(url, json=payload, timeout=60)
        r.raise_for_status()
        resp = r.json()
    except Exception as e:
        return [f"POST {url} failed: {e}"]

    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    tool_calls = msg.get("tool_calls") or []
    content = (msg.get("content") or "").strip()

    if verbose:
        print(f"    response choice: {json.dumps(choice, indent=2)[:500]}")

    if not tool_calls:
        hint = ""
        low = content.lower()
        if "<tool_call>" in low or "<function=" in low:
            hint = " — model emitted hermes-style text in content; use --tool-call-parser hermes"
        elif "[tool_calls]" in low:
            hint = " — model emitted Mistral-style text in content; use --tool-call-parser mistral"
        elif "<|tool_call|>" in low:
            hint = " — model emitted GLM-style text in content; no stock vLLM parser for GLM-4-9B-Chat"
        elif content:
            hint = f" — model answered in content text ({len(content)} chars, preview: {content[:120]!r}) instead of calling the tool"
        return [f"server returned 0 tool_calls for a weather query that explicitly should call get_weather{hint}"]

    tc_names = [tc.get("function", {}).get("name") for tc in tool_calls]
    if "get_weather" in tc_names:
        _ok(f"tool-call round-trip: {len(tool_calls)} tool_calls returned, names={tc_names}")
        return []
    _warn(f"tool_calls populated but unexpected names {tc_names}; parser is loaded, prompt may need tuning")
    return []


def preflight(model_id: str, verbose: bool = False, check_tool_call: bool = True) -> bool:
    """Run all checks for one model; returns True on pass.

    check_tool_call=False skips the dummy tool-call round-trip — use it for a
    runner that queries the model without any tools.
    """
    print(f"\n=== preflight: {model_id} ===", flush=True)
    try:
        model_cfg = get_model_config(model_id)
    except Exception as e:
        _fail(f"registry load: {e}")
        return False

    all_problems = []
    all_problems += check_env(model_cfg)
    all_problems += check_parser_name(model_cfg)
    all_problems += check_model_loaded(model_cfg)
    if not all_problems and check_tool_call:
        # Only run the round-trip if the server is reachable — otherwise the
        # error would be noise on top of the earlier failure.
        all_problems += check_tool_call_roundtrip(model_cfg, verbose=verbose)

    for p in all_problems:
        _fail(p)

    if all_problems:
        print(f"  Result: FAIL ({len(all_problems)} problem{'s' if len(all_problems)!=1 else ''})")
        return False
    print(f"  Result: OK")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight checks for ToolFailBench")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("model_id", nargs="?", help="Single model id")
    group.add_argument("--tier", type=int, help="Run preflight for all models in this tier")
    group.add_argument("--all-vllm", action="store_true", help="Run for every vLLM-backed model in the registry")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--no-tool-call-check", action="store_true", help="Skip the tool-call round-trip (for tool-less runs)")
    args = parser.parse_args()

    if args.model_id:
        ids = [args.model_id]
    elif args.tier is not None:
        ids = [m["id"] for m in get_models_for_tier(args.tier)]
    else:  # --all-vllm
        ids = [m["id"] for m in load_registry() if m["inference_backend"] == "vllm"]

    results = {mid: preflight(mid, verbose=args.verbose, check_tool_call=not args.no_tool_call_check) for mid in ids}

    print(f"\n=== summary ===")
    for mid, ok in results.items():
        print(f"  {mid:20s} {'OK' if ok else 'FAIL'}")

    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
