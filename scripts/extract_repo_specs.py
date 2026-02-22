#!/usr/bin/env python3
"""Extract repo specifications from swebench package for all SWE-bench Lite repos.

Produces data/repo_specs.json with per-repo, per-version install/test commands.
"""

import json
import sys
from pathlib import Path

from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS_PY


def main():
    data_path = Path(__file__).parent.parent / "data"
    instances_path = data_path / "swe_bench_lite.json"
    output_path = data_path / "repo_specs.json"

    # Load instances to get the set of repos/versions we need
    with open(instances_path) as f:
        instances = json.load(f)

    # Collect unique repo/version pairs
    repo_versions: dict[str, set[str]] = {}
    for inst in instances:
        repo = inst["repo"]
        version = inst["version"]
        if repo not in repo_versions:
            repo_versions[repo] = set()
        repo_versions[repo].add(version)

    print(f"Found {len(repo_versions)} repos, {sum(len(v) for v in repo_versions.values())} version entries")

    specs: dict = {}
    for repo, versions in sorted(repo_versions.items()):
        repo_spec: dict = {"versions": {}}

        # MAP_REPO_VERSION_TO_SPECS_PY is keyed by repo name, value is dict of version -> spec
        repo_all_versions = MAP_REPO_VERSION_TO_SPECS_PY.get(repo, {})

        for version in sorted(versions):
            raw = repo_all_versions.get(version, {})
            if not raw:
                print(f"  WARNING: No spec for {repo} v{version}")
                continue

            version_spec: dict = {
                "python": raw.get("python", "3.9"),
                "install": raw.get("install", "python -m pip install -e ."),
                "test_cmd": raw.get("test_cmd", "pytest -rA"),
            }
            if "pre_install" in raw:
                version_spec["pre_install"] = raw["pre_install"]
            if "pip_packages" in raw:
                version_spec["pip_packages"] = raw["pip_packages"]
            if "packages" in raw:
                version_spec["packages"] = raw["packages"]
            if "eval_commands" in raw:
                version_spec["eval_commands"] = raw["eval_commands"]

            repo_spec["versions"][version] = version_spec

        if repo_spec["versions"]:
            specs[repo] = repo_spec

    with open(output_path, "w") as f:
        json.dump(specs, f, indent=2)

    print(f"Saved specs for {len(specs)} repos to {output_path}")

    # Print summary
    for repo, spec in sorted(specs.items()):
        versions = sorted(spec["versions"].keys())
        print(f"  {repo}: {len(versions)} versions ({', '.join(versions)})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
