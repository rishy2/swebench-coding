"""SWE-bench Lite tasks.

300 tasks dynamically registered from data/swe_bench_lite.json.
Each task clones a repo, sets up the environment, and grades via SWE-bench test suites.
"""

import json
import logging
from pathlib import Path

from env import env, setup_swebench_task, make_swebench_prompt
from grading import Grade, SWEBenchGrader, ValidateMode

logger = logging.getLogger(__name__)


def _make_scenario(instance: dict):
    """Create a scenario function for a SWE-bench instance.

    Uses a factory function to properly capture `instance` in a closure,
    avoiding the Python closure-in-loop variable capture issue.
    """
    async def task(hints_enabled: bool = False, validate_mode: ValidateMode | None = None):
        setup_swebench_task(
            instance_id=instance["instance_id"],
            repo=instance["repo"],
            base_commit=instance["base_commit"],
            test_patch=instance["test_patch"],
            gold_patch=instance["patch"],
            version=instance["version"],
            environment_setup_commit=instance.get("environment_setup_commit", ""),
            fail_to_pass=instance["FAIL_TO_PASS"],
            pass_to_pass=instance["PASS_TO_PASS"],
            validate_mode=validate_mode,
        )

        hints_text = instance.get("hints_text", "") if hints_enabled else ""
        prompt = make_swebench_prompt(
            problem_statement=instance["problem_statement"],
            hints_text=hints_text,
            repo=instance["repo"],
        )

        _ = yield prompt

        grade = Grade.from_subscores([
            SWEBenchGrader.grade(
                weight=1.0,
                validate_mode=validate_mode,
            )
        ])
        yield grade.score

    task.__doc__ = f"SWE-bench: {instance['instance_id']}"
    return task


def _register_task(instance: dict) -> None:
    """Register a single SWE-bench instance as an @env.scenario."""
    scenario_name = instance["instance_id"]
    task = _make_scenario(instance)
    env.scenario(scenario_name)(task)


def register_all() -> None:
    """Load all SWE-bench Lite instances and register as scenarios."""
    data_path = Path(__file__).parent.parent / "data" / "swe_bench_lite.json"
    if not data_path.exists():
        logger.warning(f"SWE-bench data not found at {data_path}, skipping registration")
        return

    with open(data_path) as f:
        instances = json.load(f)

    logger.info(f"Registering {len(instances)} SWE-bench Lite scenarios")
    for instance in instances:
        _register_task(instance)
    logger.info(f"Registered {len(instances)} SWE-bench scenarios")


register_all()
