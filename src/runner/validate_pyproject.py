import argparse
import os
import sys
import subprocess
import re
import importlib.util
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

def simulate_resolution(pyproject_path, constraints_path):
    if not os.path.exists(constraints_path):
        log_info(f"No constraints file found at {constraints_path}. Skipping full simulation.")
        return True
    
    log_info("Simulating ARM64 resolution with uv...")
    cmd = [
        "uv", "pip", "compile",
        "--python-platform", "aarch64-unknown-linux-gnu",
        "--python", "3.12",
        "-c", constraints_path,
        pyproject_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log_error("Resolution simulation failed!")
        print(result.stderr)
        return False
    
    log_success("Resolution simulation passed.")
    return True

def check_imports(pyproject_path):
    log_info("Checking real importability of dependencies...")
    with open(pyproject_path, "r") as f:
        doc = tomlkit.parse(f.read())

    dependencies = doc.get("project", {}).get("dependencies", [])
    if not dependencies:
        log_info("No dependencies found in pyproject.toml.")
        return True

    # Mapping of package names to import names
    IMPORT_MAP = {
        "pyyaml": "yaml",
        "scikit-learn": "sklearn",
        "python-dotenv": "dotenv",
        "beautifulsoup4": "bs4",
        "opencv-python": "cv2",
        "pypi-mirror": "pypi_mirror",
    }

    failed_imports = []
    for dep in dependencies:
        # Extract package name (ignore versions/extras)
        # e.g., "polars>=0.20.0" -> "polars"
        # e.g., "fastapi[all]" -> "fastapi"
        pkg_name = re.split(r'[>=<\[ ]', dep)[0].strip().lower()
        if not pkg_name:
            continue

        import_name = IMPORT_MAP.get(pkg_name, pkg_name).replace("-", "_")

        log_info(f"Verifying import: {import_name} (from {pkg_name})...")
        spec = importlib.util.find_spec(import_name)
        if spec is None:
            log_error(f"Module '{import_name}' (package: {pkg_name}) is NOT importable!")
            failed_imports.append(pkg_name)
        else:
            log_success(f"Module '{import_name}' is available.")

    if failed_imports:
        log_error(f"Failed to import {len(failed_imports)} dependencies: {', '.join(failed_imports)}")
        log_error("This usually means smart_install.sh failed or UID/PYTHONPATH is misconfigured.")
        return False

    log_success("All project dependencies are importable.")
    return True

def main():
    parser = argparse.ArgumentParser(description="Cluster-CI Pre-flight Scanner")
    parser.add_argument("--ci", action="store_true", help="CI mode (fail-fast)")
    parser.add_argument("--interactive", action="store_true", help="Interactive mode (pre-commit)")
    parser.add_argument("--pyproject", default="pyproject.toml", help="Path to pyproject.toml")
    parser.add_argument("--constraints", default="cluster_constraints.txt", help="Path to constraints file")
    parser.add_argument("--check-imports", action="store_true", help="Verify real importability of dependencies")
    
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

    # 4. Check Imports (if requested)
    if args.check_imports:
        if not check_imports(pyproject_path):
            sys.exit(1)

    # Cleanup temp file if created
    if constraints_to_use != args.constraints and os.path.exists(constraints_to_use):
        os.remove(constraints_to_use)

    log_success("Validation complete.")

if __name__ == "__main__":
    main()
