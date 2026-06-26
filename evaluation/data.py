"""
Task loading for ToolFailBench (5 domains: finance, medical, legal, cybersecurity, real_estate).

Each task in tasks_v5/<domain>/tasks.json carries per-task data (user_message,
mock_tool_return, ground_truth, metadata). The shared system prompt lives at
system_prompts/v5/<domain>.md (YAML frontmatter is stripped), and the tool
schemas at tasks_v5/<domain>/tools.json in OpenAI function-calling format.
load_tasks_v5() attaches these plus an evaluation_criteria.tool_must_be_called
flag derived from target_failure_mode.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
V5_TASKS_DIR = ROOT / "tasks_v5"
V5_PROMPTS_DIR = ROOT / "system_prompts" / "v5"
V5_DOMAINS = ["finance", "medical", "legal", "cybersecurity", "real_estate"]

_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", flags=re.DOTALL)


def _strip_yaml_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block (--- ... ---); only the body is the system prompt."""
    if text.startswith("---"):
        m = _FRONTMATTER_RE.match(text)
        if m:
            return text[m.end():].strip()
    return text.strip()


def _load_v5_system_prompt(domain: str) -> str:
    path = V5_PROMPTS_DIR / f"{domain}.md"
    if not path.exists():
        raise FileNotFoundError(f"system prompt not found: {path}")
    return _strip_yaml_frontmatter(path.read_text())


def _load_v5_tools(domain: str) -> list[dict]:
    path = V5_TASKS_DIR / domain / "tools.json"
    if not path.exists():
        raise FileNotFoundError(f"tools spec not found: {path}")
    with open(path) as f:
        data = json.load(f)
    tools = data.get("tools")
    if not tools:
        raise ValueError(f"tools spec at {path} has no 'tools' array")
    for i, t in enumerate(tools):
        if not (isinstance(t, dict) and t.get("type") == "function" and "function" in t):
            raise ValueError(f"tool entry {i} in {path} is not in OpenAI function-calling format")
    return tools


def load_tasks_v5(domains: list[str] = None) -> list[dict]:
    """Load tasks across one or more domains (default: all five).

    Each returned task is augmented with system_prompt, available_tools, and
    evaluation_criteria.tool_must_be_called (False for control tasks, else True).
    """
    if domains is None:
        domains = V5_DOMAINS
    invalid = [d for d in domains if d not in V5_DOMAINS]
    if invalid:
        raise ValueError(f"Unknown domain(s): {invalid}; choose from {V5_DOMAINS}")

    all_tasks = []
    for domain in domains:
        sys_prompt = _load_v5_system_prompt(domain)
        tools = _load_v5_tools(domain)
        tasks_path = V5_TASKS_DIR / domain / "tasks.json"
        if not tasks_path.exists():
            raise FileNotFoundError(f"tasks not found: {tasks_path}")
        with open(tasks_path) as f:
            domain_data = json.load(f)
        domain_tasks = domain_data.get("tasks") or []
        for t in domain_tasks:
            t["system_prompt"] = sys_prompt
            t["available_tools"] = tools
            t["evaluation_criteria"] = {
                "tool_must_be_called": t["target_failure_mode"] != "control",
                "answer_must_match_format": False,
            }
            all_tasks.append(t)
        print(f"  Loaded {len(domain_tasks)} tasks from {domain}")
    return all_tasks
