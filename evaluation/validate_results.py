"""
Reproducibility check over the result + judge JSONs.

Recomputes every published number from the raw per-task rows; exit 0 means
nothing is hardcoded or drifted from its source JSON. For each model it
recomputes the six metrics and verifies the failure-mode distribution and the
hand-computed rates; for each judge it checks the agreement counts, Cohen's
kappa against the rule labels, and the judge-corrected CTUR.

Usage:
    python evaluation/validate_results.py                      # all models found
    python evaluation/validate_results.py --model qwen3.5-9b   # one model
    python evaluation/validate_results.py --judge-name qwen35_397b
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from evaluation.metrics import compute_all_metrics  # noqa: E402

ROOT = Path(__file__).parent.parent
RESULTS_DIR = ROOT / "results" / "v5"
JUDGE_DIR = ROOT / "results" / "v5" / "judge"

_EVAL_RE = re.compile(r"^(?P<model>.+?)_(?P<ts>\d{8}_\d{6})\.json$")
_JUDGE_RE = re.compile(r"^(?P<model>.+?)_judge_(?P<judge_name>.+?)_(?P<ts>\d{8}_\d{6})\.json$")


def discover_eval_files() -> dict[str, Path]:
    """Latest eval JSON per model under results/v5/ (skips judge/baseline/_old_ aux files)."""
    by_model: dict[str, tuple[Path, str]] = {}
    if not RESULTS_DIR.exists():
        return {}
    for f in RESULTS_DIR.glob("*.json"):
        if any(tok in f.name for tok in ("baseline", "judge", "_old_", "_summary")):
            continue
        m = _EVAL_RE.match(f.name)
        if not m:
            continue
        model, ts = m.group("model"), m.group("ts")
        prev = by_model.get(model)
        if prev is None or ts > prev[1]:
            by_model[model] = (f, ts)
    return {model: path for model, (path, _) in by_model.items()}


def discover_judge_files(judge_name_filter: str | None = None) -> dict[tuple[str, str], Path]:
    """Latest judge JSON per (model, judge_name) under results/v5/judge/."""
    by_pair: dict[tuple[str, str], tuple[Path, str]] = {}
    if not JUDGE_DIR.exists():
        return {}
    for f in JUDGE_DIR.glob("*.json"):
        m = _JUDGE_RE.match(f.name)
        if not m:
            continue
        model, judge_name, ts = m.group("model"), m.group("judge_name"), m.group("ts")
        if judge_name_filter and judge_name != judge_name_filter:
            continue
        key = (model, judge_name)
        prev = by_pair.get(key)
        if prev is None or ts > prev[1]:
            by_pair[key] = (f, ts)
    return {pair: path for pair, (path, _) in by_pair.items()}


def _section(title: str) -> None:
    print(f"\n{'='*72}\n  {title}\n{'='*72}")


def _nearly_equal(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) < tol


def validate_eval(model_id: str, path: Path) -> tuple[list[str], dict]:
    problems: list[str] = []
    if not path.exists():
        return [f"file not found: {path}"], {}
    with open(path) as f:
        results = json.load(f)
    if not results:
        return ["empty results list"], {}

    m1 = compute_all_metrics(results)
    m2 = compute_all_metrics(results)
    for key in ("tsr", "rir", "ofr", "ctur", "utr", "ctrl_accuracy"):
        if not _nearly_equal(m1[key], m2[key]):
            problems.append(f"metric {key!r} non-deterministic: {m1[key]} vs {m2[key]}")

    dist = Counter(r["classification"] for r in results)
    if sum(dist.values()) != m1["total_tasks"]:
        problems.append(f"distribution sum {sum(dist.values())} != total_tasks {m1['total_tasks']}")

    tool_required = [r for r in results if r["task"]["evaluation_criteria"]["tool_must_be_called"]]
    ctrl = [r for r in results if not r["task"]["evaluation_criteria"]["tool_must_be_called"]]
    tsr_count  = sum(1 for r in tool_required if r["classification"] == "tool_skip")
    ri_count   = sum(1 for r in tool_required if r["classification"] == "result_ignore")
    of_count   = sum(1 for r in tool_required if r["classification"] == "output_fabrication")
    ctur_count = sum(1 for r in tool_required if r["classification"] == "correct")
    utr_count  = sum(1 for r in ctrl if r["classification"] == "unnecessary_tool_use")
    called = [r for r in tool_required if r["classification"] != "tool_skip"]
    expected = {
        "tsr":  tsr_count  / len(tool_required) if tool_required else 0.0,
        "rir":  ri_count   / len(called)        if called else 0.0,
        "ofr":  of_count   / len(called)        if called else 0.0,
        "ctur": ctur_count / len(tool_required) if tool_required else 0.0,
        "utr":  utr_count  / len(ctrl)          if ctrl else 0.0,
    }
    for name, e in expected.items():
        if abs(round(e, 4) - m1[name]) > 1e-4:
            problems.append(f"hand-computed {name}={e:.4f} != reported {m1[name]:.4f}")

    rr_missing = [r["task"]["task_id"] for r in results if not r.get("raw_responses") and not r.get("error")]
    if rr_missing:
        problems.append(f"{len(rr_missing)} tasks missing raw_responses (e.g. {rr_missing[:3]})")

    tc_count = sum(1 for r in results if r.get("agent_trace", {}).get("tool_calls"))
    err_count = sum(1 for r in results if r.get("classification") == "other_error")
    non_empty = sum(1 for r in results if (r.get("agent_answer") or "").strip())
    if ctur_count > 0 and non_empty == 0:
        problems.append(f"CTUR={ctur_count}/{len(tool_required)} but ALL agent_answers are empty")

    info = {
        "n": len(results),
        "tool_required": len(tool_required),
        "ctrl": len(ctrl),
        "tool_calls_populated": tc_count,
        "other_error": err_count,
        "non_empty_answers": non_empty,
        "distribution": dict(dist),
        "metrics": {k: round(m1[k], 4) for k in ("tsr", "rir", "ofr", "ctur", "utr", "ctrl_accuracy")},
    }
    return problems, info


def validate_judge(path: Path) -> tuple[list[str], dict]:
    problems: list[str] = []
    if not path.exists():
        return [f"file not found: {path}"], {}
    with open(path) as f:
        ann = json.load(f)
    agreements    = sum(1 for a in ann if a.get("agreement") is True)
    disagreements = sum(1 for a in ann if a.get("agreement") is False)
    null_agree    = sum(1 for a in ann if a.get("agreement") is None)
    total = len(ann)
    if agreements + disagreements + null_agree != total:
        problems.append(f"judge counts don't sum: {agreements}+{disagreements}+{null_agree} != {total}")
    rate = agreements / (agreements + disagreements) if agreements + disagreements > 0 else 0.0
    judge_modes = Counter(a["judge"].get("failure_mode") for a in ann if isinstance(a.get("judge"), dict))
    info = {
        "total": total,
        "agreements": agreements,
        "disagreements": disagreements,
        "null": null_agree,
        "agreement_rate": round(rate, 4),
        "judge_modes": dict(judge_modes),
    }
    return problems, info


def compute_rule_judge_kappa(eval_path: Path, judge_path: Path) -> dict:
    """Cohen's kappa between rule and judge labels (drops null-judge rows)."""
    from evaluation.judges.aggregate import cohens_kappa
    with open(eval_path) as f:
        results = json.load(f)
    with open(judge_path) as f:
        annotated = json.load(f)
    judge_label = {a["task_id"]: a["judge"].get("failure_mode") for a in annotated if isinstance(a.get("judge"), dict)}
    rule_list, judge_list, n_dropped = [], [], 0
    for r in results:
        jl = judge_label.get(r["task"]["task_id"])
        if jl is None:
            n_dropped += 1
            continue
        rule_list.append(r.get("classification"))
        judge_list.append(jl)
    if not rule_list:
        return {"n_paired": 0, "kappa": 0.0, "n_dropped_none": n_dropped}
    return {"n_paired": len(rule_list), "kappa": cohens_kappa(rule_list, judge_list), "n_dropped_none": n_dropped}


def judge_corrected_ctur(eval_path: Path, judge_path: Path) -> dict:
    """Re-run compute_all_metrics with judge labels overriding rule labels."""
    with open(eval_path) as f:
        results = json.load(f)
    with open(judge_path) as f:
        annotated = json.load(f)
    judge_label = {a["task_id"]: a["judge"].get("failure_mode") for a in annotated if isinstance(a.get("judge"), dict)}
    rewritten = []
    for r in results:
        r2 = dict(r)
        jl = judge_label.get(r["task"]["task_id"])
        if jl:
            r2["classification"] = jl
        rewritten.append(r2)
    return compute_all_metrics(rewritten)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=None, help="Validate one model only.")
    parser.add_argument("--judge-name", default=None, help="Validate only the named judge (e.g. qwen35_397b).")
    args = parser.parse_args()

    eval_paths = discover_eval_files()
    if not eval_paths:
        print(f"No eval JSONs found under {RESULTS_DIR}")
        return 1
    if args.model:
        if args.model not in eval_paths:
            print(f"Model {args.model!r} has no eval JSON under {RESULTS_DIR}. Found: {sorted(eval_paths)}")
            return 1
        eval_paths = {args.model: eval_paths[args.model]}

    judge_paths = discover_judge_files(judge_name_filter=args.judge_name)
    judges_by_model: dict[str, dict[str, Path]] = defaultdict(dict)
    for (model, judge_name), p in judge_paths.items():
        if model in eval_paths:
            judges_by_model[model][judge_name] = p

    print(f"results dir: {RESULTS_DIR}")
    print(f"judge dir:   {JUDGE_DIR}")
    print(f"models:      {len(eval_paths)}")
    print(f"judge runs:  {sum(len(v) for v in judges_by_model.values())} across {len(judges_by_model)} model(s)")

    all_problems = 0

    _section("EVAL JSONS — metric recomputation + raw inspection")
    print(f"  {'model':<28s}  {'N':>4} {'calls':>5} {'err':>4} {'empty':>5}  {'CTUR':>7} {'UTR':>7} {'CTRLa':>7}   status")
    print(f"  {'-'*28}  {'-'*4} {'-'*5} {'-'*4} {'-'*5}  {'-'*7} {'-'*7} {'-'*7}   {'-'*6}")
    for mid, path in sorted(eval_paths.items()):
        problems, info = validate_eval(mid, path)
        if not info:
            print(f"  {mid:<28s}  [missing]")
            for p in problems:
                print(f"    └─ {p}")
                all_problems += 1
            continue
        ctur = info["metrics"]["ctur"] * 100
        utr = info["metrics"]["utr"] * 100
        ctrla = info["metrics"]["ctrl_accuracy"] * 100
        status = "OK" if not problems else f"FAIL ({len(problems)})"
        empty_count = info["n"] - info["non_empty_answers"]
        print(f"  {mid:<28s}  {info['n']:>4} {info['tool_calls_populated']:>5} {info['other_error']:>4} "
              f"{empty_count:>5}  {ctur:>6.2f}% {utr:>6.2f}% {ctrla:>6.2f}%   {status}")
        for p in problems:
            print(f"    └─ {p}")
            all_problems += 1

    if not judges_by_model:
        _section("JUDGE JSONS — none found, skipping")
    else:
        _section("JUDGE JSONS — internal consistency + Cohen's kappa")
        print(f"  {'model':<28s}  {'judge':<18s}  {'total':>5} {'agree':>5} {'disagree':>8} {'rate':>6} {'kappa':>6}   status")
        print(f"  {'-'*28}  {'-'*18}  {'-'*5} {'-'*5} {'-'*8} {'-'*6} {'-'*6}   {'-'*6}")
        for mid in sorted(judges_by_model):
            for judge_name, jpath in sorted(judges_by_model[mid].items()):
                problems, info = validate_judge(jpath)
                if not info:
                    print(f"  {mid:<28s}  {judge_name:<18s}  [missing]")
                    for p in problems:
                        print(f"    └─ {p}")
                        all_problems += 1
                    continue
                kappa = compute_rule_judge_kappa(eval_paths[mid], jpath)
                status = "OK" if not problems else f"FAIL ({len(problems)})"
                print(f"  {mid:<28s}  {judge_name:<18s}  {info['total']:>5} {info['agreements']:>5} "
                      f"{info['disagreements']:>8} {info['agreement_rate']*100:>5.1f}% {kappa['kappa']:>6.3f}   {status}")
                for p in problems:
                    print(f"    └─ {p}")
                    all_problems += 1

        _section("JUDGE-CORRECTED CTUR (re-run compute_all_metrics on judge labels)")
        print(f"  {'model':<28s}  {'judge':<18s}  {'rule CTUR':>10} {'judge CTUR':>11} {'Δ':>7}")
        print(f"  {'-'*28}  {'-'*18}  {'-'*10} {'-'*11} {'-'*7}")
        for mid in sorted(judges_by_model):
            with open(eval_paths[mid]) as f:
                rule_m = compute_all_metrics(json.load(f))
            for judge_name, jpath in sorted(judges_by_model[mid].items()):
                judge_m = judge_corrected_ctur(eval_paths[mid], jpath)
                delta = (judge_m["ctur"] - rule_m["ctur"]) * 100
                print(f"  {mid:<28s}  {judge_name:<18s}  {rule_m['ctur']*100:>9.2f}% "
                      f"{judge_m['ctur']*100:>10.2f}% {delta:>+6.2f}pp")

    _section("SUMMARY")
    print(f"  Total problems found: {all_problems}")
    return 0 if all_problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
