"""Coding environment - tools for solving SWE-bench programming tasks.

This environment provides tools for:
- Running bash commands in a sandboxed shell
- Editing files with view/create/edit commands

Tools prefixed with _ are internal (hidden from agent, used by scenarios).
"""

import json
import logging
import os
import subprocess
from pathlib import Path

from hud import Environment

from grading import ValidateMode
from tools import BashTool, EditTool, ToolError

logger = logging.getLogger(__name__)

# Create the environment
env = Environment("coding")

# Initialize tools
_bash_tool: BashTool | None = None
_edit_tool: EditTool | None = None


def _get_project_dir() -> str:
    """Get the project directory path."""
    return os.getenv("PROJECT_DIR", f"/home/ubuntu/{os.environ.get('FOLDER_NAME', 'project')}")


@env.initialize
async def initialize() -> None:
    """Initialize the coding environment tools."""
    global _bash_tool, _edit_tool

    logger.info("Initializing coding environment")
    _bash_tool = BashTool()
    _edit_tool = EditTool()
    logger.info("Coding environment initialized")


@env.shutdown
async def shutdown() -> None:
    """Clean up the coding environment."""
    global _bash_tool, _edit_tool

    if _bash_tool and _bash_tool._session:
        _bash_tool._session.stop()

    _bash_tool = None
    _edit_tool = None
    logger.info("Coding environment shut down")


# ============================================================================
# Agent-Visible Tools
# ============================================================================


@env.tool()
async def bash(
    command: str | None = None,
    restart: bool = False,
) -> str:
    """Run a bash command in the sandboxed shell.

    Args:
        command: The bash command to execute
        restart: Whether to restart the bash session

    Returns:
        The command output or error message
    """
    if _bash_tool is None:
        return "Error: Bash tool not initialized"

    try:
        result = await _bash_tool(command=command, restart=restart)
        output = result.output or ""
        if result.error:
            output = f"{output}\n{result.error}".strip() if output else result.error
        return output or result.system or ""
    except ToolError as e:
        return f"Error: {e.message}"


@env.tool()
async def editor(
    command: str,
    path: str,
    file_text: str | None = None,
    view_range: list[int] | None = None,
    old_str: str | None = None,
    new_str: str | None = None,
    insert_line: int | None = None,
) -> str:
    """Edit files with view, create, edit, and undo operations.

    Args:
        command: One of 'view', 'create', 'str_replace', 'insert', 'undo_edit'
        path: Absolute path to the file
        file_text: Content for 'create' command
        view_range: [start_line, end_line] for 'view' command
        old_str: String to replace for 'str_replace' command
        new_str: Replacement string for 'str_replace' or 'insert'
        insert_line: Line number for 'insert' command

    Returns:
        The command result or file content
    """
    if _edit_tool is None:
        return "Error: Editor tool not initialized"

    try:
        result = await _edit_tool(
            command=command,  # type: ignore
            path=path,
            file_text=file_text,
            view_range=view_range,
            old_str=old_str,
            new_str=new_str,
            insert_line=insert_line,
        )
        if result.error:
            return f"Error: {result.error}"
        return result.output or ""
    except ToolError as e:
        return f"Error: {e.message}"


# ============================================================================
# Scenario Helpers for SWE-bench
# ============================================================================

# Pre-baked paths (populated at Docker build time by prebake_repos.py)
PREBAKED_REPOS_DIR = "/opt/repos"
PIP_CACHE_DIR = "/opt/pip-cache"
PREBUILD_DIR = "/opt/prebuilt"


def _repo_short_name(repo: str) -> str:
    """Extract short repo name from 'org/repo' format."""
    return repo.split("/")[-1]


def _is_prebaked(repo: str) -> bool:
    """Check if a repo has been pre-baked into the image."""
    return os.path.isdir(os.path.join(PREBAKED_REPOS_DIR, _repo_short_name(repo)))


def _get_prebuild_dir(repo: str, version: str) -> str | None:
    """Get the prebuild directory for a (repo, version) combo if it exists."""
    repo_name = _repo_short_name(repo)
    ver_safe = version.replace(".", "_")
    prebuild = os.path.join(PREBUILD_DIR, f"{repo_name}_{ver_safe}")
    if os.path.isdir(prebuild):
        return prebuild
    return None


def _pip_cache_args(strict: bool = True) -> str:
    """Return pip args to use the local wheel cache if available.

    Args:
        strict: If True, use --no-index (no network fallback).
                If False, just use --find-links (prefer cache, allow network).
    """
    if os.path.isdir(PIP_CACHE_DIR):
        if strict:
            return f"--find-links {PIP_CACHE_DIR} --no-index"
        return f"--find-links {PIP_CACHE_DIR}"
    return ""


def _load_repo_specs() -> dict:
    """Load repo specifications from data/repo_specs.json."""
    specs_path = Path(__file__).parent / "data" / "repo_specs.json"
    with open(specs_path) as f:
        return json.load(f)


def _run(cmd: str | list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command and log output."""
    if isinstance(cmd, str):
        cmd_str = cmd
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, **kwargs)
    else:
        cmd_str = " ".join(cmd)
        result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        logger.warning(f"Command failed (exit {result.returncode}): {cmd_str}")
        if result.stderr:
            logger.warning(f"stderr: {result.stderr[:2000]}")
    return result


def _is_system_preinstall(cmd: str) -> bool:
    """Check if a pre_install command is a system-level operation (apt-get, wget, qhull)."""
    return any(kw in cmd for kw in ("apt-get", "wget", "QHULL", "/testbed/build"))


def _run_preinstall(pre_install: list[str], prebaked: bool, cwd: str) -> None:
    """Run pre_install commands, skipping system-level ones if prebaked."""
    for cmd in pre_install:
        if prebaked and _is_system_preinstall(cmd):
            logger.info(f"Skipping pre_install (prebaked): {cmd[:80]}")
            continue
        logger.info(f"Pre-install: {cmd}")
        _run(cmd, cwd=cwd)


def _pip_install(conda_env: str, packages: list[str], cwd: str, strict_cache: bool = True) -> None:
    """Install pip packages using cache if available."""
    cache = _pip_cache_args(strict=strict_cache)
    pip_list = " ".join(f'"{p}"' for p in packages)
    _run(f"conda run -n {conda_env} pip install {cache} {pip_list}", cwd=cwd)


def _run_install_cmd(conda_env: str, install_cmd: str, cwd: str) -> None:
    """Run the project install command, using pip cache for --find-links (non-strict)."""
    cache = _pip_cache_args(strict=False)
    if cache and "pip install" in install_cmd:
        install_cmd = install_cmd.replace("pip install", f"pip install {cache}", 1)
    _run(f"conda run -n {conda_env} {install_cmd}", cwd=cwd)


def setup_swebench_task(
    instance_id: str,
    repo: str,
    base_commit: str,
    test_patch: str,
    gold_patch: str,
    version: str,
    environment_setup_commit: str,
    fail_to_pass: list[str],
    pass_to_pass: list[str],
    validate_mode: ValidateMode | None = None,
) -> None:
    """Set up environment for a SWE-bench task.

    1. Clone the repo (from local cache if prebaked, else from GitHub)
    2. Checkout environment_setup_commit, install deps via conda
    3. Checkout base_commit
    4. Store patches
    5. If golden_pass validation: apply gold patch
    """
    project_dir = _get_project_dir()
    patches_dir = os.environ.get("PATCHES_DIR", "/home/root/patches")

    # Set PROBLEM_ID env var for grading
    os.environ["PROBLEM_ID"] = instance_id

    # Load repo specs
    repo_specs = _load_repo_specs()
    repo_spec = repo_specs.get(repo, {})
    version_spec = repo_spec.get("versions", {}).get(version, {})

    python_version = version_spec.get("python", "3.9")
    install_cmd = version_spec.get("install", "python -m pip install -e .")
    test_cmd = version_spec.get("test_cmd", "pytest -rA")
    pre_install = version_spec.get("pre_install", [])
    pip_packages = version_spec.get("pip_packages", [])
    eval_commands = version_spec.get("eval_commands", [])

    # Determine conda env name
    py_major_minor = python_version.replace(".", "")
    conda_env = f"py{py_major_minor}"
    prebaked = _is_prebaked(repo)

    logger.info(f"Setting up SWE-bench task: {instance_id}")
    logger.info(f"  repo={repo}, version={version}, python={python_version}, conda_env={conda_env}, prebaked={prebaked}")

    # 1. Clone the repo (prefer prebuild > local clone > network)
    _run(["rm", "-rf", project_dir])
    prebuild_dir = _get_prebuild_dir(repo, version)
    if prebuild_dir:
        logger.info(f"Copying prebuild from {prebuild_dir} to {project_dir}")
        _run(["cp", "-a", prebuild_dir, project_dir])
    elif prebaked:
        cached_repo = os.path.join(PREBAKED_REPOS_DIR, _repo_short_name(repo))
        logger.info(f"Local clone from {cached_repo} to {project_dir}")
        _run(["git", "clone", "--local", cached_repo, project_dir])
    else:
        logger.info(f"Network clone {repo} to {project_dir}")
        _run(["git", "clone", f"https://github.com/{repo}.git", project_dir])

    # Mark as safe directory
    _run(["git", "config", "--global", "--add", "safe.directory", project_dir])

    # 2. Checkout environment_setup_commit and install deps
    # When we have a prebuild, skip the install at env_setup_commit — the prebuild
    # already ran pre_install + pip_packages + install_cmd at that commit.
    # This avoids a redundant C extension compilation (~5 min for astropy/sklearn).
    if prebuild_dir:
        logger.info("Prebuild available — skipping env_setup_commit install (already done at build time)")
    else:
        if environment_setup_commit:
            logger.info(f"Checking out environment_setup_commit: {environment_setup_commit}")
            _run(["git", "checkout", environment_setup_commit], cwd=project_dir)

        # Run pre_install commands (skip system-level ones if prebaked)
        _run_preinstall(pre_install, prebaked, project_dir)

        # Ensure pytest is available in the conda env (needed for test execution)
        _pip_install(conda_env, ["pytest"], project_dir, strict_cache=False)

        # Install pinned pip packages BEFORE install (provides build deps like numpy
        # for repos that use --no-build-isolation like scikit-learn)
        if pip_packages:
            logger.info(f"Pinning {len(pip_packages)} pip packages")
            _pip_install(conda_env, pip_packages, project_dir, strict_cache=False)

        # Run install command, then re-pin packages to correct versions
        logger.info(f"Installing: {install_cmd}")
        _run_install_cmd(conda_env, install_cmd, project_dir)

        if pip_packages:
            logger.info(f"Re-pinning {len(pip_packages)} pip packages")
            _pip_install(conda_env, pip_packages, project_dir, strict_cache=False)

    # 3. Checkout base_commit (force to handle dirty files from pre_install)
    logger.info(f"Checking out base_commit: {base_commit}")
    _run(["git", "checkout", "-f", base_commit], cwd=project_dir)

    # Install at base_commit
    logger.info(f"Installing at base_commit")
    _run_preinstall(pre_install, prebaked, project_dir)
    _run_install_cmd(conda_env, install_cmd, project_dir)
    if pip_packages:
        logger.info(f"Pinning {len(pip_packages)} pip packages")
        _pip_install(conda_env, pip_packages, project_dir, strict_cache=False)

    # 4. Store patches
    task_patches_dir = os.path.join(patches_dir, instance_id)
    os.makedirs(task_patches_dir, exist_ok=True)

    with open(os.path.join(task_patches_dir, "test.patch"), "w") as f:
        f.write(test_patch)

    with open(os.path.join(task_patches_dir, "golden.patch"), "w") as f:
        f.write(gold_patch)

    # 5. If golden_pass validation: apply gold patch
    if validate_mode == "golden_pass":
        logger.info("Applying golden patch for validation")
        result = _run(
            ["git", "apply", "--verbose"],
            cwd=project_dir,
            input=gold_patch,
        )
        if result.returncode != 0:
            logger.error(f"Failed to apply golden patch: {result.stderr}")

    # Set ownership and protect .git
    _run(["chown", "-R", "ubuntu:ubuntu", project_dir])
    _run(["chown", "-R", "root:root", os.path.join(project_dir, ".git")])
    _run(["chmod", "-R", "700", os.path.join(project_dir, ".git")])

    # Set env vars for grading
    os.environ["SWE_CONDA_ENV"] = conda_env
    os.environ["SWE_TEST_CMD"] = test_cmd
    os.environ["SWE_INSTALL_CMD"] = install_cmd
    if eval_commands:
        os.environ["SWE_EVAL_COMMANDS"] = " && ".join(eval_commands)
    else:
        os.environ.pop("SWE_EVAL_COMMANDS", None)

    # Store test IDs for grading
    os.environ["SWE_FAIL_TO_PASS"] = json.dumps(fail_to_pass)
    os.environ["SWE_PASS_TO_PASS"] = json.dumps(pass_to_pass)

    os.chdir(project_dir)
    logger.info(f"Setup complete for {instance_id}")


def make_swebench_prompt(problem_statement: str, hints_text: str = "", repo: str = "") -> str:
    """Generate a prompt for a SWE-bench task."""
    prompt = f"""You are working on a bug fix in a Python repository.
The repository has been cloned to /home/ubuntu/project.

Here is the issue description:

{problem_statement}
"""
    if hints_text:
        prompt += f"""
Here are some hints that may help:

{hints_text}
"""

    prompt += """
You MUST edit the relevant file(s) to fix the bug. Do not just describe the fix.
Use the bash and editor tools to explore the codebase and make your changes.
"""
    return prompt


# ============================================================================
# Import and register all scenarios from tasks/
# ============================================================================

import tasks  # noqa: E402, F401 - registers scenarios
