#!/usr/bin/env python3
"""Generate HUD taskset.json from SWE-bench Lite data."""

import json
import sys
from pathlib import Path


def main():
    data_path = Path(__file__).parent.parent / "data" / "swe_bench_lite.json"
    output_path = Path(__file__).parent.parent / "data" / "taskset.json"

    with open(data_path) as f:
        instances = json.load(f)

    tasks = []
    for inst in instances:
        tasks.append({
            "scenario": inst["instance_id"],
            "args": {},
        })

    taskset = {
        "tasks": tasks,
    }

    with open(output_path, "w") as f:
        json.dump(taskset, f, indent=2)

    print(f"Generated taskset with {len(tasks)} tasks at {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
