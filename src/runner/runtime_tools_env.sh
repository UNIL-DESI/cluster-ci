#!/usr/bin/env bash
# Source inside containers to resolve platform tools consistently.
export CLUSTER_CI_TOOLS_ROOT="${CLUSTER_CI_TOOLS_ROOT:-${HOME:-/home/user}/.local/share/cluster-ci-tools}"
export PATH="$CLUSTER_CI_TOOLS_ROOT/bin:$PATH"
