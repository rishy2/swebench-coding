#!/usr/bin/env python3
"""Pre-bake SWE-bench repos and pip caches into the Docker image.

Executed during `docker build`. Reads data/repo_specs.json and swe_bench_lite.json:
1. Installs system packages (apt-get) needed by matplotlib, scikit-learn
2. Downloads external build deps (qhull for matplotlib)
3. Clones all 12 repos to /opt/repos/{short_name}/
4. Builds COMPILED wheels for all pip packages to /opt/pip-cache/
   (uses `pip install` + `pip wheel` instead of just `pip download`)
5. Pre-builds projects for all 64 (repo, version) combos to /opt/prebuilt/
   (compiles C extensions so runtime only needs incremental rebuild)

This eliminates runtime network ops AND compilation that cause ReadTimeout on HUD.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPOS_DIR = "/opt/repos"
PIP_CACHE_DIR = "/opt/pip-cache"
PREBUILD_DIR = "/opt/prebuilt"
SPECS_PATH = Path("/tmp/data/repo_specs.json")
TASKS_PATH = Path("/tmp/data/swe_bench_lite.json")


def run(cmd, **kwargs):
    """Run a command, printing it first."""
    cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
    print(f"  >>> {cmd_str[:200]}", flush=True)
    result = subprocess.run(
        cmd, shell=isinstance(cmd, str), capture_output=True, text=True, **kwargs
    )
    if result.returncode != 0:
        print(f"  WARNING: exit {result.returncode}", flush=True)
        if result.stderr:
            print(f"  stderr: {result.stderr[:500]}", flush=True)
    return result


def is_system_preinstall(cmd):
    """Check if a pre_install command is system-level."""
    return any(kw in cmd for kw in ("apt-get", "wget", "QHULL", "/testbed/build"))


def copy_pip_wheel_cache():
    """Copy all wheels from pip's internal cache to our cache directory."""
    pip_cache = Path("/root/.cache/pip/wheels")
    if not pip_cache.exists():
        return
    count = 0
    for whl in pip_cache.rglob("*.whl"):
        dest = Path(PIP_CACHE_DIR) / whl.name
        if not dest.exists():
            shutil.copy2(str(whl), str(dest))
            count += 1
    if count:
        print(f"  Copied {count} wheels from pip internal cache", flush=True)


def main():
    with open(SPECS_PATH) as f:
        specs = json.load(f)

    os.makedirs(REPOS_DIR, exist_ok=True)
    os.makedirs(PIP_CACHE_DIR, exist_ok=True)
    os.makedirs(PREBUILD_DIR, exist_ok=True)

    # Load task data for environment_setup_commits
    env_commits = {}  # (repo, version) -> environment_setup_commit
    if TASKS_PATH.exists():
        with open(TASKS_PATH) as f:
            tasks = json.load(f)
        for t in tasks:
            key = (t["repo"], t["version"])
            if key not in env_commits:
                env_commits[key] = t.get("environment_setup_commit", "")
        print(f"Loaded {len(env_commits)} unique (repo, version) combos from tasks", flush=True)

    # ---- Step 1: System packages ----
    apt_commands = set()
    for rspec in specs.values():
        for vspec in rspec.get("versions", {}).values():
            for cmd in vspec.get("pre_install", []):
                if "apt-get" in cmd:
                    apt_commands.add(cmd)

    if apt_commands:
        print("\n=== Step 1: Installing system packages ===", flush=True)
        for cmd in sorted(apt_commands):
            run(cmd)

    # ---- Step 2: External downloads (qhull for matplotlib) ----
    print("\n=== Step 2: Downloading external build dependencies ===", flush=True)
    qhull_needed = False
    for rspec in specs.values():
        for vspec in rspec.get("versions", {}).values():
            for cmd in vspec.get("pre_install", []):
                if "QHULL" in cmd or ("wget" in cmd and "qhull" in cmd.lower()):
                    qhull_needed = True
                    break

    if qhull_needed:
        run("mkdir -p /testbed/build")
        result = run(
            'wget -q -O /tmp/qhull-2020-src-8.0.2.tgz '
            '"http://www.qhull.org/download/qhull-2020-src-8.0.2.tgz"'
        )
        if result.returncode == 0:
            run("tar -xzf /tmp/qhull-2020-src-8.0.2.tgz -C /testbed/build")
            run("rm -f /tmp/qhull-2020-src-8.0.2.tgz")
        else:
            print("  WARNING: qhull download failed, will retry at runtime", flush=True)

    # ---- Step 3: Clone all repos ----
    print("\n=== Step 3: Cloning repositories ===", flush=True)
    cloned = set()
    for repo_full in sorted(specs.keys()):
        repo_name = repo_full.split("/")[-1]
        if repo_name in cloned:
            continue
        dest = f"{REPOS_DIR}/{repo_name}"
        print(f"\nCloning {repo_full} -> {dest}", flush=True)
        run(["git", "clone", f"https://github.com/{repo_full}.git", dest])
        run(["git", "config", "--global", "--add", "safe.directory", dest])
        cloned.add(repo_name)

    print(f"\nCloned {len(cloned)} repositories", flush=True)

    # ---- Step 4: Build compiled wheels and pre-install packages ----
    print("\n=== Step 4: Building compiled wheels and installing packages ===", flush=True)

    installed_envs = {}  # conda_env -> set of package specs
    prepped_envs = set()  # conda envs that have been prepped for old Python

    for repo_full, rspec in sorted(specs.items()):
        for ver, vspec in sorted(rspec.get("versions", {}).items()):
            python_ver = vspec.get("python", "3.9")
            conda_env = f"py{python_ver.replace('.', '')}"
            pip_packages = vspec.get("pip_packages", [])

            all_packages = ["pytest", "wheel"] + pip_packages

            if conda_env not in installed_envs:
                installed_envs[conda_env] = set()

            new_packages = [p for p in all_packages if p not in installed_envs[conda_env]]
            if not new_packages:
                continue

            # For old Python envs (3.6, 3.7), downgrade setuptools so old
            # setup.py files work (e.g. MarkupSafe 1.0 needs Feature from setuptools)
            if conda_env not in prepped_envs and python_ver in ("3.6", "3.7"):
                print(f"\n  Prepping {conda_env}: downgrading setuptools for old Python", flush=True)
                run(f'conda run -n {conda_env} pip install "setuptools<45" wheel')
                prepped_envs.add(conda_env)

            print(
                f"\n--- {repo_full} v{ver} ({conda_env}): "
                f"{len(new_packages)} packages ---",
                flush=True,
            )

            # For old Python (3.6, 3.7): install ONE BY ONE so a single
            # failure doesn't block all packages (e.g. MarkupSafe 1.0)
            # For modern Python: install all at once (faster, no issues)
            if python_ver in ("3.6", "3.7"):
                for pkg in new_packages:
                    result = run(f'conda run -n {conda_env} pip install "{pkg}"')
                    if result.returncode != 0:
                        print(f"  SKIP (install failed): {pkg}", flush=True)
                        continue
                    run(
                        f'conda run -n {conda_env} pip wheel '
                        f'--no-deps --wheel-dir {PIP_CACHE_DIR} "{pkg}"'
                    )
            else:
                pip_list = " ".join(f'"{p}"' for p in new_packages)
                print("  Installing into conda env...", flush=True)
                run(f"conda run -n {conda_env} pip install {pip_list}")
                print("  Building wheels...", flush=True)
                run(
                    f"conda run -n {conda_env} pip wheel "
                    f"--wheel-dir {PIP_CACHE_DIR} {pip_list}"
                )

            installed_envs[conda_env].update(all_packages)

    # Copy any additional wheels from pip's internal cache
    copy_pip_wheel_cache()

    # ---- Step 5: Pre-build projects that have C extensions ----
    # Only prebuild repos where `pip install -e .` compiles C/Cython code.
    # Pure Python repos (django, sympy, flask, etc.) install in seconds and
    # don't need prebuilds. This cuts build time from ~50min to ~10min.
    PREBUILD_REPOS = {"astropy/astropy", "matplotlib/matplotlib", "scikit-learn/scikit-learn"}
    print("\n=== Step 5: Pre-building C-extension projects ===", flush=True)

    for repo_full, rspec in sorted(specs.items()):
        if repo_full not in PREBUILD_REPOS:
            continue
        repo_name = repo_full.split("/")[-1]
        base_repo = f"{REPOS_DIR}/{repo_name}"
        if not os.path.isdir(base_repo):
            continue

        for ver, vspec in sorted(rspec.get("versions", {}).items()):
            python_ver = vspec.get("python", "3.9")
            conda_env = f"py{python_ver.replace('.', '')}"
            install_cmd = vspec.get("install", "python -m pip install -e .")
            pre_install = vspec.get("pre_install", [])

            # Sanitize version for directory name
            ver_safe = ver.replace(".", "_")
            build_dir = f"{PREBUILD_DIR}/{repo_name}_{ver_safe}"

            print(f"\n--- Pre-building {repo_full} v{ver} -> {build_dir} ---", flush=True)

            # Clone from local repo (hardlinks .git objects to save space)
            run(["git", "clone", "--local", base_repo, build_dir])
            run(["git", "config", "--global", "--add", "safe.directory", build_dir])

            # Checkout environment_setup_commit if available
            env_commit = env_commits.get((repo_full, ver), "")
            if env_commit:
                print(f"  Checking out env_commit: {env_commit[:12]}", flush=True)
                run(["git", "checkout", env_commit], cwd=build_dir)

            # Run non-system pre_install commands (sed patches etc.)
            for cmd in pre_install:
                if not is_system_preinstall(cmd):
                    run(cmd, cwd=build_dir)

            # Add --find-links to install cmd to use cached wheels
            if "pip install" in install_cmd:
                install_cmd_cached = install_cmd.replace(
                    "pip install",
                    f"pip install --find-links {PIP_CACHE_DIR}",
                    1,
                )
            else:
                install_cmd_cached = install_cmd

            # Run the project install (compiles C extensions)
            print(f"  Running: {install_cmd_cached[:120]}", flush=True)
            run(f"conda run -n {conda_env} {install_cmd_cached}", cwd=build_dir)

    # Copy any new wheels generated during project installs
    copy_pip_wheel_cache()

    # Show final sizes
    print("\n=== Pre-bake complete! ===", flush=True)
    result = run(f"du -sh {REPOS_DIR} {PIP_CACHE_DIR} {PREBUILD_DIR}")
    if result.stdout:
        print(result.stdout, flush=True)

    whl_count = len(list(Path(PIP_CACHE_DIR).glob("*.whl")))
    tar_count = len(list(Path(PIP_CACHE_DIR).glob("*.tar.gz")))
    prebuild_count = len(
        [d for d in os.listdir(PREBUILD_DIR) if os.path.isdir(f"{PREBUILD_DIR}/{d}")]
    )
    print(
        f"  Wheels: {whl_count}, Source dists: {tar_count}, Prebuilds: {prebuild_count}",
        flush=True,
    )


if __name__ == "__main__":
    main()
