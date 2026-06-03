#!/bin/bash
# Smart dependency installer for Cluster-CI
# Skips installation if dependency specs haven't changed since last successful install.
# Hash is stored in /home/user/.cluster-ci-deps-hash (persistent Docker volume).
set -e

HASH_FILE="/home/user/.cluster-ci-deps-hash"

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
    # Quick sanity check: verify that key packages are actually importable
    # This catches edge cases where the install was cached but the container was killed
    if python3 -c "import dvc" 2>/dev/null; then
        echo "✅ [Cluster-CI] Dependencies unchanged (cached). Skipping install."
        exit 0
    else
        echo "⚠️  [Cluster-CI] Cache hit but packages not importable. Reinstalling..."
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
        # Step 1: Install git deps to system site-packages
        while read pkg_name git_url; do
            echo "📦 [Cluster-CI] Pre-installing private git dependency: $pkg_name from $git_url"
            pip install --break-system-packages "$git_url" 2>&1 || echo "⚠️  [Cluster-CI] Warning: failed to install $pkg_name, continuing..."
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

# Install project with system packages using pip to bypass lockfile conflicts with NGC PyTorch
pip install --break-system-packages --prefix /home/user/.local -e .
pip install --break-system-packages --prefix /home/user/.local dvc-http

# Restore original pyproject.toml
if [ -f "pyproject.toml.cluster-ci-bak" ]; then
    mv pyproject.toml.cluster-ci-bak pyproject.toml
fi

# Post-install: purge any PyPI-downloaded NVIDIA/PyTorch/vLLM packages that would
# shadow the highly-optimized NGC system libraries or source-compiled vLLM in /home/user/vllm
# See: PyTorch/NVIDIA Library Shadowing Bug (memory ae4a85be)
for site_packages_dir in "/home/user/.local/lib/python3."*"/site-packages" "/workspace/.venv/lib/python3."*"/site-packages" "./.venv/lib/python3."*"/site-packages"; do
    if [ -d "$site_packages_dir" ] || ls "$site_packages_dir" 1>/dev/null 2>&1; then
        rm -rf "$site_packages_dir"/torch \
               "$site_packages_dir"/torch-* \
               "$site_packages_dir"/torchvision \
               "$site_packages_dir"/torchvision-* \
               "$site_packages_dir"/nvidia* \
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

# Save hash only after successful install
echo "$DEPS_HASH" > "$HASH_FILE"
echo "✅ [Cluster-CI] Dependencies installed and cached."
