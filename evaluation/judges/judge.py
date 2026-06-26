"""
LLM-as-judge evaluation for ToolFailBench.

Semantic evaluation of model responses, complementing the rule-based substring
matching in detect.py. Judge identity, sampling parameters, prompt variant, and
endpoint are all driven by the YAML configs under evaluation/judges/configs/.

Usage (as a library):
    from evaluation.judges.judge import load_judge_config, run_judge_on_result
    cfg = load_judge_config("qwen35_397b")
    verdict = run_judge_on_result(result_dict, cfg)
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

JUDGES_ROOT = Path(__file__).resolve().parent
CONFIGS_DIR = JUDGES_ROOT / "configs"
PROMPTS_DIR = JUDGES_ROOT / "prompts"


@dataclass
class JudgeConfig:
    """Loaded view of a judge YAML, including the assembled system prompt."""
    judge_name: str
    hf_id: str
    served_name: str
    api_base: str
    api_key: str
    temperature: float
    max_tokens: int
    num_retries: int
    timeout: int
    extra_body: dict
    system_prompt: str  # base_rubric.md + variant overlay
    raw: dict = field(default_factory=dict)

    @property
    def litellm_string(self) -> str:
        """litellm route for an OpenAI-compatible vLLM endpoint."""
        return f"openai/{self.served_name}"


def _resolve_env(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val


def load_judge_config(name_or_path: str) -> JudgeConfig:
    """Load a judge YAML by bare name ("qwen35_397b") or path.

    Resolves api_base / api_key from the env vars named in the YAML, and
    concatenates the base rubric + variant overlay into system_prompt.
    """
    p = Path(name_or_path)
    if not p.exists():
        p = CONFIGS_DIR / f"{name_or_path}.yaml"
    if not p.exists():
        raise FileNotFoundError(
            f"Judge config not found: {name_or_path!r} (looked at {p}). "
            f"Available: {sorted(c.stem for c in CONFIGS_DIR.glob('*.yaml'))}"
        )

    with open(p) as f:
        raw = yaml.safe_load(f)

    model = raw["model"]
    endpoint = raw["endpoint"]
    inference = raw["inference"]
    prompt = raw["prompt"]

    api_base = _resolve_env(endpoint["api_base_env"])
    if not api_base:
        raise RuntimeError(
            f"Judge {raw['judge_name']!r} requires env var {endpoint['api_base_env']!r} to be set "
            f"(e.g. https://your-endpoint/v1). Did you source .env / deploy the judge endpoint?"
        )
    api_key = _resolve_env(endpoint["api_key_env"], default="EMPTY")

    base_rubric = (PROMPTS_DIR / prompt["base_rubric"]).read_text()
    variant = (PROMPTS_DIR / prompt["variant"]).read_text()
    system_prompt = base_rubric.rstrip() + "\n\n" + variant.lstrip()

    return JudgeConfig(
        judge_name=raw["judge_name"],
        hf_id=model["hf_id"],
        served_name=model["served_name"],
        api_base=api_base,
        api_key=api_key,
        temperature=float(inference["temperature"]),
        max_tokens=int(inference["max_tokens"]),
        num_retries=int(inference.get("num_retries", 10)),
        timeout=int(inference.get("timeout", 90)),
        extra_body=dict(inference.get("extra_body") or {}),
        system_prompt=system_prompt,
        raw=raw,
    )


def list_judge_configs() -> list[str]:
    """All judge config names available in the configs dir."""
    return sorted(c.stem for c in CONFIGS_DIR.glob("*.yaml"))


def _clean_answer(text: str) -> str:
    """Strip model-specific special tokens (think blocks, channel markers) from the answer."""
    text = re.sub(r'<\|channel>thought\s*\n?<channel\|>', '', text)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'</think>\s*', '', text)
    return text.strip()


def build_judge_prompt(result: dict) -> str:
    """Build the judge's user message for a single eval result.

    Includes the task, tools, model response, and ground truth, but deliberately
    omits the rule-based classification so the judge is not biased by it.
    """
    task = result["task"]
    trace = result["agent_trace"]
    answer = _clean_answer(result["agent_answer"])

    is_ctrl = not task["evaluation_criteria"]["tool_must_be_called"]
    task_type = "CONTROL (no tool needed)" if is_ctrl else "TOOL-REQUIRED"

    tool_calls = trace.get("tool_calls", [])
    if tool_calls:
        tool_calls_str = "\n".join(f"  {tc['name']}({json.dumps(tc['arguments'])})" for tc in tool_calls)
    else:
        tool_calls_str = "  (none — no tool was called)"

    # Tools use the OpenAI shape: {"type": "function", "function": {name, description, ...}}.
    def _tool_meta(t):
        f = t.get("function", t)
        return f.get("name", "?"), f.get("description", "")

    tools_str = "\n".join(f"  • {n}: {d}" for n, d in (_tool_meta(t) for t in task["available_tools"]))

    sections = [
        f"TASK TYPE: {task_type}",
        f"DOMAIN: {task['domain']}",
        f"USER MESSAGE:\n{task['user_message']}",
        f"AVAILABLE TOOLS:\n{tools_str}",
    ]
    if not is_ctrl:
        exp = task["expected_tool_call"]
        sections.append(f"EXPECTED TOOL CALL:\n  {exp['name']}({json.dumps(exp['arguments'])})")
        sections.append(
            f"TOOL RETURN VALUE (what the tool would return):\n{json.dumps(task['mock_tool_return'], indent=2)}"
        )
    sections.append(f"MODEL'S ACTUAL TOOL CALLS:\n{tool_calls_str}")
    sections.append(f"MODEL'S FINAL ANSWER:\n{answer if answer else '(empty — model returned no text)'}")

    gt = task["ground_truth"]
    gt_str = f"Must contain: {gt['answer_must_contain']}"
    if gt.get("answer_must_not_contain"):
        gt_str += f"\nMust NOT contain: {gt['answer_must_not_contain']}"
    gt_str += f"\nMatch mode: {gt.get('match_mode', 'all')}"
    sections.append(f"GROUND TRUTH:\n{gt_str}")

    return "\n\n".join(sections)


def parse_judge_response(response_text: str) -> Optional[dict]:
    """Parse the judge's JSON verdict; return the dict, or None if it fails validation."""
    text = response_text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*\n?', '', text)
        text = re.sub(r'\n?```\s*$', '', text)
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            return None

    if "failure_mode" not in data:
        return None
    valid_modes = {"correct", "tool_skip", "result_ignore", "output_fabrication",
                   "unnecessary_tool_use", "wrong_answer"}
    if data["failure_mode"] not in valid_modes:
        return None
    if "reasoning" not in data:
        data["reasoning"] = ""
    for key in ("tool_selection", "result_faithfulness", "answer_correctness"):
        if key in data and isinstance(data[key], (int, float)):
            data[key] = max(0, min(3, int(data[key])))
    return data


def run_judge_on_result(result: dict, cfg: JudgeConfig) -> dict:
    """Run the LLM judge on a single eval result.

    Sampling, endpoint, and prompt assembly are all driven by cfg. Returns the
    parsed verdict (failure_mode, scores, reasoning), or a dict with an "error" key.
    """
    import litellm

    user_prompt = build_judge_prompt(result)
    kwargs = {
        "model": cfg.litellm_string,
        "messages": [
            {"role": "system", "content": cfg.system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "timeout": cfg.timeout,
        "num_retries": cfg.num_retries,
        "api_base": cfg.api_base,
        "api_key": cfg.api_key,
    }
    if cfg.extra_body:
        kwargs["extra_body"] = cfg.extra_body

    try:
        response = litellm.completion(**kwargs)
        text = response.choices[0].message.content or ""
        parsed = parse_judge_response(text)
        if parsed is None:
            return {"error": "parse_failed", "raw_response": text, "failure_mode": None}
        parsed["raw_response"] = text
        return parsed
    except Exception as e:
        return {"error": str(e), "raw_response": None, "failure_mode": None}


def compare_classifications(results_with_judge: list[dict]) -> dict:
    """Compare rule-based vs judge labels; return agreement counts + disagreement details."""
    total = 0
    agree = 0
    disagree_list = []
    for entry in results_with_judge:
        judge = entry.get("judge", {})
        if judge.get("failure_mode") is None:
            continue
        total += 1
        rb = entry["rule_based_classification"]
        jm = judge["failure_mode"]
        if rb == jm:
            agree += 1
        else:
            disagree_list.append({
                "task_id": entry["task_id"],
                "domain": entry.get("domain", ""),
                "rule_based": rb,
                "judge": jm,
                "judge_confidence": judge.get("confidence", ""),
                "reasoning": judge.get("reasoning", ""),
            })
    return {
        "total_judged": total,
        "agreements": agree,
        "disagreements": len(disagree_list),
        "agreement_rate": agree / total if total > 0 else 0.0,
        "disagreement_details": disagree_list,
    }
