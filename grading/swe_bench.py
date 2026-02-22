"""SWE-bench grading: runner and grader for SWE-bench tasks.

The runner applies the test patch and runs FAIL_TO_PASS / PASS_TO_PASS tests.
The grader wraps the runner and handles validation modes.
"""

import json
import logging
import os
import subprocess
import uuid

from .spec import Grader, ValidateMode

logger = logging.getLogger(__name__)


class SWEBenchRunner:
    """Runs SWE-bench tests: applies test_patch, runs F2P and P2P test suites."""

    def __init__(
        self,
        instance_id: str,
        conda_env: str,
        test_cmd: str,
        fail_to_pass: list[str],
        pass_to_pass: list[str],
        eval_commands: str = "",
        patches_dir: str = "/home/root/patches",
        repo_path: str | None = None,
        timeout: int = 600,
    ):
        self.instance_id = instance_id
        self.conda_env = conda_env
        self.test_cmd = test_cmd
        self.fail_to_pass = fail_to_pass
        self.pass_to_pass = pass_to_pass
        self.eval_commands = eval_commands
        self.patches_dir = patches_dir
        self.repo_path = repo_path or f"/home/ubuntu/{os.environ.get('FOLDER_NAME', 'project')}"
        self.timeout = timeout
        self.working_dir = f"/tmp/grading_{uuid.uuid4()}"

    @property
    def test_patch_path(self) -> str:
        return os.path.join(self.patches_dir, self.instance_id, "test.patch")

    def _run_test_suite(self, test_ids: list[str], label: str) -> tuple[bool, dict]:
        """Run a set of tests and return (success, metadata)."""
        if not test_ids:
            return True, {"skipped": True, "label": label}

        # Build test command with specific test IDs
        test_id_str = " ".join(test_ids)
        cmd = f"{self.test_cmd} {test_id_str}"

        # Prepend eval_commands if present
        if self.eval_commands:
            cmd = f"{self.eval_commands} && {cmd}"

        # Write to a temp script to avoid shell quoting issues with test IDs
        # (test IDs often contain brackets, colons, etc.)
        script_path = os.path.join(self.working_dir, f"_run_{label}.sh")
        with open(script_path, "w") as f:
            f.write("#!/bin/bash\nset -e\n")
            f.write(f"cd {self.working_dir}\n")
            f.write(f"{cmd}\n")
        os.chmod(script_path, 0o755)

        full_cmd = f"conda run -n {self.conda_env} bash {script_path}"
        logger.info(f"Running {label} ({len(test_ids)} tests): {cmd[:500]}")

        try:
            result = subprocess.run(
                ["bash", "-lc", full_cmd],
                cwd=self.working_dir,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"{label} timed out after {self.timeout}s")
            return False, {"timeout": True, "label": label}

        logger.info(f"{label} exit code: {result.returncode}")
        if result.stdout:
            logger.info(f"{label} stdout (last 3000):\n{result.stdout[-3000:]}")
        if result.stderr:
            logger.info(f"{label} stderr (last 2000):\n{result.stderr[-2000:]}")

        return result.returncode == 0, {
            "label": label,
            "exit_code": result.returncode,
            "stdout": result.stdout[-3000:] if result.stdout else "",
            "stderr": result.stderr[-2000:] if result.stderr else "",
        }

    def grade(self) -> tuple[float, dict]:
        """Run grading: copy repo, apply test patch, run F2P + P2P tests.

        Returns:
            (score, metadata) where score is 1.0 if all pass, 0.0 otherwise
        """
        # Copy repo to grading workspace
        logger.info(f"Copying repo to {self.working_dir}")
        subprocess.run(["cp", "-rT", self.repo_path, self.working_dir], check=True)

        # Apply test patch
        logger.info(f"Applying test patch: {self.test_patch_path}")
        with open(self.test_patch_path) as f:
            patch_content = f.read()

        if not patch_content.strip():
            logger.error("Test patch is empty")
            return 0.0, {"error": "empty_test_patch"}

        result = subprocess.run(
            ["git", "apply", "--verbose"],
            cwd=self.working_dir,
            input=patch_content,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(f"git apply failed: {result.stderr}")
            return 0.0, {"error": "git_apply_failed", "stderr": result.stderr}

        # Run FAIL_TO_PASS tests
        f2p_success, f2p_meta = self._run_test_suite(self.fail_to_pass, "FAIL_TO_PASS")

        # Run PASS_TO_PASS tests
        p2p_success, p2p_meta = self._run_test_suite(self.pass_to_pass, "PASS_TO_PASS")

        score = 1.0 if (f2p_success and p2p_success) else 0.0
        metadata = {
            "f2p": f2p_meta,
            "p2p": p2p_meta,
            "f2p_pass": f2p_success,
            "p2p_pass": p2p_success,
        }

        logger.info(f"Grade: {score} (F2P={f2p_success}, P2P={p2p_success})")
        return score, metadata


class SWEBenchGrader(Grader):
    """Grader for SWE-bench tasks. Reads config from env vars set by setup_swebench_task()."""

    name = "SWEBenchGrader"

    @classmethod
    def compute_score(
        cls,
        validate_mode: ValidateMode | None = None,
        **kwargs,
    ) -> tuple[float, dict]:
        instance_id = os.environ.get("PROBLEM_ID", "")
        conda_env = os.environ.get("SWE_CONDA_ENV", "py39")
        test_cmd = os.environ.get("SWE_TEST_CMD", "pytest -rA")
        eval_commands = os.environ.get("SWE_EVAL_COMMANDS", "")
        fail_to_pass = json.loads(os.environ.get("SWE_FAIL_TO_PASS", "[]"))
        pass_to_pass = json.loads(os.environ.get("SWE_PASS_TO_PASS", "[]"))

        runner = SWEBenchRunner(
            instance_id=instance_id,
            conda_env=conda_env,
            test_cmd=test_cmd,
            fail_to_pass=fail_to_pass,
            pass_to_pass=pass_to_pass,
            eval_commands=eval_commands,
        )

        score, metadata = runner.grade()

        # Handle validation modes
        if validate_mode == "baseline_fail":
            # At baseline (no fix), F2P should FAIL. We only check F2P here.
            # If F2P failed (as expected), that's correct -> score 1.0
            # P2P should still pass at baseline.
            f2p_failed = not metadata.get("f2p_pass", True)
            p2p_passed = metadata.get("p2p_pass", False)
            score = 1.0 if (f2p_failed and p2p_passed) else 0.0
            metadata["validate_mode"] = "baseline_fail"

        return score, metadata
