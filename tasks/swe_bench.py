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


def _register_task(instance: dict) -> None:
    """Register a single SWE-bench instance as an @env.scenario."""
    instance_id = instance["instance_id"]
    # Scenario names use the instance_id directly (e.g., "django__django-11099")
    scenario_name = instance_id

    @env.scenario(scenario_name)
    async def task(
        hints_enabled: bool = False,
        validate_mode: ValidateMode | None = None,
        _instance: dict = instance,
    ):
        setup_swebench_task(
            instance_id=_instance["instance_id"],
            repo=_instance["repo"],
            base_commit=_instance["base_commit"],
            test_patch=_instance["test_patch"],
            gold_patch=_instance["patch"],
            version=_instance["version"],
            environment_setup_commit=_instance.get("environment_setup_commit", ""),
            fail_to_pass=_instance["FAIL_TO_PASS"],
            pass_to_pass=_instance["PASS_TO_PASS"],
            validate_mode=validate_mode,
        )

        hints_text = _instance.get("hints_text", "") if hints_enabled else ""
        prompt = make_swebench_prompt(
            problem_statement=_instance["problem_statement"],
            hints_text=hints_text,
            repo=_instance["repo"],
        )

        _ = yield prompt

        grade = Grade.from_subscores([
            SWEBenchGrader.grade(
                weight=1.0,
                validate_mode=validate_mode,
            )
        ])
        yield grade.score

    task.__doc__ = f"SWE-bench: {instance_id}"


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
