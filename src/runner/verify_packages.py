"""
Fail-fast verification of project packages for Cluster-CI.

Ensures that every Python distribution installed by the project in the user
prefix (/home/user/.local) is importable and that its resolved/imported version
matches the version installed in /home/user/.local.

Fails loudly with non-zero exit code if any package installed by the project
is shadowed by an older container package or fails to import.
"""

import importlib.metadata
import json
import os
import subprocess
import sys
from typing import Dict, List, Optional, Tuple


def discover_project_paths(base_dir: str = "/home/user/.local") -> List[str]:
    """Find site-packages and dist-packages directories under base_dir (excluding uv tools)."""
    paths: List[str] = []
    if not os.path.exists(base_dir):
        return paths

    for root, dirs, _ in os.walk(base_dir):
        if "share/uv" in root:
            continue
        for d in dirs:
            if d in ("site-packages", "dist-packages"):
                full_p = os.path.join(root, d)
                if full_p not in paths:
                    paths.append(full_p)
    paths.sort()
    return paths


def _run_subprocess_check(
    condition_name: str,
    env_override: Dict[str, str],
    cwd: str,
    expected_pkgs: Dict[str, str],
) -> List[str]:
    """
    Runs a subprocess with specified env and cwd to verify active package versions.
    """
    child_code = """
import sys
import json
import importlib.metadata

expected = json.loads(sys.stdin.read())
mismatches = []

for pkg_name, exp_ver in expected.items():
    try:
        active_dist = importlib.metadata.distribution(pkg_name)
        active_ver = active_dist.version
        active_loc = getattr(active_dist, "_path", getattr(active_dist, "locate_file", lambda f: "")(""))
        if active_ver != exp_ver:
            mismatches.append(
                f"Package '{pkg_name}': expected version '{exp_ver}', but imported version '{active_ver}' from {active_loc}"
            )
    except importlib.metadata.PackageNotFoundError:
        mismatches.append(
            f"Package '{pkg_name}': expected version '{exp_ver}', but package was not found in active environment"
        )
    except Exception as exc:
        mismatches.append(f"Package '{pkg_name}': verification failed with exception: {exc}")

if mismatches:
    print(json.dumps(mismatches))
    sys.exit(1)
sys.exit(0)
"""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", child_code],
            input=json.dumps(expected_pkgs),
            text=True,
            capture_output=True,
            cwd=cwd,
            env=env_override,
            timeout=30,
        )
    except Exception as exc:
        return [f"Condition '{condition_name}': subprocess failed to execute: {exc}"]

    if proc.returncode != 0:
        try:
            return json.loads(proc.stdout)
        except Exception:
            err = proc.stderr.strip() or proc.stdout.strip()
            return [f"Condition '{condition_name}': verification process failed (code {proc.returncode}): {err}"]

    return []


def verify_packages(
    pythonpath: Optional[str] = None,
    check_subprocesses: bool = False,
    workspace_dir: str = "/workspace",
) -> int:
    """
    Verifies that all distributions installed in project_paths match their active imported versions.
    Checks in-process and optionally verifies dual-stage conditions via subprocesses:
      1) Condition 1: env -u PYTHONPATH
      2) Condition 2: PYTHONPATH=. with cwd=workspace_dir
    Returns 0 on success, 1 on failure.
    """
    if pythonpath is None:
        pythonpath = os.environ.get("PYTHONPATH", "")

    project_paths = [p for p in pythonpath.split(":") if p]
    if not project_paths:
        project_paths = discover_project_paths()

    if not project_paths:
        print("ℹ️ [Cluster-CI] No project package paths found under /home/user/.local. Skipping package verification.")
        return 0

    # Collect unique distributions installed in project_paths.
    # The order of project_paths determines priority (first occurrence wins).
    seen_distributions: Dict[str, Tuple[str, str, str]] = {}
    for pdir in project_paths:
        if not os.path.isdir(pdir):
            continue
        for dist in importlib.metadata.distributions(path=[pdir]):
            if not dist.name:
                continue
            norm_name = dist.name.lower().replace("-", "_")
            if norm_name not in seen_distributions:
                seen_distributions[norm_name] = (
                    dist.name,
                    dist.version,
                    getattr(dist, "_path", getattr(dist, "locate_file", lambda f: "")("")),
                )

    if not seen_distributions:
        print(f"ℹ️ [Cluster-CI] No installed distributions found in {project_paths}.")
        return 0

    mismatches: List[str] = []
    verified_count = 0

    for norm_name, (pkg_name, exp_ver, exp_loc) in sorted(seen_distributions.items()):
        try:
            active_dist = importlib.metadata.distribution(pkg_name)
            active_ver = active_dist.version
            active_loc = getattr(active_dist, "_path", getattr(active_dist, "locate_file", lambda f: "")(""))

            if active_ver != exp_ver:
                mismatches.append(
                    f"Package '{pkg_name}': expected version '{exp_ver}' from {exp_loc}, "
                    f"but imported version '{active_ver}' from {active_loc}"
                )
            else:
                verified_count += 1
        except importlib.metadata.PackageNotFoundError:
            mismatches.append(
                f"Package '{pkg_name}': expected version '{exp_ver}' from {exp_loc}, "
                f"but package was not found in active environment"
            )
        except Exception as exc:
            mismatches.append(f"Package '{pkg_name}': verification failed with exception: {exc}")

    if mismatches:
        sys.stderr.write(f"❌ [Cluster-CI] FAIL-FAST: {len(mismatches)} package version mismatch(es) detected!\n")
        for m in mismatches:
            sys.stderr.write(f"  - {m}\n")
        return 1

    # If subprocess checks requested, verify real stage execution conditions
    if check_subprocesses:
        expected_dict = {pkg_name: exp_ver for _, (pkg_name, exp_ver, _) in seen_distributions.items()}
        work_dir = workspace_dir if os.path.isdir(workspace_dir) else os.getcwd()

        # Condition 1: PYTHONPATH unset
        env1 = dict(os.environ)
        env1.pop("PYTHONPATH", None)
        sub_mismatches_c1 = _run_subprocess_check("PYTHONPATH unset", env1, work_dir, expected_dict)
        if sub_mismatches_c1:
            sys.stderr.write(
                f"❌ [Cluster-CI] FAIL-FAST: {len(sub_mismatches_c1)} package version mismatch(es) detected under condition 'PYTHONPATH unset'!\n"
            )
            for m in sub_mismatches_c1:
                sys.stderr.write(f"  - {m}\n")
            return 1

        # Condition 2: PYTHONPATH=. with cwd=workspace_dir
        env2 = dict(os.environ)
        env2["PYTHONPATH"] = "."
        sub_mismatches_c2 = _run_subprocess_check("PYTHONPATH=.", env2, work_dir, expected_dict)
        if sub_mismatches_c2:
            sys.stderr.write(
                f"❌ [Cluster-CI] FAIL-FAST: {len(sub_mismatches_c2)} package version mismatch(es) detected under condition 'PYTHONPATH=.'!\n"
            )
            for m in sub_mismatches_c2:
                sys.stderr.write(f"  - {m}\n")
            return 1

        print(
            f"✅ [Cluster-CI] All {verified_count} project packages verified under both PYTHONPATH unset and PYTHONPATH=. conditions."
        )
    else:
        print(f"✅ [Cluster-CI] All {verified_count} project packages verified (imported version matches installed version).")

    return 0


if __name__ == "__main__":
    pp = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(verify_packages(pp, check_subprocesses=True))
