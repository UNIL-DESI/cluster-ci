import argparse
import os
import sys
import subprocess
import re
from pathlib import Path

try:
    import tomlkit
except ImportError:
    print("❌ Error: 'tomlkit' is required. Please install it with 'pip install tomlkit'.")
    sys.exit(1)

import urllib.request
import tempfile

# Force UTF-8 on Windows consoles to prevent UnicodeEncodeError
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

def log_info(msg):
    print(f"ℹ️  [Cluster-CI] {msg}")

def log_error(msg):
    print(f"❌ [Cluster-CI] {msg}")

def log_success(msg):
    print(f"✅ [Cluster-CI] {msg}")

def fetch_latest_constraints():
    url = "https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/cluster_constraints.txt"
    log_info(f"Fetching latest cluster constraints from GitHub...")
    try:
        req = urllib.request.Request(url, headers={'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(req, timeout=10) as response:
            content = response.read().decode('utf-8')
            
        # Write to a temporary file
        fd, temp_path = tempfile.mkstemp(suffix=".txt", prefix="cluster_constraints_")
        with os.fdopen(fd, 'w') as f:
            f.write(content)
        return temp_path
    except Exception as e:
        log_error(f"Failed to fetch constraints: {e}")
        return None

def check_python_version(pyproject_path, target_version="3.12"):
    with open(pyproject_path, "r") as f:
        doc = tomlkit.parse(f.read())
    
    requires_python = doc.get("project", {}).get("requires-python", "")
    if not requires_python:
        return True, None

    # Simple check for compatibility with target_version (e.g. 3.12)
    # We look for <3.11, <3.12 etc.
    # Note: A real semver check would be better but requires packaging.specifiers
    # We'll use a simple regex for common problematic patterns mentioned in the issue
    if re.search(r"<3\.(?:11|10|[0-9])", requires_python):
        return False, requires_python
    
    return True, requires_python

def check_torch_pinning(pyproject_path):
    with open(pyproject_path, "r") as f:
        doc = tomlkit.parse(f.read())
    
    dependencies = doc.get("project", {}).get("dependencies", [])
    conflicts = []
    for dep in dependencies:
        if "torch" in dep.lower() and "==" in dep:
            conflicts.append(dep)
    
    return conflicts

def fix_pyproject(pyproject_path, fix_python=False, remove_torch_pin=False):
    with open(pyproject_path, "r") as f:
        content = f.read()
        doc = tomlkit.parse(content)
    
    modified = False
    if fix_python:
        if "project" in doc and "requires-python" in doc["project"]:
            old_val = doc["project"]["requires-python"]
            doc["project"]["requires-python"] = ">=3.12"
            log_success(f"Updated requires-python: {old_val} -> >=3.12")
            modified = True

    if remove_torch_pin:
        if "project" in doc and "dependencies" in doc["project"]:
            deps = doc["project"]["dependencies"]
            new_deps = []
            for dep in deps:
                if "==" in dep:
                    parts = dep.split("==")
                    dep_name = parts[0].strip().lower()
                    if dep_name == "torch":
                        new_dep = "torch>=2.0"
                        new_deps.append(new_dep)
                        log_success(f"Relaxed dependency: {dep} -> {new_dep}")
                        modified = True
                    elif dep_name == "torchvision":
                        new_dep = "torchvision>=0.15"
                        new_deps.append(new_dep)
                        log_success(f"Relaxed dependency: {dep} -> {new_dep}")
                        modified = True
                    elif dep_name == "torchaudio":
                        new_dep = "torchaudio>=2.0"
                        new_deps.append(new_dep)
                        log_success(f"Relaxed dependency: {dep} -> {new_dep}")
                        modified = True
                    else:
                        new_deps.append(dep)
                else:
                    new_deps.append(dep)
            doc["project"]["dependencies"] = new_deps

    if modified:
        with open(pyproject_path, "w") as f:
            f.write(tomlkit.dumps(doc))
        return True
    return False

def _get_project_dep_names(pyproject_path):
    """Extract normalized dependency names from pyproject.toml."""
    try:
        with open(pyproject_path, "r") as f:
            doc = tomlkit.parse(f.read())
        deps = doc.get("project", {}).get("dependencies", [])
        names = set()
        for dep in deps:
            # Extract package name (before any version specifier)
            name = re.split(r"[><=!~\[;]", dep.strip())[0].strip()
            names.add(name.lower().replace("-", "_").replace(".", "_"))
        return names
    except Exception:
        return set()


def _filter_constraints(constraints_path, pyproject_path):
    """Create a filtered constraints file that excludes packages which are also
    direct project dependencies. This prevents false resolution failures when
    the container ships custom builds (e.g., NeMo's transformers) with different
    dependency metadata than PyPI publishes."""
    project_deps = _get_project_dep_names(pyproject_path)
    if not project_deps:
        return constraints_path

    filtered_lines = []
    excluded = []
    with open(constraints_path, "r") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                filtered_lines.append(line)
                continue
            pkg_name = re.split(r"[><=!~\[;]", stripped)[0].strip()
            normalized = pkg_name.lower().replace("-", "_").replace(".", "_")
            if normalized in project_deps:
                excluded.append(pkg_name)
                continue
            filtered_lines.append(line)

    if excluded:
        log_info(f"Excluded {len(excluded)} project deps from constraints: {', '.join(excluded)}")
        fd, filtered_path = tempfile.mkstemp(suffix=".txt", prefix="filtered_constraints_")
        with os.fdopen(fd, "w") as f:
            f.writelines(filtered_lines)
        return filtered_path

    return constraints_path


def simulate_resolution(pyproject_path, constraints_path):
    if not os.path.exists(constraints_path):
        log_info(f"No constraints file found at {constraints_path}. Skipping full simulation.")
        return True

    # Filter out project deps from constraints to avoid false conflicts
    # with custom container builds (e.g., NeMo's transformers + huggingface-hub)
    effective_constraints = _filter_constraints(constraints_path, pyproject_path)

    log_info("Simulating ARM64 resolution with uv...")
    cmd = [
        "uv", "pip", "compile",
        "--python-platform", "aarch64-unknown-linux-gnu",
        "--python", "3.12",
        "-c", effective_constraints,
        pyproject_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    # Cleanup filtered constraints if we created one
    if effective_constraints != constraints_path and os.path.exists(effective_constraints):
        os.remove(effective_constraints)

    if result.returncode != 0:
        log_error("Resolution simulation failed!")
        print(result.stderr)
        return False

    log_success("Resolution simulation passed.")
    return True

def main():
    parser = argparse.ArgumentParser(description="Cluster-CI Pre-flight Scanner")
    parser.add_argument("--ci", action="store_true", help="CI mode (fail-fast)")
    parser.add_argument("--interactive", action="store_true", help="Interactive mode (pre-commit)")
    parser.add_argument("--pyproject", default="pyproject.toml", help="Path to pyproject.toml")
    parser.add_argument("--constraints", default="cluster_constraints.txt", help="Path to constraints file")
    
    args = parser.parse_args()
    
    pyproject_path = Path(args.pyproject)
    if not pyproject_path.exists():
        if args.ci:
            log_info("No pyproject.toml found. Skipping check.")
            sys.exit(0)
        else:
            log_error(f"File not found: {args.pyproject}")
            sys.exit(1)

    # 1. Check Python Version
    compat, version = check_python_version(pyproject_path)
    if not compat:
        log_error(f"Incompatible Python version detected: {version}")
        log_error("The cluster requires Python 3.12. Your project excludes it.")
        if args.interactive:
            ans = input("❓ Would you like to fix it to '>=3.12' automatically? [Y/n]: ")
            if ans.lower() in ["", "y", "yes"]:
                fix_pyproject(pyproject_path, fix_python=True)
            else:
                log_info("Aborted by user.")
                sys.exit(1)
        else:
            sys.exit(1)

    # 2. Check Torch Pinning
    torch_conflicts = check_torch_pinning(pyproject_path)
    if torch_conflicts:
        log_error(f"Strict Torch pinning detected: {torch_conflicts}")
        log_info("Strict pinning on ARM64 may conflict with NVIDIA's native builds.")
        if args.interactive:
            ans = input("❓ Would you like to relax these constraints? [Y/n]: ")
            if ans.lower() in ["", "y", "yes"]:
                fix_pyproject(pyproject_path, remove_torch_pin=True)
            else:
                log_info("Aborted by user.")
                sys.exit(1)
        else:
            log_info("Warning: Strict pinning detected. This might fail during installation.")

    # 3. Fetch latest constraints & Simulate full resolution
    constraints_to_use = args.constraints
    if not os.path.exists(constraints_to_use) or args.interactive:
        # In interactive (pre-commit) mode, or if local file missing, always fetch fresh from central repo
        fresh_constraints = fetch_latest_constraints()
        if fresh_constraints:
            constraints_to_use = fresh_constraints
            
    if not simulate_resolution(pyproject_path, constraints_to_use):
        if args.interactive:
            log_error("Automatic fix failed to resolve all conflicts. Manual intervention required.")
        # Cleanup temp file if created
        if constraints_to_use != args.constraints and os.path.exists(constraints_to_use):
            os.remove(constraints_to_use)
        sys.exit(1)

    # Cleanup temp file if created
    if constraints_to_use != args.constraints and os.path.exists(constraints_to_use):
        os.remove(constraints_to_use)

    log_success("Validation complete.")

if __name__ == "__main__":
    main()
