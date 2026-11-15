#!/usr/bin/env bash
# Keep orchestration tools outside the user-site directories smart_install purges.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/runtime_tools_env.sh"
TOOLS_ROOT="$CLUSTER_CI_TOOLS_ROOT"
TOOLS_BIN="$TOOLS_ROOT/bin"
mkdir -p "$TOOLS_BIN"

# Containers of the same project/image share this prefix. Serialize first-time
# setup, and never replace a healthy tool environment underneath a live process.
exec 201>"$TOOLS_ROOT/bootstrap.lock"
flock -w 600 201

# Explicit paths avoid the uv shim and any broken legacy ~/.local/bin launchers.
if ! "$TOOLS_BIN/uv" --version >/dev/null 2>&1; then
    if ! (curl -LsSf https://astral.sh/uv/install.sh |
          env UV_INSTALL_DIR="$TOOLS_BIN" UV_NO_MODIFY_PATH=1 sh); then
        # The PyPI wheel contains a standalone executable. Do not leave a console
        # script depending on the research project's disposable user site.
        python3 -m pip install --upgrade --target "$TOOLS_ROOT/uv-python" uv
        ln -sf "$TOOLS_ROOT/uv-python/bin/uv" "$TOOLS_BIN/uv"
        ln -sf "$TOOLS_ROOT/uv-python/bin/uvx" "$TOOLS_BIN/uvx"
    fi
fi
"$TOOLS_BIN/uv" --version

export UV_TOOL_DIR="$TOOLS_ROOT/venvs"
export UV_TOOL_BIN_DIR="$TOOLS_BIN"

# Reuse valid cached installations. --force is only for initial setup or repair,
# not every container start: it removes modules from already-running DVC jobs.
if ! "$TOOLS_BIN/dvc" --version >/dev/null 2>&1; then
    "$TOOLS_BIN/uv" tool install --force dvc --with dvc-http
fi
if ! "$TOOLS_BIN/dvc-viewer" --help >/dev/null 2>&1; then
    "$TOOLS_BIN/uv" tool install --force git+https://github.com/UNIL-DESI/dvc-viewer.git
fi
"$TOOLS_BIN/dvc" --version >/dev/null
"$TOOLS_BIN/dvc-viewer" --help >/dev/null
