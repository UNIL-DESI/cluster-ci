#!/bin/bash
# Smart dependency installer for Cluster-CI
# R1: Specialized runtime images (vllm, nemo) - Image environment WINS entirely.
# R2: Generic images (pytorch, python) - Pure-Python upgrades allowed, core heavy pinned.
# R3: Stage interpreter inspection via PYTHONNOUSERSITE=1 python3 -s.
# R4: Composite deps hash with mechanism version and image footprint; purge on change.
# R5: Fail-fast verification of package versions in real stage execution conditions.
set -e

SMART_INSTALL_VERSION="v3-runtime-r1r5-v1"

USER_BASE="${PYTHONUSERBASE:-/home/user/.local}"
export PYTHONUSERBASE="$USER_BASE"
export PATH="$USER_BASE/bin:$PATH"

HOME_DIR="${HOME:-/home/user}"
HASH_FILE="${CLUSTER_CI_HASH_FILE:-$HOME_DIR/.cluster-ci-deps-hash}"

STAGE_PYTHON="${STAGE_PYTHON:-python3}"

# Compute composite hash of dependency specification files, mechanism version and clean image footprint
compute_deps_hash() {
    local files=""
    [ -f "pyproject.toml" ] && files="$files pyproject.toml"
    [ -f "uv.lock" ] && files="$files uv.lock"
    [ -f "requirements.txt" ] && files="$files requirements.txt"
    [ -f "setup.py" ] && files="$files setup.py"

    local proj_hash="none"
    if [ -n "$files" ]; then
        proj_hash=$(md5sum $files 2>/dev/null | md5sum | cut -d' ' -f1)
    fi

    local img_footprint=$(PYTHONNOUSERSITE=1 "$STAGE_PYTHON" -s -c "
import importlib.metadata as m, sys
py_id = f'{sys.executable}:{sys.version_info[:3]}'
dists = sorted(f'{d.name or \"\"}=={d.version or \"\"}' for d in m.distributions() if d.name)
print(py_id + '\n' + '\n'.join(dists))
" 2>/dev/null | md5sum | cut -d' ' -f1)

    echo "${SMART_INSTALL_VERSION}:${proj_hash}:${img_footprint}" | md5sum | cut -d' ' -f1
}

if [ "$1" = "--compute-hash" ]; then
    compute_deps_hash
    exit 0
fi

# Function to detect runtime mode (r1 vs r2) using clean interpreter inspection
detect_runtime_mode() {
    PYTHONNOUSERSITE=1 "$STAGE_PYTHON" -s -c "
import importlib.metadata as meta
names = set()
for dist in meta.distributions():
    if dist.metadata.get('Name'):
        norm = dist.metadata['Name'].lower().replace('-', '_')
        names.add(norm)
if 'vllm' in names or 'nemo_automodel' in names or 'nemo' in names:
    print('r1')
else:
    print('r2')
" 2>/dev/null || echo "r2"
}

if [ "$1" = "--detect-mode" ]; then
    detect_runtime_mode
    exit 0
fi

# Function to ensure usercustomize.py exists in all user site-packages directories
ensure_usercustomize() {
    local mode="$1"
    if [ -z "$mode" ]; then
        mode=$(detect_runtime_mode)
    fi

    for sp in "$USER_BASE"/lib/python3.*/site-packages "$USER_BASE"/lib/python3.*/dist-packages; do
        if [ -d "$sp" ]; then
            if [ "$mode" = "r1" ]; then
                cat << 'EOF_UC_R1' > "$sp/usercustomize.py"
import sys
import site

user_site = site.getusersitepackages()
user_paths = [p for p in sys.path if p == user_site or (isinstance(p, str) and p.startswith("/home/user/.local/"))]
for p in user_paths:
    while p in sys.path:
        sys.path.remove(p)
for p in user_paths:
    sys.path.append(p)
EOF_UC_R1
            else
                cat << 'EOF_UC_R2' > "$sp/usercustomize.py"
import sys
import site

user_site = site.getusersitepackages()
if user_site in sys.path:
    sys.path.remove(user_site)
    idx = 1 if (sys.path and sys.path[0] in ("", ".", "/workspace")) else 0
    sys.path.insert(idx, user_site)
EOF_UC_R2
            fi
        fi
    done
}

# Concurrency control: acquire exclusive flock on shared installation volume
LOCK_FILE="${CLUSTER_CI_LOCK_FILE:-$USER_BASE/.cluster-ci-install.lock}"
LOCK_TIMEOUT="${SMART_INSTALL_LOCK_TIMEOUT:-600}"

mkdir -p "$(dirname "$LOCK_FILE")"

if command -v flock >/dev/null 2>&1; then
    exec 200>"$LOCK_FILE"
    if ! flock -w "$LOCK_TIMEOUT" 200; then
        echo "❌ [Cluster-CI] Timeout: Failed to acquire installation lock on '$LOCK_FILE' within ${LOCK_TIMEOUT}s. Another installation process is running or stalled." >&2
        exit 1
    fi
    release_install_lock() {
        flock -u 200 2>/dev/null || true
        exec 200>&- 2>/dev/null || true
    }
fi

cleanup_smart_install() {
    if [ -f "pyproject.toml.cluster-ci-bak" ]; then
        mv -f "pyproject.toml.cluster-ci-bak" "pyproject.toml" 2>/dev/null || true
    fi
    if type release_install_lock >/dev/null 2>&1; then
        release_install_lock
    fi
}
trap cleanup_smart_install EXIT

# Migration: migrate legacy ~/.local/local (from previous pip --prefix installs) to standard user-site ~/.local
if [ -d "$USER_BASE/local" ]; then
    echo "📦 [Cluster-CI] Migrating legacy packages from $USER_BASE/local to user-site..."
    for d in "$USER_BASE"/local/lib/python3.*/dist-packages; do
        if [ -d "$d" ]; then
            pyver=$(basename $(dirname "$d"))
            target="$USER_BASE/lib/$pyver/site-packages"
            mkdir -p "$target"
            if [ -n "$(ls -A "$d" 2>/dev/null)" ]; then
                cp -a "$d"/. "$target/" || {
                    echo "❌ [Cluster-CI] Migration failed: could not copy packages from $d to $target" >&2
                    exit 1
                }
            fi
        fi
    done
    if [ -d "$USER_BASE/local/bin" ]; then
        mkdir -p "$USER_BASE/bin"
        if [ -n "$(ls -A "$USER_BASE/local/bin" 2>/dev/null)" ]; then
            cp -a "$USER_BASE/local/bin"/. "$USER_BASE/bin/" || {
                echo "❌ [Cluster-CI] Migration failed: could not copy binaries from $USER_BASE/local/bin to $USER_BASE/bin" >&2
                exit 1
            }
        fi
    fi
    rm -rf "$USER_BASE/local"
    echo "✅ [Cluster-CI] Migration to user-site complete."
fi


RUNTIME_MODE=$(detect_runtime_mode)
echo "🔍 [Cluster-CI] Stage Python: $($STAGE_PYTHON --version 2>&1) | Runtime mode: $RUNTIME_MODE"

DEPS_HASH=$(compute_deps_hash)
CACHED_HASH=$(cat "$HASH_FILE" 2>/dev/null || echo "none")

if [ "$DEPS_HASH" = "$CACHED_HASH" ]; then
    # Quick sanity check: verify that packages exist if pyproject.toml or requirements.txt is present
    if { [ ! -f "pyproject.toml" ] && [ ! -f "requirements.txt" ]; } || find "$USER_BASE" -path '*/site-packages/*.dist-info' 2>/dev/null | head -1 | grep -q .; then
        echo "✅ [Cluster-CI] Dependencies unchanged (cached). Skipping install."
        ensure_usercustomize "$RUNTIME_MODE"
        if [ -f "/cluster-ci/src/runner/verify_packages.py" ]; then
            "$STAGE_PYTHON" /cluster-ci/src/runner/verify_packages.py
        elif command -v "$STAGE_PYTHON" >/dev/null 2>&1 && [ -f "$(dirname "$0")/verify_packages.py" ]; then
            "$STAGE_PYTHON" "$(dirname "$0")/verify_packages.py"
        fi
        exit 0
    else
        echo "⚠️  [Cluster-CI] Cache hit but packages missing from user-site. Reinstalling..."
        rm -f "$HASH_FILE"
    fi
fi

echo "📦 [Cluster-CI] Dependencies changed (hash: ${CACHED_HASH:0:8}… → ${DEPS_HASH:0:8}…). Installing..."

# Volume invalidation (R4): purge user site before fresh install, preserving shared caches
echo "🧹 [Cluster-CI] Invalidation: purging user-site ($USER_BASE/lib, $USER_BASE/local) for clean install..."
rm -rf "$USER_BASE"/lib "$USER_BASE"/local
rm -rf "$USER_BASE"/bin
mkdir -p "$USER_BASE"/bin

# Ensure usercustomize exists in newly created environment
ensure_usercustomize "$RUNTIME_MODE"

# Handle private git dependencies declared in [tool.uv.sources] that pip cannot resolve from PyPI
set +e
GIT_DEPS_FILE="/tmp/cluster-ci-git-deps.txt"

if [ -f "pyproject.toml" ]; then
    "$STAGE_PYTHON" -c "
import re
content = open('pyproject.toml').read()
m = re.search(r'\[tool\.uv\.sources\](.*?)(\n\[|\Z)', content, re.DOTALL)
if m:
    section = m.group(1)
    for match in re.finditer(r'(\S+)\s*=\s*\{[^}]*git\s*=\s*\"([^\"]+)\"', section):
        pkg, url = match.group(1), match.group(2)
        branch_match = re.search(r'branch\s*=\s*\"([^\"]+)\"', match.group(0))
        ref = f'@{branch_match.group(1)}' if branch_match else ''
        print(f'{pkg} git+{url}{ref}')
" > "$GIT_DEPS_FILE" 2>/dev/null

    if [ -s "$GIT_DEPS_FILE" ]; then
        while read pkg_name git_url; do
            echo "📦 [Cluster-CI] Pre-installing private git dependency: $pkg_name from $git_url"
            pip install -q --progress-bar off --break-system-packages --user "$git_url" 2>&1 || echo "⚠️  [Cluster-CI] Warning: failed to install $pkg_name, continuing..."
        done < "$GIT_DEPS_FILE"

        cp pyproject.toml pyproject.toml.cluster-ci-bak
        while read pkg_name git_url; do
            pkg_pattern=$(echo "$pkg_name" | sed 's/[-_]/[-_]/g')
            sed -i "/\"${pkg_pattern}[^a-zA-Z0-9]/d; /\"${pkg_pattern}\"/d" pyproject.toml
        done < "$GIT_DEPS_FILE"
        echo "📦 [Cluster-CI] Temporarily stripped private git deps from pyproject.toml for pip compatibility"
    fi
fi
set -e

CONSTRAINTS_FILE="/tmp/cluster-ci-system-constraints.txt"
ABSENT_DEPS_FILE="/tmp/cluster-ci-absent-deps.txt"
rm -f "$ABSENT_DEPS_FILE"

# Helper function to run pip silently and only print output on failure
run_pip_silently() {
    local log_file="/tmp/pip_install.log"
    if ! pip install -q "$@" > "$log_file" 2>&1; then
        cat "$log_file"
        rm -f "$log_file"
        return 1
    fi
    rm -f "$log_file"
    return 0
}

if [ "$RUNTIME_MODE" = "r1" ]; then
    echo "⚡ [Cluster-CI] Specialized Runtime Image (R1): Image environment WINS entirely."

    # Parse project dependencies (pyproject.toml / requirements.txt) against clean stage image distributions (R3)
    ANALYSIS_FILE="/tmp/cluster-ci-r1-analysis-$$.txt"
    ANALYSIS_ERR="/tmp/cluster-ci-r1-analysis-$$.err"
    ABSENT_DEPS_FILE="/tmp/cluster-ci-absent-deps-$$.txt"
    CONSTRAINTS_FILE="/tmp/cluster-ci-img-constraints-$$.txt"

    if ! PYTHONNOUSERSITE=1 "$STAGE_PYTHON" -s -c "
import sys, os, re
try:
    from packaging.requirements import Requirement
except ImportError:
    from pip._vendor.packaging.requirements import Requirement

try:
    import importlib.metadata as meta
except ImportError:
    meta = None

installed = {}
if meta:
    for dist in meta.distributions():
        dname = dist.metadata.get('Name')
        if dname:
            norm = re.sub(r'[-_.]+', '-', dname).lower()
            installed[norm] = dist.version

deps = []
try:
    import tomllib
    with open('pyproject.toml', 'rb') as f:
        data = tomllib.load(f)
    deps = list(data.get('project', {}).get('dependencies', []))
except Exception:
    pass

if not deps:
    try:
        with open('pyproject.toml', 'r', encoding='utf-8') as f:
            content = f.read()
        in_deps = False
        for line in content.splitlines():
            line_s = line.strip()
            if 'dependencies' in line_s and '=' in line_s:
                in_deps = True
                continue
            if in_deps:
                if line_s.startswith(']'):
                    break
                m = re.match(r'^\s*[\"\']([^\"\']+)[\"\']', line)
                if m:
                    deps.append(m.group(1))
    except Exception:
        pass

if os.path.isfile('requirements.txt'):
    try:
        with open('requirements.txt', 'r', encoding='utf-8') as f:
            for line in f:
                line_s = line.strip()
                if not line_s or line_s.startswith('#') or line_s.startswith('-'):
                    continue
                req_val = line_s.split('#')[0].strip()
                if req_val and req_val not in deps:
                    deps.append(req_val)
    except Exception:
        pass

conflicts = []
absent = []
satisfied = []

for dep_str in deps:
    try:
        req = Requirement(dep_str)
        if req.marker and not req.marker.evaluate():
            continue
        norm_name = re.sub(r'[-_.]+', '-', req.name).lower()
        if norm_name in installed:
            inst_ver = installed[norm_name]
            try:
                sat = req.specifier.contains(inst_ver, prereleases=True)
            except Exception as spec_err:
                sat = False
                conflicts.append((req.name, f'{req.specifier} (invalid specifier or evaluation error: {spec_err})', inst_ver))
                continue
            if sat:
                satisfied.append((req.name, inst_ver))
            else:
                conflicts.append((req.name, str(req.specifier), inst_ver))
        else:
            absent.append(dep_str)
    except Exception as exc:
        conflicts.append((dep_str, f'unparseable dependency specification: {exc}', 'none'))

if conflicts:
    print('STATUS:CONFLICT')
    for name, spec, inst_ver in conflicts:
        print(f'CONFLICT:{name}:{spec}:{inst_ver}')
    sys.exit(0)

print('STATUS:OK')
for a in absent:
    print(f'ABSENT:{a}')
for s_name, s_ver in satisfied:
    print(f'SATISFIED:{s_name}:{s_ver}')
" > "$ANALYSIS_FILE" 2> "$ANALYSIS_ERR"; then
        echo "❌ [Cluster-CI] Error: Dependency analysis against specialized runtime image failed." >&2
        if [ -s "$ANALYSIS_ERR" ]; then
            cat "$ANALYSIS_ERR" >&2
        fi
        rm -f "$ANALYSIS_ERR" "$ANALYSIS_FILE"
        exit 1
    fi
    rm -f "$ANALYSIS_ERR"

    if ! grep -q "^STATUS:" "$ANALYSIS_FILE"; then
        echo "❌ [Cluster-CI] Error: Dependency analysis produced invalid output (missing STATUS header)." >&2
        cat "$ANALYSIS_FILE" >&2
        rm -f "$ANALYSIS_FILE"
        exit 1
    fi

    if grep -q "^STATUS:CONFLICT" "$ANALYSIS_FILE"; then
        echo "❌ [Cluster-CI] Error: Project requirements conflict with specialized runtime image." >&2
        grep "^CONFLICT:" "$ANALYSIS_FILE" | while IFS=: read -r _ c_name c_spec c_ver; do
            echo "   - Package '$c_name' requires '$c_spec', but specialized image provides '$c_ver'." >&2
        done
        echo "   In specialized runtime images (vLLM / NeMo), the image environment must win entirely to prevent regressions." >&2
        echo "   Please adjust your project requirements to match the image version or use a compatible runtime image." >&2
        rm -f "$ANALYSIS_FILE"
        exit 1
    fi

    # Extract absent dependencies
    grep "^ABSENT:" "$ANALYSIS_FILE" | cut -d: -f2- > "$ABSENT_DEPS_FILE" || true
    ABSENT_COUNT=$(wc -l < "$ABSENT_DEPS_FILE" 2>/dev/null || echo 0)
    echo "📋 [Cluster-CI] Specialized image analysis: $(grep -c '^SATISFIED:' "$ANALYSIS_FILE" 2>/dev/null || echo 0) dependencies satisfied by image, $ABSENT_COUNT absent dependencies to install."
    rm -f "$ANALYSIS_FILE"

    # Freeze clean system packages as strict constraints
    PYTHONNOUSERSITE=1 "$STAGE_PYTHON" -s -m pip freeze --all 2>/dev/null \
        | grep -v "^-e " | grep -v "^#" | grep -v " @ " \
        > "$CONSTRAINTS_FILE"

    # Install absent dependencies into user-site
    if [ "$ABSENT_COUNT" -gt 0 ]; then
        echo "📦 [Cluster-CI] Installing absent dependencies into user-site..."
        while read -r dep; do
            [ -z "$dep" ] && continue
            echo "   -> Installing: $dep"
            if ! run_pip_silently --progress-bar off --break-system-packages --user -c "$CONSTRAINTS_FILE" "$dep"; then
                echo "❌ [Cluster-CI] Failed to install absent dependency under specialized runtime image constraints: $dep" >&2
                echo "   Specialized runtime images (vLLM / NeMo) lock preinstalled system packages." >&2
                echo "   Package '$dep' or its transitive dependencies conflict with the frozen image environment." >&2
                echo "   Silent --no-deps fallback is disabled. Adjust project requirements or use a compatible runtime image." >&2
                rm -f "$ABSENT_DEPS_FILE" "$CONSTRAINTS_FILE"
                exit 1
            fi
        done < "$ABSENT_DEPS_FILE"
    fi
    rm -f "$ABSENT_DEPS_FILE" "$CONSTRAINTS_FILE"

    # Install project itself into user-site with --no-deps if project definition exists
    if [ -f "pyproject.toml" ] || [ -f "setup.py" ]; then
        echo "📦 [Cluster-CI] Installing project in editable mode (--no-deps)..."
        run_pip_silently --progress-bar off --break-system-packages --user --no-deps -e . || {
            echo "❌ [Cluster-CI] Failed to install project in editable mode." >&2
            exit 1
        }
    fi

    # Post-install purge in R1: remove any package from user-site that exists in the image environment
    echo "🧹 [Cluster-CI] Post-install R1 hygiene: purging any image distributions from user-site..."
    PYTHONNOUSERSITE=1 "$STAGE_PYTHON" -s -c "
import importlib.metadata as meta, os, shutil, re
user_base = '$USER_BASE'
img_pkgs = {re.sub(r'[-_.]+', '-', d.name).lower() for d in meta.distributions() if d.name}
for sp in [os.path.join(user_base, 'lib', d, 'site-packages') for d in os.listdir(os.path.join(user_base, 'lib')) if os.path.isdir(os.path.join(user_base, 'lib', d))]:
    if not os.path.isdir(sp):
        continue
    for item in os.listdir(sp):
        norm = re.sub(r'[-_.]+', '-', item.split('-')[0]).lower()
        if norm in img_pkgs and item != 'usercustomize.py':
            full = os.path.join(sp, item)
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
            elif os.path.isfile(full):
                os.remove(full)
" 2>/dev/null || true

else
    echo "⚡ [Cluster-CI] Generic Image (R2): Pure-Python upgrades allowed, core heavy pinned."

    # Extract project dependency names from pyproject.toml / requirements.txt that conflict with container packages
    PROJECT_DEPS=""
    if [ -f "pyproject.toml" ] || [ -f "requirements.txt" ]; then
        PROJECT_DEPS=$("$STAGE_PYTHON" -c "
import sys, os, re
try:
    from packaging.requirements import Requirement
except ImportError:
    from pip._vendor.packaging.requirements import Requirement

deps = []
try:
    import tomllib
    with open('pyproject.toml', 'rb') as f:
        data = tomllib.load(f)
    deps = list(data.get('project', {}).get('dependencies', []))
except Exception:
    pass

if not deps:
    try:
        with open('pyproject.toml', 'r', encoding='utf-8') as f:
            content = f.read()
        in_deps = False
        for line in content.splitlines():
            line_s = line.strip()
            if 'dependencies' in line_s and '=' in line_s:
                in_deps = True
                continue
            if in_deps:
                if line_s.startswith(']'):
                    break
                m = re.match(r'^\s*[\"\']([^\"\']+)[\"\']', line)
                if m:
                    deps.append(m.group(1))
    except Exception:
        pass

if os.path.isfile('requirements.txt'):
    try:
        with open('requirements.txt', 'r', encoding='utf-8') as f:
            for line in f:
                line_s = line.strip()
                if not line_s or line_s.startswith('#') or line_s.startswith('-'):
                    continue
                req_val = line_s.split('#')[0].strip()
                if req_val and req_val not in deps:
                    deps.append(req_val)
    except Exception:
        pass

installed = {}
try:
    import importlib.metadata as meta
    for dist in meta.distributions():
        dname = dist.metadata.get('Name')
        if dname:
            norm = re.sub(r'[-_.]+', '-', dname).lower()
            installed[norm] = dist.version
except Exception:
    pass

# Packages that MUST NEVER be upgraded in user-site for generic images
PROTECTED = {'torch', 'torchvision', 'torchaudio', 'nvidia', 'nvshmem', 'triton', 'xformers'}

conflicting = set()
for dep_str in deps:
    try:
        req = Requirement(dep_str)
        if req.marker and not req.marker.evaluate():
            continue
        norm_name = re.sub(r'[-_.]+', '-', req.name).lower()
        if norm_name in installed:
            # If protected, cannot be excluded from constraints
            if any(norm_name.startswith(p) for p in PROTECTED):
                continue
            inst_ver = installed[norm_name]
            try:
                satisfies = req.specifier.contains(inst_ver, prereleases=True)
            except Exception:
                satisfies = False
            if not satisfies:
                conflicting.add(norm_name.replace('-', '_'))
                conflicting.add(norm_name.replace('_', '-'))
    except Exception:
        pass

for name in sorted(conflicting):
    print(name)
" 2>/dev/null || true)
    fi

    # Build grep exclusion pattern from conflicting project deps
    EXCLUDE_PATTERN=""
    for dep in $PROJECT_DEPS; do
        if [ -n "$EXCLUDE_PATTERN" ]; then
            EXCLUDE_PATTERN="$EXCLUDE_PATTERN|^${dep}=="
        else
            EXCLUDE_PATTERN="^${dep}=="
        fi
    done

    if [ -n "$EXCLUDE_PATTERN" ]; then
        pip freeze --all 2>/dev/null | grep -v "^-e " | grep -v "^#" \
            | grep -v " @ " \
            | grep -ivE "$EXCLUDE_PATTERN" \
            > "$CONSTRAINTS_FILE"
        EXCLUDED_COUNT=$(echo "$PROJECT_DEPS" | wc -w)
        echo "📋 [Cluster-CI] System constraints: $(wc -l < "$CONSTRAINTS_FILE") packages pinned ($EXCLUDED_COUNT conflicting deps excluded)"
    else
        pip freeze --all 2>/dev/null | grep -v "^-e " | grep -v "^#" \
            | grep -v " @ " \
            > "$CONSTRAINTS_FILE"
        echo "📋 [Cluster-CI] System constraints: $(wc -l < "$CONSTRAINTS_FILE") packages pinned"
    fi

    if [ -f "pyproject.toml" ] || [ -f "setup.py" ]; then
        run_pip_silently --progress-bar off --break-system-packages --user -c "$CONSTRAINTS_FILE" -e . || {
            echo "⚠️  [Cluster-CI] Constrained install failed, falling back with --ignore-installed..."
            run_pip_silently --progress-bar off --break-system-packages --ignore-installed --user -e .
        }
    elif [ -f "requirements.txt" ]; then
        run_pip_silently --progress-bar off --break-system-packages --user -c "$CONSTRAINTS_FILE" -r requirements.txt || {
            echo "⚠️  [Cluster-CI] Constrained install failed, falling back with --ignore-installed..."
            run_pip_silently --progress-bar off --break-system-packages --ignore-installed --user -r requirements.txt
        }
    fi
fi

# Restore original pyproject.toml if temporarily stripped
if [ -f "pyproject.toml.cluster-ci-bak" ]; then
    mv -f pyproject.toml.cluster-ci-bak pyproject.toml 2>/dev/null || true
fi

# Post-install: ALWAYS purge any PyPI-downloaded NVIDIA/PyTorch/vLLM packages that would
# shadow the highly-optimized NGC system libraries or source-compiled vLLM
for site_packages_dir in \
    "$USER_BASE/lib/python3."*"/site-packages" \
    "$USER_BASE/lib/python3."*"/dist-packages" \
    "$USER_BASE/local/lib/python3."*"/site-packages" \
    "$USER_BASE/local/lib/python3."*"/dist-packages"; do
    if [ -d "$site_packages_dir" ]; then
        rm -rf "$site_packages_dir"/torch \
               "$site_packages_dir"/torch-* \
               "$site_packages_dir"/torchvision \
               "$site_packages_dir"/torchvision-* \
               "$site_packages_dir"/torchaudio \
               "$site_packages_dir"/torchaudio-* \
               "$site_packages_dir"/nvidia* \
               "$site_packages_dir"/nvshmem* \
               "$site_packages_dir"/triton* \
               "$site_packages_dir"/xformers* \
               "$site_packages_dir"/vllm \
               "$site_packages_dir"/vllm-* \
               "$site_packages_dir"/nemo \
               "$site_packages_dir"/nemo_* 2>/dev/null || true
    fi
done

# Common NVSHMEM Stub Fix for DGX Spark (PyTorch container)
echo "📋 [Cluster-CI] Applying NVSHMEM stub check..."
"$STAGE_PYTHON" -c "
import os
try:
    import torch
    torch_lib = os.path.join(os.path.dirname(torch.__file__), 'lib')
    stub_target = os.path.join(torch_lib, 'libnvshmem.so')
    if not os.path.exists(stub_target):
        os.system(f'ln -sf /usr/local/cuda/lib64/stubs/libnvshmem.so {stub_target}')
        print(f'Symlinked NVSHMEM stub to {stub_target}')
except Exception:
    pass
" 2>/dev/null || true

# Patch bitsandbytes for newer CUDA versions (e.g. 13.2) if missing
BNB_DIR=$(ls -d "$USER_BASE"/lib/python3.*/site-packages/bitsandbytes 2>/dev/null | head -n 1)
if [ -n "$BNB_DIR" ] && command -v nvcc >/dev/null; then
    SYS_CUDA=$(nvcc --version | grep 'release' | awk '{print $5}' | cut -d',' -f1 | tr -d '.')
    if [ -n "$SYS_CUDA" ]; then
        HIGHEST_SO=$(ls "$BNB_DIR"/libbitsandbytes_cuda*.so 2>/dev/null | grep -Eo 'cuda[0-9]+' | sed 's/cuda//' | sort -nr | head -n 1)
        if [ -n "$HIGHEST_SO" ] && [ "$SYS_CUDA" -gt "$HIGHEST_SO" ] && [ ! -f "$BNB_DIR/libbitsandbytes_cuda${SYS_CUDA}.so" ]; then
            echo "🔧 [Cluster-CI] Patching bitsandbytes for CUDA $SYS_CUDA (fallback to $HIGHEST_SO)"
            ln -s "libbitsandbytes_cuda${HIGHEST_SO}.so" "$BNB_DIR/libbitsandbytes_cuda${SYS_CUDA}.so"
        fi
    fi
fi

# Ensure isolated DVC launcher from uv tool is preserved in /home/user/.local/bin
UV_DVC_BIN="$USER_BASE/share/uv/tools/dvc/bin/dvc"
if [ -f "$UV_DVC_BIN" ]; then
    mkdir -p "$USER_BASE/bin"
    ln -sf "$UV_DVC_BIN" "$USER_BASE/bin/dvc"
    echo "🔧 [Cluster-CI] Restored isolated DVC launcher symlink ($UV_DVC_BIN -> $USER_BASE/bin/dvc)"
fi

# Ensure usercustomize.py is written in appropriate mode
ensure_usercustomize "$RUNTIME_MODE"

# Fail-fast verification of package versions in real stage execution conditions (R5)
if [ -f "/cluster-ci/src/runner/verify_packages.py" ]; then
    "$STAGE_PYTHON" /cluster-ci/src/runner/verify_packages.py
elif command -v "$STAGE_PYTHON" >/dev/null 2>&1 && [ -f "$(dirname "$0")/verify_packages.py" ]; then
    "$STAGE_PYTHON" "$(dirname "$0")/verify_packages.py"
fi

# Save hash only after successful install
echo "$DEPS_HASH" > "$HASH_FILE"
echo "✅ [Cluster-CI] Dependencies installed and cached."
