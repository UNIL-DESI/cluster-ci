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

import argparse
import importlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple, Set

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def get_importable_modules(dist: Optional[importlib.metadata.Distribution], pkg_name: str) -> List[str]:
    """
    Determines the top-level Python importable module names for a given distribution.
    Prioritizes distribution top_level.txt and official packages_distributions() mapping,
    retaining only primary top-level modules corresponding to the package name or declared modules,
    without importing standalone utility scripts (e.g. rec2idx.py in nvidia-dali).
    """
    modules: List[str] = []

    # 1. Distribution top_level.txt if dist is provided
    if dist is not None:
        try:
            top_level = dist.read_text("top_level.txt")
            if top_level:
                for line in top_level.splitlines():
                    name = line.strip().split("/")[0].split("\\")[0]
                    if name and not name.startswith("#") and not name.startswith("_") and name not in modules:
                        modules.append(name)
        except Exception:
            pass

    # 2. Official PEP 566 importlib.metadata.packages_distributions mapping
    if not modules:
        try:
            mapping = importlib.metadata.packages_distributions()
            norm = re.sub(r"[-_.]+", "-", pkg_name).lower()
            for mod, dists in mapping.items():
                if not mod or mod.startswith("_"):
                    continue
                clean_mod = mod.split("/")[0].split("\\")[0]
                for d in dists:
                    if re.sub(r"[-_.]+", "-", d).lower() == norm:
                        if clean_mod not in modules and not clean_mod.startswith("_"):
                            modules.append(clean_mod)
        except Exception:
            pass

    # 3. Fallback to normalized package name
    if not modules:
        modules = [re.sub(r"[-_.]+", "_", pkg_name)]

    public_mods = [m for m in modules if not m.startswith("_")]
    candidate_mods = public_mods if public_mods else modules

    # 4. Filter out standalone utility scripts that do not match the package name or its parts.
    # If multiple candidates exist, retain only modules that correspond to the package name or its constituents.
    norm_pkg = re.sub(r"[-_.]+", "_", pkg_name).lower()
    pkg_parts = set(norm_pkg.split("_"))
    matching = [
        m for m in candidate_mods
        if (
            m.lower() == norm_pkg
            or m.lower() in pkg_parts
            or norm_pkg.startswith(m.lower())
            or m.lower().startswith(norm_pkg)
        )
    ]
    if matching:
        return matching

    return candidate_mods


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
import os

data = json.loads(sys.stdin.read())
expected = data.get("expected", {})
forbidden = data.get("forbidden", {})
import_modules = data.get("import_modules", True)
work_dir = data.get("work_dir", "")
if work_dir and os.path.isdir(work_dir) and work_dir not in sys.path:
    sys.path.insert(0, work_dir)

mismatches = []

def get_importable_modules(dist, pkg_name):
    modules = []
    try:
        mapping = importlib.metadata.packages_distributions()
        norm = re.sub(r"[-_.]+", "-", pkg_name).lower()
        for mod, dists in mapping.items():
            if not mod or mod.startswith("_"):
                continue
            for d in dists:
                if re.sub(r"[-_.]+", "-", d).lower() == norm:
                    if mod not in modules:
                        modules.append(mod)
    except Exception:
        pass

    if not modules and dist is not None:
        try:
            top_level = dist.read_text("top_level.txt")
            if top_level:
                for line in top_level.splitlines():
                    name = line.strip().split("/")[0]
                    if name and not name.startswith("#") and not name.startswith("_") and name not in modules:
                        modules.append(name)
        except Exception:
            pass

    if not modules:
        modules = [re.sub(r"[-_.]+", "_", pkg_name)]

    public_mods = [m for m in modules if not m.startswith("_")]
    candidate_mods = public_mods if public_mods else modules

    norm_pkg = re.sub(r"[-_.]+", "_", pkg_name).lower()
    pkg_parts = set(norm_pkg.split("_"))
    matching = [
        m for m in candidate_mods
        if (
            m.lower() == norm_pkg
            or m.lower() in pkg_parts
            or norm_pkg.startswith(m.lower())
            or m.lower().startswith(norm_pkg)
        )
    ]
    if matching:
        return matching
    return candidate_mods

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
        "work_dir": cwd,
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
    packages_to_verify: Optional[List[str]] = None,
) -> int:
    """
    Verifies that all project dependencies match their active imported versions according to R1/R2 rules.
    Checks in-process and optionally verifies dual-stage conditions via subprocesses:
      1) Condition 1: env -u PYTHONPATH
      2) Condition 2: PYTHONPATH=. with cwd=workspace_dir
    Returns 0 on success, 1 on failure.
    """
    target_work_dir: Optional[str] = None
    if workspace_dir and os.path.isdir(workspace_dir):
        target_work_dir = workspace_dir
    elif os.path.isdir("/workspace"):
        target_work_dir = "/workspace"
    elif "CLUSTER_CI_WORKSPACE" in os.environ and os.path.isdir(os.environ["CLUSTER_CI_WORKSPACE"]):
        target_work_dir = os.environ["CLUSTER_CI_WORKSPACE"]

    if target_work_dir and target_work_dir not in sys.path:
        sys.path.insert(0, target_work_dir)

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

    # 4. Parse declared dependencies of the submitted repository
    project_dep_names: Set[str] = set()
    project_pkg_norm: Optional[str] = None

    if target_work_dir:
        pyproj_path = os.path.join(target_work_dir, "pyproject.toml")
        if os.path.isfile(pyproj_path):
            try:
                import tomllib
                with open(pyproj_path, "rb") as f:
                    data = tomllib.load(f)
                p_name = data.get("project", {}).get("name")
                if p_name:
                    project_pkg_norm = re.sub(r"[-_.]+", "_", p_name).lower()
                for d in data.get("project", {}).get("dependencies", []):
                    m = re.match(r"^([a-zA-Z0-9_\-\.]+)", d.strip())
                    if m:
                        project_dep_names.add(m.group(1).lower().replace("-", "_"))
            except Exception:
                pass

        req_path = os.path.join(target_work_dir, "requirements.txt")
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

    # 5. Identify the repository package itself (installed in editable mode or matching pyproject project.name).
    # Rationale: verify_packages audits third-party runtime dependencies (R1-R5). The submitted repo itself
    # is the code under test, executed and verified by stage pipelines (DVC repro). It is excluded from third-party
    # dependency verification to prevent false alarms on internal scripts/modules, while work_dir is added to sys.path.
    editable_pkg_norms: Set[str] = set()
    if project_pkg_norm:
        editable_pkg_norms.add(project_pkg_norm)

    for norm_name, (pkg_name, _, loc) in user_distributions.items():
        if norm_name == project_pkg_norm:
            editable_pkg_norms.add(norm_name)
            continue
        try:
            dist = importlib.metadata.distribution(pkg_name)
            read_fn = getattr(dist, "read_text", None)
            if not callable(read_fn):
                continue
            direct_url_text = read_fn("direct_url.json")
            if not direct_url_text:
                continue
            data = json.loads(direct_url_text)
            dir_info = (
                data.get("dir_info", {})
                if isinstance(data, dict) and isinstance(data.get("dir_info"), dict)
                else {}
            )
            is_editable = bool(dir_info.get("editable")) or "editable" in direct_url_text
            url_str = data.get("url", "") if isinstance(data, dict) else ""
            matches_work_dir = False
            if target_work_dir:
                tw_norm = target_work_dir.replace("\\", "/")
                matches_work_dir = (
                    target_work_dir in direct_url_text
                    or tw_norm in direct_url_text.replace("\\", "/")
                    or tw_norm in url_str.replace("\\", "/")
                )
            if is_editable or matches_work_dir:
                editable_pkg_norms.add(norm_name)
        except FileNotFoundError:
            # direct_url.json absent; normal for standard non-editable distributions
            pass
        except json.JSONDecodeError as exc:
            sys.stderr.write(
                f"⚠️ [Cluster-CI] Invalid direct_url.json for package '{pkg_name}': {exc}\n"
            )

    # If caller requested specific packages via CLI, restrict declared dependencies
    if packages_to_verify:
        project_dep_names = {p.lower().replace("-", "_") for p in packages_to_verify}

    expected_pkgs: Dict[str, str] = {}
    forbidden_prefixes: Dict[str, str] = {}

    if is_r1:
        # In R1: Image environment WINS entirely.
        for norm_name, (pkg_name, img_ver, _) in image_distributions.items():
            if (
                norm_name in project_dep_names
                or norm_name in ("transformers", "huggingface_hub", "vllm", "nemo_automodel", "nemo", "torch")
            ):
                expected_pkgs[pkg_name] = img_ver
                forbidden_prefixes[pkg_name] = base_dir

        for norm_name, (pkg_name, user_ver, _) in user_distributions.items():
            if norm_name in editable_pkg_norms:
                continue
            if norm_name not in image_distributions:
                if not project_dep_names or norm_name in project_dep_names:
                    expected_pkgs[pkg_name] = user_ver
            else:
                expected_pkgs[pkg_name] = image_distributions[norm_name][1]
                forbidden_prefixes[pkg_name] = base_dir

    else:
        # In R2 (Generic):
        # Only verify user-installed distributions corresponding to declared dependencies (or all user dists if none declared)
        for norm_name, (pkg_name, user_ver, _) in user_distributions.items():
            if norm_name in editable_pkg_norms:
                continue
            if not project_dep_names or norm_name in project_dep_names:
                expected_pkgs[pkg_name] = user_ver

        # Pinned core packages must match image versions. NEVER auto-include preinstalled packages by nvidia_ prefix.
        PINNED = {"torch", "torchvision", "torchaudio", "triton", "xformers"}
        for norm_name, (pkg_name, img_ver, _) in image_distributions.items():
            if norm_name in PINNED:
                if norm_name in project_dep_names or norm_name in user_distributions or not project_dep_names:
                    if pkg_name not in expected_pkgs:
                        expected_pkgs[pkg_name] = img_ver

    # 6. Fail-fast if declared dependencies are missing entirely from the environment
    if project_dep_names:
        missing_declared = []
        for dep_norm in sorted(project_dep_names):
            if dep_norm in editable_pkg_norms:
                continue
            # Check if resolved in expected_pkgs
            matched = any(re.sub(r"[-_.]+", "_", p).lower() == dep_norm for p in expected_pkgs)
            if not matched:
                try:
                    d = importlib.metadata.distribution(dep_norm)
                    expected_pkgs[d.name] = d.version
                except importlib.metadata.PackageNotFoundError:
                    missing_declared.append(dep_norm)

        if missing_declared:
            sys.stderr.write(
                f"❌ [Cluster-CI] FAIL-FAST: {len(missing_declared)} declared project dependency/dependencies missing from environment: {missing_declared}\n"
            )
            return 1

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
        sub_cwd = target_work_dir if target_work_dir else os.getcwd()
        # Condition 1: PYTHONPATH unset
        env1 = dict(os.environ)
        env1.pop("PYTHONPATH", None)
        sub_mismatches_c1 = _run_subprocess_check(
            "PYTHONPATH unset", env1, sub_cwd, expected_pkgs, forbidden_prefixes, import_modules=import_modules
        )
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
        sub_mismatches_c2 = _run_subprocess_check(
            "PYTHONPATH=.", env2, sub_cwd, expected_pkgs, forbidden_prefixes, import_modules=import_modules
        )
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
    parser = argparse.ArgumentParser(description="Verify installed packages against image and declared dependencies.")
    parser.add_argument("packages", nargs="*", default=None, help="Specific packages or path to verify (optional)")
    parser.add_argument("--workspace", default="/workspace", help="Project workspace directory containing pyproject.toml / requirements.txt")
    parser.add_argument("--pythonpath", default=None, help="Explicit PYTHONPATH to check")
    parser.add_argument("--base-dir", default="/home/user/.local", help="User base directory")
    parser.add_argument("--no-subprocesses", action="store_true", help="Disable subprocess dual-stage verification")
    parser.add_argument("--no-import-modules", action="store_true", help="Disable module import verification")
    parsed_args = parser.parse_args()

    target_work_dir = parsed_args.workspace if os.path.isdir(parsed_args.workspace) else os.getcwd()
    explicit_pp = parsed_args.pythonpath
    specific_pkgs_list = None

    if parsed_args.packages:
        # Check if first positional argument is a directory or path string (legacy invocation compatibility)
        first_arg = parsed_args.packages[0]
        if len(parsed_args.packages) == 1 and (os.path.isdir(first_arg) or "/" in first_arg or ":" in first_arg or first_arg.startswith(".")):
            explicit_pp = first_arg
        else:
            specific_pkgs_list = parsed_args.packages

    sys.exit(
        verify_packages(
            pythonpath=explicit_pp,
            check_subprocesses=not parsed_args.no_subprocesses,
            workspace_dir=target_work_dir,
            base_dir=parsed_args.base_dir,
            import_modules=not parsed_args.no_import_modules,
            packages_to_verify=specific_pkgs_list,
        )
    )
