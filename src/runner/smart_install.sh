#!/bin/bash
# Smart dependency installer for Cluster-CI
# Skips installation if dependency specs haven't changed since last successful install.
# Hash is stored in /home/user/.cluster-ci-deps-hash (persistent Docker volume).
set -e

USER_BASE="${PYTHONUSERBASE:-/home/user/.local}"
export PYTHONUSERBASE="$USER_BASE"
export PATH="$USER_BASE/bin:$PATH"

HOME_DIR="${HOME:-/home/user}"
HASH_FILE="${CLUSTER_CI_HASH_FILE:-$HOME_DIR/.cluster-ci-deps-hash}"

# Function to ensure usercustomize.py exists in all user site-packages directories
ensure_usercustomize() {
    for sp in "$USER_BASE"/lib/python3.*/site-packages; do
        if [ -d "$sp" ]; then
            cat << 'EOF_UC' > "$sp/usercustomize.py"
import sys
import site

user_site = site.getusersitepackages()
if user_site in sys.path:
    sys.path.remove(user_site)
    idx = 1 if (sys.path and sys.path[0] in ("", ".", "/workspace")) else 0
    sys.path.insert(idx, user_site)
EOF_UC
        fi
    done
}

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

# Ensure usercustomize.py exists right away (e.g. for existing/migrated packages)
ensure_usercustomize

# Compute a composite hash of all dependency specification files
compute_deps_hash() {
    local files="pyproject.toml"
    [ -f "uv.lock" ] && files="$files uv.lock"
    [ -f "requirements.txt" ] && files="$files requirements.txt"
    [ -f "setup.py" ] && files="$files setup.py"
    md5sum $files 2>/dev/null | md5sum | cut -d' ' -f1
}

DEPS_HASH=$(compute_deps_hash)
CACHED_HASH=$(cat "$HASH_FILE" 2>/dev/null || echo "none")

if [ "$DEPS_HASH" = "$CACHED_HASH" ]; then
    # Quick sanity check: verify that pip-installed packages are actually present in user-site.
    if find "$USER_BASE" -path '*/site-packages/*.dist-info' 2>/dev/null | head -1 | grep -q .; then
        echo "✅ [Cluster-CI] Dependencies unchanged (cached). Skipping install."
        ensure_usercustomize
        if [ -f "/cluster-ci/src/runner/verify_packages.py" ]; then
            python3 /cluster-ci/src/runner/verify_packages.py
        elif command -v python3 >/dev/null 2>&1 && [ -f "$(dirname "$0")/verify_packages.py" ]; then
            python3 "$(dirname "$0")/verify_packages.py"
        fi
        exit 0
    else
        echo "⚠️  [Cluster-CI] Cache hit but pip packages missing from user-site. Reinstalling..."
        rm -f "$HASH_FILE"
    fi
fi

echo "📦 [Cluster-CI] Dependencies changed (hash: ${CACHED_HASH:0:8}… → ${DEPS_HASH:0:8}…). Installing..."

# Handle private git dependencies declared in [tool.uv.sources] that pip cannot resolve from PyPI.
# Strategy:
#   1. Install them to system site-packages from git
#   2. Temporarily strip them from pyproject.toml so pip install -e . doesn't try to resolve them
#   3. Restore pyproject.toml after install
# NOTE: We disable set -e here because pip install of git deps may fail (private repo, network, etc.)
# and we don't want that to kill the entire script.
set +e
GIT_DEPS_FILE="/tmp/cluster-ci-git-deps.txt"

if [ -f "pyproject.toml" ]; then
    python3 -c "
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
        # Step 1: Install git deps to user site-packages
        while read pkg_name git_url; do
            echo "📦 [Cluster-CI] Pre-installing private git dependency: $pkg_name from $git_url"
            pip install -q --progress-bar off --break-system-packages --user "$git_url" 2>&1 || echo "⚠️  [Cluster-CI] Warning: failed to install $pkg_name, continuing..."
        done < "$GIT_DEPS_FILE"

        # Step 2: Temporarily strip git deps from pyproject.toml
        cp pyproject.toml pyproject.toml.cluster-ci-bak
        while read pkg_name git_url; do
            pkg_pattern=$(echo "$pkg_name" | sed 's/[-_]/[-_]/g')
            sed -i "/\"${pkg_pattern}[^a-zA-Z0-9]/d; /\"${pkg_pattern}\"/d" pyproject.toml
        done < "$GIT_DEPS_FILE"
        echo "📦 [Cluster-CI] Temporarily stripped private git deps from pyproject.toml for pip compatibility"
    fi
fi
set -e

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

# Install project deps. Strategy: freeze system packages as constraints to prevent
# pip from re-downloading torch (426MB), nvidia-cudnn (444MB), etc.
# Only exclude packages from constraints whose installed version in the container
# DOES NOT satisfy the version bounds declared in pyproject.toml (e.g., custom NeMo builds).
# Packages already satisfying the bound (e.g. huggingface-hub>=0.20.0 with 0.23.4) MUST remain
# pinned in constraints to avoid unwanted PyPI upgrades and permission errors on /usr/local/bin.
CONSTRAINTS_FILE="/tmp/cluster-ci-system-constraints.txt"

# Extract project dependency names from pyproject.toml that conflict with container packages
PROJECT_DEPS=""
if [ -f "pyproject.toml" ]; then
    PROJECT_DEPS=$(python3 -c "
import sys, re

try:
    try:
        from packaging.requirements import Requirement
    except ImportError:
        from pip._vendor.packaging.requirements import Requirement
except ImportError:
    # If packaging is unavailable, we cannot reliably evaluate PEP 440/508 bounds.
    # The safest alternative is to keep constraints intact rather than blindly excluding.
    sys.stderr.write('⚠️  [Cluster-CI] Warning: packaging not available, keeping constraints intact\n')
    sys.exit(0)

deps = []
try:
    import tomllib
    with open('pyproject.toml', 'rb') as f:
        data = tomllib.load(f)
    deps = data.get('project', {}).get('dependencies', [])
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

conflicting = set()
for dep_str in deps:
    try:
        req = Requirement(dep_str)
        if req.marker and not req.marker.evaluate():
            continue
        norm_name = re.sub(r'[-_.]+', '-', req.name).lower()
        if norm_name in installed:
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

run_pip_silently --progress-bar off --break-system-packages --user -c "$CONSTRAINTS_FILE" -e . || {
    echo "⚠️  [Cluster-CI] Constrained install failed, falling back with --ignore-installed..."
    run_pip_silently --progress-bar off --break-system-packages --ignore-installed --user -e .
}


# --- NVSHMEM Stub Fix for DGX Spark (PyTorch container) ---
# vLLM searches for libnvshmem.so on multi-GPU/cluster builds. On the single-GPU Spark,
# it's missing. We symlink the NVIDIA stub directly into the PyTorch lib folder.
echo "📋 [Cluster-CI] Applying NVSHMEM stub fix..."
python3 -c "
import torch, os
torch_lib = os.path.join(os.path.dirname(torch.__file__), 'lib')
stub_target = os.path.join(torch_lib, 'libnvshmem.so')
if not os.path.exists(stub_target):
    os.system(f'ln -sf /usr/local/cuda/lib64/stubs/libnvshmem.so {stub_target}')
    print(f'Symlinked NVSHMEM stub to {stub_target}')
"

# Restore original pyproject.toml
if [ -f "pyproject.toml.cluster-ci-bak" ]; then
    mv pyproject.toml.cluster-ci-bak pyproject.toml
fi

# Post-install: purge any PyPI-downloaded NVIDIA/PyTorch/vLLM packages that would
# shadow the highly-optimized NGC system libraries or source-compiled vLLM in /home/user/vllm
# See: PyTorch/NVIDIA Library Shadowing Bug (memory ae4a85be)
# NOTE: --prefix installs to dist-packages on Debian, so we must check both patterns.
for site_packages_dir in \
    "/home/user/.local/lib/python3."*"/site-packages" \
    "/home/user/.local/lib/python3."*"/dist-packages" \
    "/home/user/.local/local/lib/python3."*"/site-packages" \
    "/home/user/.local/local/lib/python3."*"/dist-packages" \
    "/workspace/.venv/lib/python3."*"/site-packages" \
    "./.venv/lib/python3."*"/site-packages"; do
    if [ -d "$site_packages_dir" ] || ls "$site_packages_dir" 1>/dev/null 2>&1; then
        rm -rf "$site_packages_dir"/torch \
               "$site_packages_dir"/torch-* \
               "$site_packages_dir"/torchvision \
               "$site_packages_dir"/torchvision-* \
               "$site_packages_dir"/nvidia* \
               "$site_packages_dir"/nvshmem* \
               "$site_packages_dir"/triton* \
               "$site_packages_dir"/xformers* \
               "$site_packages_dir"/vllm \
               "$site_packages_dir"/vllm-* 2>/dev/null || true
    fi
done

# Patch bitsandbytes for newer CUDA versions (e.g. 13.2) if missing
BNB_DIR=$(ls -d /home/user/.local/lib/python3.*/site-packages/bitsandbytes 2>/dev/null | head -n 1)
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
# (prevents pip install -e . or pip dependencies from overwriting it with a broken shebang)
UV_DVC_BIN="$USER_BASE/share/uv/tools/dvc/bin/dvc"
if [ -f "$UV_DVC_BIN" ]; then
    mkdir -p "$USER_BASE/bin"
    ln -sf "$UV_DVC_BIN" "$USER_BASE/bin/dvc"
    echo "🔧 [Cluster-CI] Restored isolated DVC launcher symlink ($UV_DVC_BIN -> $USER_BASE/bin/dvc)"
else
    echo "⚠️  [Cluster-CI] Warning: isolated uv DVC binary not found at $UV_DVC_BIN"
fi

# Ensure usercustomize.py exists in user-site to guarantee user packages take priority across all images
ensure_usercustomize
if [ -f "/cluster-ci/src/runner/verify_packages.py" ]; then
    python3 /cluster-ci/src/runner/verify_packages.py
elif command -v python3 >/dev/null 2>&1 && [ -f "$(dirname "$0")/verify_packages.py" ]; then
    python3 "$(dirname "$0")/verify_packages.py"
fi

# Save hash only after successful install
echo "$DEPS_HASH" > "$HASH_FILE"
echo "✅ [Cluster-CI] Dependencies installed and cached."
