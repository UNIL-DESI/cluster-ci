#!/bin/bash
# Script to extract the native PyPI versions from the reference Docker image
# This script is automatically executed by the CI to keep constraints updated.
set -e

# Load .env if it exists
if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

DOCKER_IMAGE=${DOCKER_BASE_IMAGE:-"nvcr.io/nvidia/pytorch:26.05-py3"}

echo "🔍 Extracting constraints from image: $DOCKER_IMAGE..."

# Get Python version (filter out NVIDIA banner)
PYTHON_VERSION=$(docker run --rm "$DOCKER_IMAGE" python3 --version 2>&1 | grep '^Python ' | awk '{print $2}')
echo "🐍 Python version: $PYTHON_VERSION"

# Generate constraints file
# We extract pip freeze but we also want to ensure we have a clean list
# We'll filter for common packages and specific NVIDIA ones if needed (ignore NVIDIA banner)
docker run --rm "$DOCKER_IMAGE" pip freeze 2>&1 | sed -n -E 's/^([a-zA-Z0-9_.-]+)==([0-9]+.*)/\1==\2/p' > cluster_constraints.txt

# Prepend python version as a comment for the validator
sed -i "1i # Python: $PYTHON_VERSION" cluster_constraints.txt

echo "✅ cluster_constraints.txt updated."
