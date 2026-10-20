#!/usr/bin/env bash
# Keep orchestration tools outside the user-site directories smart_install purges.
set -euo pipefail

TOOLS_ROOT="${CLUSTER_CI_TOOLS_ROOT:-${HOME:-/home/user}/.local/share/cluster-ci-tools}"
TOOLS_BIN="$TOOLS_ROOT/bin"
mkdir -p "$TOOLS_BIN"

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

# --force also recreates missing entry points when a cached tool environment
# survives a prior user-site cleanup. A failed bootstrap must fail the job.
"$TOOLS_BIN/uv" tool install --force dvc --with dvc-http
"$TOOLS_BIN/uv" tool install --force git+https://github.com/UNIL-DESI/dvc-viewer.git
test -x "$TOOLS_BIN/dvc"
test -x "$TOOLS_BIN/dvc-viewer"
