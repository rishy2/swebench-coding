"""SWE-bench grading: runner and grader for SWE-bench tasks.

The runner applies the test patch, runs ALL tests from the test files,
then parses output to check FAIL_TO_PASS / PASS_TO_PASS individually.
The grader wraps the runner and handles validation modes.
"""

import json
import logging
import os
import re
import shlex
import subprocess

from .spec import Grader, ValidateMode

logger = logging.getLogger(__name__)


class SWEBenchRunner:
    """Runs SWE-bench tests: applies test_patch, runs tests, parses results.

    Applies the test patch in-place (not copying to temp dir) to avoid
    issues with editable installs (pip install -e .) that create symlinks
    pointing to the original directory.

    Runs ALL tests from the test patch files once, then parses the output
    to determine per-test pass/fail status. This handles repos where test
    IDs are bare function names (sympy) or from the same file (Django).
    """

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

    @property
    def test_patch_path(self) -> str:
        return os.path.join(self.patches_dir, self.instance_id, "test.patch")

    @staticmethod
    def _extract_test_files(patch_content: str) -> list[str]:
        """Extract test file paths from a unified diff patch."""
        files = set()
        for line in patch_content.split("\n"):
            if line.startswith("diff --git"):
                # diff --git a/path/to/file b/path/to/file
                parts = line.split(" b/", 1)
                if len(parts) == 2:
                    files.add(parts[1].strip())
        return sorted(files)

    @staticmethod
    def _get_test_directives(test_ids: list[str]) -> list[str]:
        """Convert SWE-bench test IDs to test command directives.

        Handles three formats:
        - unittest-style: 'test_foo (module.path.ClassName)' -> 'module.path.ClassName.test_foo'
        - unittest with embedded method: 'test_foo (module.path.ClassName.test_foo)' -> 'module.path.ClassName.test_foo'
        - pytest-style: 'tests/test_foo.py::test_bar' -> used as-is
        - bare function names: 'test_foo' -> used as-is (needs file paths separately)
        """
        directives = []
        for tid in test_ids:
            m = re.match(r"^(\S+)\s+\((.+)\)$", tid)
            if m:
                test_method, class_path = m.groups()
                # Some SWE-bench instances already include the method name
                # in the class path — don't append it again
                if class_path.endswith(f".{test_method}"):
                    directives.append(class_path)
                else:
                    directives.append(f"{class_path}.{test_method}")
            else:
                directives.append(tid)
        return directives

    @staticmethod
    def _ids_have_paths(test_ids: list[str]) -> bool:
        """Check if test IDs include file paths (pytest/Django style).

        Returns False for bare function names (sympy style like 'test_foo').
        """
        for tid in test_ids:
            # pytest: contains / or ::
            if "/" in tid or "::" in tid:
                return True
            # Django unittest: contains parentheses
            if "(" in tid:
                return True
            # Dotted module path (Django-converted): has multiple dots
            if tid.count(".") >= 2:
                return True
        return False

    def _run_cmd(self, cmd: str, label: str) -> tuple[int, str, str]:
        """Run a command via conda in a clean environment."""
        script_path = os.path.join(self.repo_path, f"_run_{label}.sh")
        with open(script_path, "w") as f:
            f.write("#!/bin/bash\nset -e\n")
            f.write("unset PYTHONPATH\n")
            f.write(f"cd {self.repo_path}\n")
            f.write(f"{cmd}\n")
        os.chmod(script_path, 0o755)

        full_cmd = f"conda run --no-capture-output -n {self.conda_env} bash {script_path}"
        logger.info(f"Running {label}: {cmd[:500]}")

        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

        try:
            result = subprocess.run(
                ["bash", "-lc", full_cmd],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"{label} timed out after {self.timeout}s")
            return -1, "", "TIMEOUT"

        logger.info(f"{label} exit code: {result.returncode}")
        if result.stdout:
            logger.info(f"{label} stdout (last 3000):\n{result.stdout[-3000:]}")
        if result.stderr:
            logger.info(f"{label} stderr (last 2000):\n{result.stderr[-2000:]}")

        return result.returncode, result.stdout or "", result.stderr or ""

    @staticmethod
    def _strip_ansi(text: str) -> str:
        """Strip ANSI escape sequences from text."""
        return re.sub(r"\x1b\[[0-9;]*m", "", text)

    @staticmethod
    def _parse_test_output(stdout: str, stderr: str) -> dict[str, str]:
        """Parse test output from various frameworks into {test_id: PASSED|FAILED}.

        Supports:
        - pytest: 'PASSED tests/test_foo.py::test_bar' in summary
        - Django: 'test_name (module.Class) ... ok' in stderr
        - sympy: 'test_name ok' in stderr
        """
        results = {}

        # Strip ANSI color codes (e.g. from tox/sphinx output)
        stdout = SWEBenchRunner._strip_ansi(stdout)
        stderr = SWEBenchRunner._strip_ansi(stderr)

        # 1. pytest format: two sub-formats
        #    a) Short summary (-rA):  'PASSED tests/test_foo.py::test_bar'
        #    b) Verbose/classic:      'tests/test_foo.py::test_bar PASSED'
        for line in stdout.split("\n"):
            line = line.strip()
            # Format a: status first
            m = re.match(r"^(PASSED|FAILED|ERROR)\s+(.+?)(\s+-\s+.*)?$", line)
            if m:
                status = "PASSED" if m.group(1) == "PASSED" else "FAILED"
                results[m.group(2).strip()] = status
                continue
            # Format b: status last (verbose/classic output)
            m = re.match(r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR)(\s+-\s+.*)?$", line)
            if m:
                status = "PASSED" if m.group(2) == "PASSED" else "FAILED"
                results[m.group(1).strip()] = status

        # 2. Django format in stderr. Two sub-formats:
        #    a) Single line: 'test_method (module.Class) ... ok/FAIL/ERROR'
        #    b) Multi-line (tests with docstrings):
        #       'test_method (module.Class)'
        #       'Description text. ... ok/FAIL/ERROR'
        stderr_lines = stderr.split("\n")
        pending_test_id = None
        for line in stderr_lines:
            line = line.strip()
            # Try single-line format
            m = re.match(r"^(\S+\s+\(.+?\))\s+\.\.\.\s+(ok|FAIL|ERROR|skipped)", line)
            if m:
                test_id = m.group(1)
                status = "PASSED" if m.group(2) == "ok" else "FAILED"
                results[test_id] = status
                pending_test_id = None
                continue
            # Check for test_name (module.Class) without status — multi-line
            m2 = re.match(r"^(\S+\s+\(.+?\))\s*$", line)
            if m2:
                pending_test_id = m2.group(1)
                continue
            # Check for status on description line (multi-line continuation)
            if pending_test_id:
                m3 = re.search(r"\.\.\.\s+(ok|FAIL|ERROR|skipped)\s*$", line)
                if m3:
                    status = "PASSED" if m3.group(1) == "ok" else "FAILED"
                    results[pending_test_id] = status
                    pending_test_id = None
                    continue

        # 3. sympy format: 'test_name ok/F/f/E/s/X/w' in stdout
        # sympy outputs to stdout, statuses: ok=pass, F=fail, f=xfail, E=error,
        # s=skip, X=xpass, w=slow. Only use if no other results found.
        if not results:
            for line in stdout.split("\n"):
                line = line.strip()
                m = re.match(
                    r"^(test_\w+)\s+(ok|F|f|E|s|X|w|FAIL|ERROR|skip|XFAIL)\s*$",
                    line,
                )
                if m:
                    test_name = m.group(1)
                    raw_status = m.group(2)
                    if raw_status in ("ok", "f", "X", "w", "XFAIL"):
                        status = "PASSED"
                    elif raw_status in ("s", "skip"):
                        status = "SKIPPED"
                    else:
                        status = "FAILED"
                    results[test_name] = status

        return results

    @staticmethod
    def _check_tests(
        results: dict[str, str],
        test_ids: list[str],
        expect_pass: bool,
        lenient_not_found: bool = False,
    ) -> tuple[bool, dict]:
        """Check if test IDs match expected status in parsed results.

        Args:
            results: Parsed {test_id: PASSED|FAILED} from test output
            test_ids: List of test IDs to check
            expect_pass: True if tests should pass, False if they should fail
            lenient_not_found: If True, NOT_FOUND tests are ignored (for P2P
                where tests may be skipped due to missing optional deps)
        """
        details = {}
        all_match = True

        for tid in test_ids:
            # Try exact match first
            status = results.get(tid)

            # Try matching by function name (for sympy bare names)
            if status is None:
                func_name = tid.split("::")[-1] if "::" in tid else tid
                for k, v in results.items():
                    # Match bare function name against any key ending with it
                    k_func = k.split("::")[-1] if "::" in k else k
                    if k_func == func_name:
                        status = v
                        break

            if status is None:
                if lenient_not_found:
                    details[tid] = "NOT_FOUND (skipped)"
                else:
                    details[tid] = "NOT_FOUND"
                    all_match = False
            elif status == "SKIPPED":
                # Skipped tests are not failures — treat as OK
                details[tid] = "SKIPPED"
            elif expect_pass and status != "PASSED":
                details[tid] = status
                all_match = False
            elif not expect_pass and status == "PASSED":
                details[tid] = "PASSED (expected FAILED)"
                all_match = False
            else:
                details[tid] = status

        return all_match, details

    def _run_tests_with_directives(
        self, directives: list[str], label: str
    ) -> tuple[int, str, str]:
        """Run the test command with given directives.

        Uses shlex.quote() on each directive to handle special chars
        (parentheses in parametrized pytest IDs, brackets, etc.).
        """
        directive_str = " ".join(shlex.quote(d) for d in directives)
        cmd = f"{self.test_cmd} {directive_str}"
        if self.eval_commands:
            cmd = f"{self.eval_commands} && {cmd}"
        return self._run_cmd(cmd, label)

    def grade(self) -> tuple[float, dict]:
        """Run grading: apply test patch in-place, run tests, parse results.

        Returns:
            (score, metadata) where score is 1.0 if all pass, 0.0 otherwise
        """
        # Verify conda env exists before running tests
        check = subprocess.run(
            ["conda", "run", "-n", self.conda_env, "python", "--version"],
            capture_output=True, text=True, timeout=30,
        )
        if check.returncode != 0:
            logger.error(
                f"Conda env '{self.conda_env}' is missing or broken: {check.stderr}"
            )
            return 0.0, {
                "error": "conda_env_missing",
                "conda_env": self.conda_env,
                "stderr": check.stderr,
            }

        logger.info(f"Applying test patch in-place: {self.test_patch_path}")
        with open(self.test_patch_path) as f:
            patch_content = f.read()

        if not patch_content.strip():
            logger.error("Test patch is empty")
            return 0.0, {"error": "empty_test_patch"}

        result = subprocess.run(
            ["git", "apply", "--verbose"],
            cwd=self.repo_path,
            input=patch_content,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(f"git apply failed: {result.stderr}")
            return 0.0, {"error": "git_apply_failed", "stderr": result.stderr}

        try:
            # Determine test directives
            all_test_ids = self.fail_to_pass + self.pass_to_pass
            directives = self._get_test_directives(all_test_ids)

            # Filter out non-standard test IDs (descriptions, comments)
            # that appear in some SWE-bench instances
            valid_directives = [
                d for d in directives
                if not d.startswith("#") and "  " not in d
                and not d.endswith(".") and len(d.split()) <= 3
            ]
            if len(valid_directives) < len(directives):
                logger.info(
                    f"Filtered {len(directives) - len(valid_directives)} "
                    f"non-standard test IDs"
                )
                directives = valid_directives

            if not self._ids_have_paths(directives):
                # Bare function names (sympy etc.) — use file paths from test_patch
                test_files = self._extract_test_files(patch_content)
                logger.info(f"Using test files from patch: {test_files}")
                directives = test_files
            else:
                # Check if test IDs contain special chars that break pytest
                # node ID matching (parametrized IDs with *, (, ", etc.)
                # or if there are many IDs (>50). In either case, use file
                # paths instead of individual IDs for reliability.
                special_chars = set('()[]*,"\'')
                has_special = any(
                    any(c in d for c in special_chars)
                    for d in directives
                    if "::" in d  # only check pytest-style IDs
                )
                if has_special or len(directives) > 50:
                    file_set = set()
                    for d in directives:
                        if "::" in d:
                            file_set.add(d.split("::")[0])
                        elif "/" in d:
                            file_set.add(d)
                    # For Django-style dotted paths, fall back to test files
                    # from the test patch, converting to dotted module names
                    # for runtests.py (which rejects file paths)
                    if not file_set:
                        for f in self._extract_test_files(patch_content):
                            # Convert tests/app/test_foo.py → app.test_foo
                            label = f
                            if label.startswith("tests/"):
                                label = label[len("tests/"):]
                            label = label.replace("/", ".").removesuffix(".py")
                            file_set.add(label)
                    if file_set:
                        logger.info(
                            f"Using {len(file_set)} test files instead of "
                            f"{len(directives)} individual IDs"
                            f"{' (special chars detected)' if has_special else ''}"
                        )
                        directives = sorted(file_set)

            # De-duplicate directives while preserving order
            seen = set()
            unique_directives = []
            for d in directives:
                if d not in seen:
                    seen.add(d)
                    unique_directives.append(d)

            # Run ALL tests once
            exit_code, stdout, stderr = self._run_tests_with_directives(
                unique_directives, "ALL_TESTS"
            )

            if exit_code == -1:
                # Timeout
                return 0.0, {"error": "timeout"}

            # Parse per-test results from output
            test_results = self._parse_test_output(stdout, stderr)
            logger.info(f"Parsed {len(test_results)} test results")

            # Check F2P: all should PASS (agent fixed the bug)
            f2p_pass, f2p_details = self._check_tests(
                test_results, self.fail_to_pass, expect_pass=True
            )

            # Check P2P: all should still PASS (lenient for NOT_FOUND/skipped)
            p2p_pass, p2p_details = self._check_tests(
                test_results, self.pass_to_pass, expect_pass=True,
                lenient_not_found=True,
            )

        finally:
            logger.info("Reversing test patch")
            subprocess.run(
                ["git", "apply", "--reverse"],
                cwd=self.repo_path,
                input=patch_content,
                capture_output=True,
                text=True,
            )

        score = 1.0 if (f2p_pass and p2p_pass) else 0.0
        metadata = {
            "f2p_pass": f2p_pass,
            "p2p_pass": p2p_pass,
            "f2p_details": f2p_details,
            "p2p_details": p2p_details,
            "exit_code": exit_code,
            "parsed_test_count": len(test_results),
        }

        logger.info(f"Grade: {score} (F2P={f2p_pass}, P2P={p2p_pass})")
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
            # At baseline (no fix), F2P should FAIL and P2P should PASS.
            # The runner checks F2P with expect_pass=True, so f2p_pass=False at baseline.
            # We invert: if F2P failed (as expected) and P2P passed, that's correct.
            f2p_failed = not metadata.get("f2p_pass", True)
            p2p_passed = metadata.get("p2p_pass", False)
            score = 1.0 if (f2p_failed and p2p_passed) else 0.0
            metadata["validate_mode"] = "baseline_fail"

        return score, metadata
