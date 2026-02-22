#!/usr/bin/env python3
"""Download SWE-bench Lite dataset from HuggingFace and save as JSON."""

import json
import sys
from pathlib import Path

from datasets import load_dataset


def main():
    output_path = Path(__file__).parent.parent / "data" / "swe_bench_lite.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print("Loading SWE-bench Lite from HuggingFace...")
    ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")

    instances = []
    for row in ds:
        instance = {
            "instance_id": row["instance_id"],
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "patch": row["patch"],
            "test_patch": row["test_patch"],
            "problem_statement": row["problem_statement"],
            "hints_text": row.get("hints_text", ""),
            "FAIL_TO_PASS": json.loads(row["FAIL_TO_PASS"]) if isinstance(row["FAIL_TO_PASS"], str) else row["FAIL_TO_PASS"],
            "PASS_TO_PASS": json.loads(row["PASS_TO_PASS"]) if isinstance(row["PASS_TO_PASS"], str) else row["PASS_TO_PASS"],
            "version": row.get("version", ""),
            "environment_setup_commit": row.get("environment_setup_commit", ""),
            "created_at": row.get("created_at", ""),
        }
        instances.append(instance)

    with open(output_path, "w") as f:
        json.dump(instances, f, indent=2)

    print(f"Saved {len(instances)} instances to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
