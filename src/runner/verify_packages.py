"""
Fail-fast verification of project packages for Cluster-CI.

R1: Specialized runtime images (vllm, nemo) - verifies imported version = image version
    for image packages (no masking), and imported = installed for absent packages.
R2: Generic images (pytorch, python) - verifies imported = installed for user packages,
    and imported = image version for pinned core packages.

Ensures that packages are verified both in-process and in real stage execution conditions:
  1) Condition 1: env -u PYTHONPATH
  2) Condition 2: PYTHONPATH=. with cwd=workspace_dir

Fails loudly with non-zero exit code if any package fails verification.
"""

import importlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple, Set


def get_importable_modules(dist: Optional[importlib.metadata.Distribution], pkg_name: str) -> List[str]:
    """
    Determines the top-level Python importable module names for a given distribution.
    Attempts top_level.txt, then packages_distributions(), then normalized pkg_name.
    """
    modules: List[str] = []
    if dist is not None:
        try:
            top_level = dist.read_text("top_level.txt")
            if top_level:
                for line in top_level.splitlines():
                    name = line.strip().split("/")[0]
                    if name and not name.startswith("#") and name not in modules:
                        modules.append(name)
        except Exception:
            pass

    if not modules:
        try:
            mapping = importlib.metadata.packages_distributions()
            norm = re.sub(r"[-_.]+", "-", pkg_name).lower()
            for mod, dists in mapping.items():
                for d in dists:
                    if re.sub(r"[-_.]+", "-", d).lower() == norm:
                        modules.append(mod)
        except Exception:
            pass

    if not modules:
        modules = [re.sub(r"[-_.]+", "_", pkg_name)]

    public_mods = [m for m in modules if not m.startswith("_")]
    return public_mods if public_mods else modules


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
    forbidden_prefixes: Optional[Dict[str, str]] = None,
    import_modules: bool = True,
) -> List[str]:
    """
    Runs a subprocess with specified env and cwd to verify active package versions,
    ensure image packages are not shadowed by user-site in R1, and verify that modules
    can actually be imported without runtime or dependency errors.
    """
    child_code = """
import sys
import json
import importlib
import importlib.metadata
import re

data = json.loads(sys.stdin.read())
expected = data.get("expected", {})
forbidden = data.get("forbidden", {})
import_modules = data.get("import_modules", True)
mismatches = []

def get_importable_modules(dist, pkg_name):
    modules = []
    if dist is not None:
        try:
            top_level = dist.read_text("top_level.txt")
            if top_level:
                for line in top_level.splitlines():
                    name = line.strip().split("/")[0]
                    if name and not name.startswith("#") and name not in modules:
                        modules.append(name)
        except Exception:
            pass
    if not modules:
        try:
            mapping = importlib.metadata.packages_distributions()
            norm = re.sub(r"[-_.]+", "-", pkg_name).lower()
            for mod, dists in mapping.items():
                for d in dists:
                    if re.sub(r"[-_.]+", "-", d).lower() == norm:
                        modules.append(mod)
        except Exception:
            pass
    if not modules:
        modules = [re.sub(r"[-_.]+", "_", pkg_name)]
    public_mods = [m for m in modules if not m.startswith("_")]
    return public_mods if public_mods else modules

for pkg_name, exp_ver in expected.items():
    try:
        active_dist = importlib.metadata.distribution(pkg_name)
        active_ver = active_dist.version
        active_loc = str(getattr(active_dist, "_path", getattr(active_dist, "locate_file", lambda f: "")("")))
        if active_ver != exp_ver:
            mismatches.append(
                f"Package '{pkg_name}': expected version '{exp_ver}', but imported version '{active_ver}' from {active_loc}"
            )
        elif pkg_name in forbidden:
            bad_prefix = forbidden[pkg_name]
            if bad_prefix in active_loc:
                mismatches.append(
                    f"Package '{pkg_name}': expected from image environment, but imported from user-site '{active_loc}' (masking violation)"
                )
        elif import_modules:
            targets = get_importable_modules(active_dist, pkg_name)
            for mod in targets:
                try:
                    importlib.import_module(mod)
                except Exception as exc:
                    mismatches.append(
                        f"Package '{pkg_name}': module '{mod}' failed real import: {type(exc).__name__}: {exc}"
                    )
                    break
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
    payload = {
        "expected": expected_pkgs,
        "forbidden": forbidden_prefixes or {},
        "import_modules": import_modules,
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-c", child_code],
            input=json.dumps(payload),
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
    base_dir: str = "/home/user/.local",
    import_modules: bool = True,
) -> int:
    """
    Verifies that all distributions match their active imported versions according to R1/R2 rules.
    Checks in-process and optionally verifies dual-stage conditions via subprocesses:
      1) Condition 1: env -u PYTHONPATH
      2) Condition 2: PYTHONPATH=. with cwd=workspace_dir
    Returns 0 on success, 1 on failure.
    """
    if pythonpath is None:
        pythonpath = os.environ.get("PYTHONPATH", "")

    project_paths = [p for p in pythonpath.split(":") if p]
    if not project_paths:
        project_paths = discover_project_paths(base_dir)

    # 1. Discover user-installed distributions
    user_distributions: Dict[str, Tuple[str, str, str]] = {}
    for pdir in project_paths:
        if not os.path.isdir(pdir):
            continue
        for dist in importlib.metadata.distributions(path=[pdir]):
            if not dist.name:
                continue
            norm_name = dist.name.lower().replace("-", "_")
            if norm_name not in user_distributions:
                loc = str(getattr(dist, "_path", getattr(dist, "locate_file", lambda f: "")("")))
                user_distributions[norm_name] = (dist.name, dist.version, loc)

    # 2. Discover image distributions (all distributions outside user base)
    image_distributions: Dict[str, Tuple[str, str, str]] = {}
    for dist in importlib.metadata.distributions():
        if not dist.name:
            continue
        loc = str(getattr(dist, "_path", getattr(dist, "locate_file", lambda f: "")("")))
        if base_dir not in loc:
            norm_name = dist.name.lower().replace("-", "_")
            if norm_name not in image_distributions:
                image_distributions[norm_name] = (dist.name, dist.version, loc)

    # 3. Detect runtime mode: R1 (Specialized) vs R2 (Generic)
    is_r1 = any(k in image_distributions for k in ("vllm", "nemo_automodel", "nemo"))

    expected_pkgs: Dict[str, str] = {}
    forbidden_prefixes: Dict[str, str] = {}

    if is_r1:
        # In R1: Image environment WINS entirely.
        # Key image distributions must be resolved from the image (no masking by user-site).
        # Absent distributions installed in user-site must resolve to user-installed versions.
        # Check declared project dependencies if pyproject.toml or requirements.txt exists
        pyproj_path = os.path.join(workspace_dir if os.path.isdir(workspace_dir) else os.getcwd(), "pyproject.toml")
        project_dep_names: Set[str] = set()
        if os.path.isfile(pyproj_path):
            try:
                import tomllib
                with open(pyproj_path, "rb") as f:
                    data = tomllib.load(f)
                for d in data.get("project", {}).get("dependencies", []):
                    m = re.match(r"^([a-zA-Z0-9_\-\.]+)", d.strip())
                    if m:
                        project_dep_names.add(m.group(1).lower().replace("-", "_"))
            except Exception:
                pass

        req_path = os.path.join(workspace_dir if os.path.isdir(workspace_dir) else os.getcwd(), "requirements.txt")
        if os.path.isfile(req_path):
            try:
                with open(req_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line_s = line.strip()
                        if not line_s or line_s.startswith("#") or line_s.startswith("-"):
                            continue
                        line_s = line_s.split("#")[0].strip()
                        m = re.match(r"^([a-zA-Z0-9_\-\.]+)", line_s)
                        if m:
                            project_dep_names.add(m.group(1).lower().replace("-", "_"))
            except Exception:
                pass

        # For every package declared by project or already in image, verify image version
        for norm_name, (pkg_name, img_ver, _) in image_distributions.items():
            if norm_name in project_dep_names or norm_name in ("transformers", "huggingface_hub", "vllm", "nemo_automodel", "torch"):
                expected_pkgs[pkg_name] = img_ver
                forbidden_prefixes[pkg_name] = base_dir

        # For absent packages in user site:
        for norm_name, (pkg_name, user_ver, _) in user_distributions.items():
            if norm_name not in image_distributions:
                expected_pkgs[pkg_name] = user_ver
            else:
                # If package is present in both, image WINS in R1!
                expected_pkgs[pkg_name] = image_distributions[norm_name][1]
                forbidden_prefixes[pkg_name] = base_dir

    else:
        # In R2 (Generic):
        # User-installed distributions take priority.
        for norm_name, (pkg_name, user_ver, _) in user_distributions.items():
            expected_pkgs[pkg_name] = user_ver

        # Pinned core packages must match image versions
        PINNED = {"torch", "torchvision", "torchaudio", "triton", "xformers"}
        for norm_name, (pkg_name, img_ver, _) in image_distributions.items():
            if norm_name in PINNED or norm_name.startswith("nvidia_"):
                if norm_name not in expected_pkgs:
                    expected_pkgs[pkg_name] = img_ver

    if not expected_pkgs and not user_distributions:
        print("ℹ️ [Cluster-CI] No project distributions to verify. Skipping package verification.")
        return 0

    mismatches: List[str] = []
    verified_count = 0

    for pkg_name, exp_ver in sorted(expected_pkgs.items()):
        try:
            active_dist = importlib.metadata.distribution(pkg_name)
            active_ver = active_dist.version
            active_loc = str(getattr(active_dist, "_path", getattr(active_dist, "locate_file", lambda f: "")("")))

            if active_ver != exp_ver:
                mismatches.append(
                    f"Package '{pkg_name}': expected version '{exp_ver}', but imported version '{active_ver}' from {active_loc}"
                )
            elif pkg_name in forbidden_prefixes and forbidden_prefixes[pkg_name] in active_loc:
                mismatches.append(
                    f"Package '{pkg_name}': expected from image environment, but imported from user-site '{active_loc}' (masking violation)"
                )
            elif import_modules:
                targets = get_importable_modules(active_dist, pkg_name)
                import_failed = False
                for mod in targets:
                    try:
                        importlib.import_module(mod)
                    except Exception as exc:
                        mismatches.append(
                            f"Package '{pkg_name}': module '{mod}' failed real import: {type(exc).__name__}: {exc}"
                        )
                        import_failed = True
                        break
                if not import_failed:
                    verified_count += 1
            else:
                verified_count += 1
        except importlib.metadata.PackageNotFoundError:
            mismatches.append(
                f"Package '{pkg_name}': expected version '{exp_ver}', but package was not found in active environment"
            )
        except Exception as exc:
            mismatches.append(f"Package '{pkg_name}': verification failed with exception: {exc}")

    if mismatches:
        sys.stderr.write(f"❌ [Cluster-CI] FAIL-FAST: {len(mismatches)} package verification failure(s) detected!\n")
        for m in mismatches:
            sys.stderr.write(f"  - {m}\n")
        return 1

    # If subprocess checks requested, verify real stage execution conditions
    if check_subprocesses:
        work_dir = workspace_dir if os.path.isdir(workspace_dir) else os.getcwd()

        # Condition 1: PYTHONPATH unset
        env1 = dict(os.environ)
        env1.pop("PYTHONPATH", None)
        sub_mismatches_c1 = _run_subprocess_check("PYTHONPATH unset", env1, work_dir, expected_pkgs, forbidden_prefixes, import_modules=import_modules)
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
        sub_mismatches_c2 = _run_subprocess_check("PYTHONPATH=.", env2, work_dir, expected_pkgs, forbidden_prefixes, import_modules=import_modules)
        if sub_mismatches_c2:
            sys.stderr.write(
                f"❌ [Cluster-CI] FAIL-FAST: {len(sub_mismatches_c2)} package version mismatch(es) detected under condition 'PYTHONPATH=.'!\n"
            )
            for m in sub_mismatches_c2:
                sys.stderr.write(f"  - {m}\n")
            return 1

        mode_str = "R1 (Specialized Image)" if is_r1 else "R2 (Generic Image)"
        print(
            f"✅ [Cluster-CI] All {verified_count} packages verified under {mode_str} in both PYTHONPATH unset and PYTHONPATH=. conditions."
        )
    else:
        print(f"✅ [Cluster-CI] All {verified_count} packages verified (imported version matches expected version).")

    return 0


if __name__ == "__main__":
    pp = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(verify_packages(pp, check_subprocesses=True))
