"""Loads and validates the per-model YAML configs in models/configs/."""
import warnings
import yaml
from pathlib import Path

CONFIGS_DIR = Path(__file__).parent / "configs"

REQUIRED_FIELDS = ["id", "hf_model_id", "family", "size", "tier", "category", "inference_backend"]

# Tool-call parsers shipped by stock vLLM; an unknown value triggers a warning.
KNOWN_VLLM_PARSERS = {
    "hermes", "mistral", "llama3_json", "llama4_pythonic", "pythonic",
    "granite", "granite4", "granite-20b-fc", "internlm", "jamba", "xlam",
    "minimax_m1", "deepseek_v3", "deepseek_v31", "openai", "kimi_k2",
    "hunyuan_a13b", "longcat", "glm45", "glm47", "functiongemma",
    "qwen3_xml", "olmo3", "gigachat3", "gemma4", "qwen3_coder",
}


def _load_model_config(path: Path) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"Invalid model config at {path} — expected a YAML mapping.")
    missing = [f for f in REQUIRED_FIELDS if f not in cfg]
    if missing:
        raise ValueError(f"Model config {path.name} is missing required fields: {missing}")
    if cfg.get("inference_backend") == "vllm":
        parser = cfg.get("tool_call_parser")
        if parser and parser not in KNOWN_VLLM_PARSERS:
            warnings.warn(
                f"{path.name}: tool_call_parser={parser!r} is not a stock vLLM parser; "
                f"serving will fail unless a plugin registers it.",
                stacklevel=3,
            )
    return cfg


def load_registry() -> list[dict]:
    """Return all model configs, sorted by tier then id."""
    configs = [_load_model_config(p) for p in sorted(CONFIGS_DIR.rglob("*.yaml"))]
    if not configs:
        raise RuntimeError(f"No model configs found in {CONFIGS_DIR}")
    return sorted(configs, key=lambda m: (m["tier"], m["id"]))


def get_model_config(model_id: str) -> dict:
    for m in load_registry():
        if m["id"] == model_id:
            return m
    raise ValueError(f"Model '{model_id}' not found in registry. Check models/configs/.")


def get_models_for_tier(tier: int) -> list[dict]:
    return [m for m in load_registry() if m["tier"] == tier]
