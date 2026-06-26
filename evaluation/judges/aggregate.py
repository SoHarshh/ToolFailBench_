"""
Multi-judge ensemble aggregation for ToolFailBench.

Combines the rule-based label with the two LLM-judge labels (Qwen3.5-397B and
GLM-4.7) per task into a majority-vote ensemble label (ties → rule), and
computes the inter-rater agreement statistics the paper reports:

  - Pairwise Cohen's kappa (rule↔judge, judge↔judge)
  - Fleiss' kappa across all three raters
  - Per-model ensemble label counts

Inputs:
  - eval JSON   (rule labels:  results/v5/<model>_<ts>.json)
  - judge JSONs (one per judge: results/v5/judge/<model>_judge_<judge>_<ts>.json)

Output: results/v5/judge_ensemble/<model>_ensemble_<ts>.json with the per-task
labels from each rater and the aggregated metrics. Pure Python — no scipy.
"""
import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent

# Label space shared by the rule classifier and the judges.
LABELS = ("correct", "tool_skip", "result_ignore", "output_fabrication",
          "unnecessary_tool_use", "wrong_answer")


def _ensemble_dir_for(eval_path: Path) -> Path:
    """Place the ensemble output next to the eval JSON's results dir."""
    eval_path = eval_path.resolve()
    if "results/v5" in str(eval_path) or eval_path.parent.name == "v5":
        return ROOT / "results" / "v5" / "judge_ensemble"
    return ROOT / "results" / "judge_ensemble"


def _load_eval(path: Path) -> dict[str, str]:
    """Return {task_id: rule_label} from an eval JSON."""
    with open(path) as f:
        data = json.load(f)
    rows = data if isinstance(data, list) else (data.get("results") or data.get("tasks") or [])
    out = {}
    for r in rows:
        task = r.get("task") if isinstance(r.get("task"), dict) else {}
        tid = task.get("task_id") or r.get("task_id") or r.get("id")
        label = r.get("classification") or r.get("failure_mode") or r.get("label")
        if tid and label:
            out[tid] = label
    return out


def _load_judge(path: Path) -> dict[str, str]:
    """Return {task_id: judge_label} from a judge JSON; None labels are dropped."""
    with open(path) as f:
        data = json.load(f)
    rows = data if isinstance(data, list) else (data.get("judgments") or data.get("results") or [])
    out = {}
    for r in rows:
        tid = r.get("task_id") or r.get("id")
        if not tid:
            continue
        judge = r.get("judge") if isinstance(r.get("judge"), dict) else None
        label = (judge.get("failure_mode") if judge else None) or r.get("judge_label") or r.get("label")
        if label and label != "None":
            out[tid] = label
    return out


def cohens_kappa(labels_a: list, labels_b: list, classes: tuple = LABELS) -> float:
    """Cohen's kappa between two equal-length categorical label lists."""
    if len(labels_a) != len(labels_b):
        raise ValueError("Label lists must be same length")
    n = len(labels_a)
    if n == 0:
        return 0.0
    p_o = sum(1 for a, b in zip(labels_a, labels_b) if a == b) / n
    counts_a = Counter(labels_a)
    counts_b = Counter(labels_b)
    p_e = sum((counts_a.get(c, 0) / n) * (counts_b.get(c, 0) / n) for c in classes)
    if p_e >= 1.0:
        return 1.0
    return (p_o - p_e) / (1.0 - p_e)


def fleiss_kappa(rater_labels: list[list], classes: tuple = LABELS) -> float:
    """Fleiss' kappa across N raters and M items.

    rater_labels = [[item1_rater1, item1_rater2, ...], [item2_...], ...]
    """
    if not rater_labels:
        return 0.0
    n_items = len(rater_labels)
    n_raters = len(rater_labels[0])
    if any(len(r) != n_raters for r in rater_labels):
        raise ValueError("All items must have the same number of raters")

    item_class_counts = [[Counter(item).get(cls, 0) for cls in classes] for item in rater_labels]
    p_i = [
        sum(c * (c - 1) for c in counts) / (n_raters * (n_raters - 1)) if n_raters > 1 else 0
        for counts in item_class_counts
    ]
    p_bar = sum(p_i) / n_items if n_items else 0

    class_totals = [0] * len(classes)
    for counts in item_class_counts:
        for j, c in enumerate(counts):
            class_totals[j] += c
    total_assignments = n_items * n_raters
    p_j = [t / total_assignments for t in class_totals]
    p_e_bar = sum(p ** 2 for p in p_j)
    if p_e_bar >= 1.0:
        return 1.0
    return (p_bar - p_e_bar) / (1.0 - p_e_bar)


def majority_vote(labels: list, fallback: str) -> str:
    """Majority label across raters; ties resolve to fallback (the rule label)."""
    if not labels:
        return fallback
    top = Counter(labels).most_common()
    if len(top) > 1 and top[0][1] == top[1][1]:
        return fallback
    return top[0][0]


def aggregate(rule_labels: dict, judge_labels: dict) -> dict:
    """Combine rule labels with N judge label dicts ({judge_name: {tid: label}})."""
    judge_names = list(judge_labels.keys())
    common_tids = sorted(set(rule_labels) & set.intersection(*[set(j) for j in judge_labels.values()]))

    rows = []
    for tid in common_tids:
        rule = rule_labels[tid]
        judges = [judge_labels[name][tid] for name in judge_names]
        rows.append({
            "task_id": tid,
            "rule": rule,
            **{f"judge_{n}": l for n, l in zip(judge_names, judges)},
            "ensemble": majority_vote([rule] + judges, fallback=rule),
        })

    rule_list = [r["rule"] for r in rows]
    judge_lists = {n: [r[f"judge_{n}"] for r in rows] for n in judge_names}

    pairwise = {f"rule__{judge_names[0]}": cohens_kappa(rule_list, judge_lists[judge_names[0]])}
    if len(judge_names) >= 2:
        pairwise[f"rule__{judge_names[1]}"] = cohens_kappa(rule_list, judge_lists[judge_names[1]])
        pairwise[f"{judge_names[0]}__{judge_names[1]}"] = cohens_kappa(
            judge_lists[judge_names[0]], judge_lists[judge_names[1]]
        )

    rater_lists = [rule_list] + [judge_lists[n] for n in judge_names]
    fleiss = fleiss_kappa([list(t) for t in zip(*rater_lists)])

    return {
        "n_tasks": len(rows),
        "n_raters": 1 + len(judge_names),
        "raters": ["rule"] + judge_names,
        "pairwise_cohens_kappa": pairwise,
        "fleiss_kappa": fleiss,
        "ensemble_label_counts": dict(Counter(r["ensemble"] for r in rows)),
        "rows": rows,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--eval", required=True, help="Path to eval JSON")
    p.add_argument("--judge", action="append", required=True,
                   help="Judge JSON as name:path (repeatable), e.g. "
                        "qwen35_397b:results/v5/judge/<model>_judge_qwen35_397b_<ts>.json")
    p.add_argument("--output", help="Output JSON path; default → results/v5/judge_ensemble/")
    args = p.parse_args()

    rule_labels = _load_eval(Path(args.eval))
    judge_labels = {}
    for spec in args.judge:
        if ":" not in spec:
            raise SystemExit(f"--judge must be name:path, got {spec!r}")
        name, path = spec.split(":", 1)
        judge_labels[name] = _load_judge(Path(path))

    result = aggregate(rule_labels, judge_labels)

    if args.output:
        out_path = Path(args.output)
    else:
        ensemble_dir = _ensemble_dir_for(Path(args.eval))
        ensemble_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = ensemble_dir / f"{Path(args.eval).stem}_ensemble_{ts}.json"

    out_path.write_text(json.dumps(result, indent=2))
    print(f"Wrote {out_path}")
    print(f"  n_tasks: {result['n_tasks']}")
    print(f"  Fleiss' kappa: {result['fleiss_kappa']:.4f}")
    for name, k in result["pairwise_cohens_kappa"].items():
        print(f"  Cohen's kappa ({name}): {k:.4f}")


if __name__ == "__main__":
    main()
