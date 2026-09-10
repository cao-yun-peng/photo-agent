"""Run L1/L2 Development baselines; all paid requests require --live."""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def git_head():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=["routing", "trajectory"])
    parser.add_argument(
        "--split", choices=["development", "validation", "test"], default="development"
    )
    parser.add_argument("--ack-test-consumption", action="store_true")
    parser.add_argument(
        "--mode",
        choices=["rule_router", "contextual_router", "router_system"],
        default="rule_router",
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--dataset", type=Path, help="explicit synthetic dataset override"
    )
    args = parser.parse_args()
    if args.split == "test" and not args.ack_test_consumption:
        parser.error(
            "Test requires --ack-test-consumption; viewing results consumes the holdout"
        )
    if not 1 <= args.limit <= 100 or not 1 <= args.repeat <= 5:
        parser.error("limit must be 1..100, repeat 1..5")
    if args.suite == "routing" and args.mode != "rule_router" and not args.live:
        parser.error("model routing requires explicit --live; no mock quality scores")
    if args.suite == "routing" and args.mode == "rule_router" and args.live:
        parser.error("rule_router does not call models")
    if args.suite == "trajectory" and args.mode != "rule_router":
        parser.error("--mode applies only to routing")
    # Explicit offline environment overrides prevent inherited .env credentials.
    os.environ["OTEL_ENABLED"] = "false"
    if not args.live:
        os.environ.update(DASHSCOPE_API_KEY="", OPENAI_API_KEY="")
    from app.evaluation.runner import (
        file_hashes,
        read_cases,
        run_routing,
        run_trajectory,
    )
    from app.evaluation.scoring import aggregate, routing_metrics
    from app.services.agent_contract import PROMPT_CONTRACT_VERSION
    from app.services.agent_llm import _is_mock_llm
    from app.config import settings

    if args.live and _is_mock_llm():
        parser.error("--live requires configured model credentials")
    path = ROOT / (
        "tests/eval/routing/turn_routing_v1.jsonl"
        if args.suite == "routing"
        else "tests/eval/agent/agent_trajectory_v1.jsonl"
    )
    path = args.dataset or path
    cases = read_cases(path, args.split)
    if args.suite == "routing" and args.mode == "contextual_router":
        cases = [c for c in cases if c["expected"]["rule_outcome"] == "defer"]
    cases = cases[: args.limit]
    rows = []
    for repeat in range(args.repeat):
        for case in cases:
            row = await (
                run_routing(case, args.mode)
                if args.suite == "routing"
                else run_trajectory(case, args.live)
            )
            row["repeat"] = repeat
            rows.append(row)
    summary = aggregate(rows)
    if args.suite == "routing":
        summary.update(routing_metrics(rows))
    report = {
        "schema_version": 1,
        "suite": args.suite,
        "mode": args.mode
        if args.suite == "routing"
        else "live"
        if args.live
        else "scripted",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "split": args.split,
        "repeat": args.repeat,
        "case_limit": args.limit,
        "sampling": {
            "agent_temperature": 0.3,
            "agent_max_tokens": 800,
            "router_temperature": 0.0,
            "router_max_tokens": 240,
        },
        "git_head": git_head(),
        "file_sha256": file_hashes(ROOT),
        "dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "version_basis": "source_and_dataset_sha256",
        "model": settings.qwen_chat_model if args.live else None,
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
        "evidence_type": "model_seed_baseline"
        if args.live
        else "deterministic_contract_baseline",
        "limitations": [
            "single-annotator seeds; no release approval",
            "no real tools, ownership, billing or E2E measured",
            "contextual model prompt has no reference-date injection; only local parser clock is fixed",
            "no monetary price configuration; cost is null",
        ],
        "summary": summary,
        "rows": rows,
        "gate": "baseline_pass"
        if summary["passed"] == summary["count"]
        else "baseline_failed",
        "release_gate": "not_evaluated",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"report": str(args.output), "gate": report["gate"], "summary": summary},
            ensure_ascii=False,
        )
    )
    return 0 if report["gate"] == "baseline_pass" else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except (ValueError, KeyError, TypeError) as exc:
        print(f"Invalid evaluation contract: {type(exc).__name__}", file=sys.stderr)
        sys.exit(2)
