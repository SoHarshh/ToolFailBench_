"""Run an LLM judge over ToolFailBench eval results, writing per-task judge labels and their agreement with the rule classifier."""
import argparse
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from evaluation.judges.judge import (  # noqa: E402
    JudgeConfig,
    compare_classifications,
    list_judge_configs,
    load_judge_config,
    run_judge_on_result,
)

load_dotenv()

ROOT = Path(__file__).parent.parent
RESULTS_DIR = ROOT / "results" / "v5"
OUTPUT_DIR = ROOT / "results" / "v5" / "judge"


def find_result_files(results_dir: Path) -> list[Path]:
    """Canonical eval JSONs in results_dir (skips baseline/judge/_old_/_summary aux files)."""
    files = []
    for f in sorted(results_dir.glob("*.json")):
        n = f.name
        if "baseline" in n or "judge" in n or "_old_" in n or n.endswith("_summary.json"):
            continue
        files.append(f)
    return files


def _annotate_one(result: dict, cfg: JudgeConfig, dry_run: bool) -> dict:
    """Build one annotated record (judge verdict + rule comparison)."""
    task_id = result["task"]["task_id"]
    domain = result["task"]["domain"]
    model_id = result["model_id"]
    rb_class = result["classification"]

    if dry_run:
        return {
            "task_id": task_id, "domain": domain, "model_id": model_id,
            "rule_based_classification": rb_class,
            "judge": {"failure_mode": None, "reasoning": "dry_run"},
            "agreement": None,
        }

    verdict = run_judge_on_result(result, cfg)
    agreement = verdict["failure_mode"] == rb_class if verdict.get("failure_mode") else None
    return {
        "task_id": task_id, "domain": domain, "model_id": model_id,
        "rule_based_classification": rb_class,
        "judge": verdict, "agreement": agreement,
    }


def judge_results(results, cfg, delay=0.0, dry_run=False, concurrency=1) -> list[dict]:
    """Run the judge on each result, returning annotated dicts in input order.

    concurrency > 1 dispatches via ThreadPoolExecutor (judge calls are I/O-bound);
    delay applies only when concurrency == 1.
    """
    desc = f"Judging ({cfg.judge_name})"

    if concurrency <= 1 or dry_run:
        annotated = []
        for result in tqdm(results, desc=desc):
            annotated.append(_annotate_one(result, cfg, dry_run))
            if delay > 0 and not dry_run:
                time.sleep(delay)
        return annotated

    annotated = [None] * len(results)
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_annotate_one, r, cfg, False): i for i, r in enumerate(results)}
        with tqdm(total=len(results), desc=desc) as pbar:
            for fut in as_completed(futures):
                annotated[futures[fut]] = fut.result()
                pbar.update(1)
    return annotated


def print_summary(annotated: list[dict], model_id: str, judge_name: str):
    cmp_ = compare_classifications(annotated)

    print(f"\n{'='*60}")
    print(f"  Judge Summary: {model_id}  (judge: {judge_name})")
    print(f"{'='*60}")
    print(f"  Tasks judged:     {cmp_['total_judged']}")
    print(f"  Agreements:       {cmp_['agreements']}")
    print(f"  Disagreements:    {cmp_['disagreements']}")
    print(f"  Agreement rate:   {cmp_['agreement_rate']:.1%}")

    if cmp_["disagreement_details"]:
        print(f"\n  --- Disagreements ({len(cmp_['disagreement_details'])}) ---")
        for d in cmp_["disagreement_details"][:20]:
            print(f"  {d['task_id']:20s}  rule={d['rule_based']:20s}  judge={d['judge']:20s}  conf={d['judge_confidence']}")
            print(f"  {'':20s}  {(d['reasoning'] or '')[:100]}")

    for key in ("result_faithfulness", "answer_correctness"):
        scores = [e["judge"].get(key) for e in annotated if e["judge"].get(key) is not None]
        if scores:
            print(f"\n  --- {key} ---")
            for s in range(4):
                count = scores.count(s)
                print(f"    {s}: {'█' * count} ({count})")

    judge_modes = [e["judge"]["failure_mode"] for e in annotated if e["judge"].get("failure_mode")]
    if judge_modes:
        dist = Counter(judge_modes)
        print(f"\n  --- Judge Failure-Mode Distribution ---")
        for mode, count in sorted(dist.items(), key=lambda x: -x[1]):
            print(f"    {mode:25s} {count:3d}")
    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="Run LLM-as-judge on ToolFailBench results")

    input_group = parser.add_mutually_exclusive_group(required=False)
    input_group.add_argument("--results-file", type=str, help="Path to a single results JSON file")
    input_group.add_argument("--results-dir", type=str,
                             help="Directory of results JSONs (judges all non-aux files). Default: results/v5/")

    parser.add_argument("--judge-config", required=True,
                        help="Bare name (e.g. 'qwen35_397b') or path to a YAML in "
                             f"evaluation/judges/configs/. Available: {list_judge_configs()}")
    parser.add_argument("--output-dir", default=None, help="Output directory. Default: results/v5/judge/")
    parser.add_argument("--sample", type=int, default=None, help="Only judge the first N results per file.")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Delay between API calls in seconds. Ignored when --concurrency > 1.")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="Parallel judge calls per process (ThreadPoolExecutor). Default 1 (sequential).")
    parser.add_argument("--dry-run", action="store_true", help="Skip API calls (validate config + file loading only).")
    args = parser.parse_args()

    results_dir = Path(args.results_dir) if args.results_dir else RESULTS_DIR
    output_dir = Path(args.output_dir) if args.output_dir else OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        os.environ.setdefault("JUDGE_QWEN35_API_BASE", "http://dry-run")
        os.environ.setdefault("JUDGE_GLM47_API_BASE", "http://dry-run")
    cfg = load_judge_config(args.judge_config)
    print(f"Judge: {cfg.judge_name}")
    print(f"  hf_id:    {cfg.hf_id}")
    print(f"  endpoint: {cfg.api_base}")
    print(f"  variant:  {cfg.raw['prompt']['variant']}")

    if args.results_file:
        files = [Path(args.results_file)]
    else:
        if not results_dir.exists():
            print(f"Results dir does not exist: {results_dir}")
            sys.exit(1)
        files = find_result_files(results_dir)
        if not files:
            print(f"No eval JSONs found in {results_dir}")
            sys.exit(1)

    print(f"Files to judge: {[f.name for f in files]}")

    for filepath in files:
        print(f"\n{'='*60}")
        print(f"  Loading: {filepath.name}")
        print(f"{'='*60}")

        results = json.load(open(filepath))
        if not results:
            print("  (empty file, skipping)")
            continue

        model_id = results[0].get("model_id", "unknown")
        if args.sample:
            results = results[: args.sample]
        if args.dry_run:
            print("  [DRY RUN] Skipping API calls…\n")

        annotated = judge_results(results, cfg=cfg, delay=args.delay, dry_run=args.dry_run, concurrency=args.concurrency)

        if args.dry_run:
            continue

        print_summary(annotated, model_id, cfg.judge_name)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = output_dir / f"{model_id}_judge_{cfg.judge_name}_{timestamp}.json"
        with open(out_path, "w") as f:
            json.dump(annotated, f, indent=2, default=str)
        print(f"  Saved to {out_path}")


if __name__ == "__main__":
    main()
