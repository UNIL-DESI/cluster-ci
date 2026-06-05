#!/bin/bash
# GPU Memory Watchdog — Runs on the HOST (not inside Docker)
# Monitors GPU VRAM usage and kills the container if it exceeds the REQUIRED_VRAM limit.
# This prevents the NVIDIA driver from crashing, which would take down the entire worker.
#
# Usage: gpu_watchdog.sh <container_name> <vram_limit_gb>
# Example: gpu_watchdog.sh cluster-job-abc123 70
#
# The watchdog polls nvidia-smi every 5 seconds. If VRAM usage exceeds
# the limit for 3 consecutive checks (15s grace period), the container is killed.
# Exit code 137 is set to trigger OOM detection in the pipeline.

CONTAINER_NAME="$1"
VRAM_LIMIT_GB="$2"

if [ -z "$CONTAINER_NAME" ] || [ -z "$VRAM_LIMIT_GB" ]; then
    echo "[GPU Watchdog] Usage: gpu_watchdog.sh <container_name> <vram_limit_gb>"
    exit 1
fi

# Convert GB to MiB for nvidia-smi comparison
VRAM_LIMIT_MIB=$(echo "$VRAM_LIMIT_GB * 1024" | bc | cut -d. -f1)

echo "[GPU Watchdog] Started — Container: $CONTAINER_NAME, VRAM limit: ${VRAM_LIMIT_GB}GB (${VRAM_LIMIT_MIB} MiB)"

CONSECUTIVE_OVER=0
THRESHOLD=3  # Kill after 3 consecutive violations (15s grace)

while true; do
    sleep 5

    # Check if container is still running
    if ! docker inspect "$CONTAINER_NAME" --format '{{.State.Running}}' 2>/dev/null | grep -q "true"; then
        echo "[GPU Watchdog] Container $CONTAINER_NAME is no longer running. Exiting."
        exit 0
    fi

    # Query GPU memory usage (all GPUs, sum total)
    VRAM_USED_MIB=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | awk '{sum+=$1} END {print int(sum)}')

    if [ -z "$VRAM_USED_MIB" ] || [ "$VRAM_USED_MIB" = "0" ]; then
        CONSECUTIVE_OVER=0
        continue
    fi

    VRAM_USED_GB=$(echo "scale=1; $VRAM_USED_MIB / 1024" | bc)

    if [ "$VRAM_USED_MIB" -gt "$VRAM_LIMIT_MIB" ]; then
        CONSECUTIVE_OVER=$((CONSECUTIVE_OVER + 1))
        echo "[GPU Watchdog] ⚠️  VRAM usage ${VRAM_USED_GB}GB > ${VRAM_LIMIT_GB}GB limit (violation $CONSECUTIVE_OVER/$THRESHOLD)"

        if [ "$CONSECUTIVE_OVER" -ge "$THRESHOLD" ]; then
            echo "[GPU Watchdog] ❌ VRAM limit exceeded for ${THRESHOLD} consecutive checks. Killing container to protect the worker."
            echo "[GPU Watchdog] ❌ Erreur: Le job a dépassé la limite REQUIRED_VRAM allouée (${VRAM_LIMIT_GB} GB, utilisé: ${VRAM_USED_GB} GB). Le conteneur a été arrêté préventivement pour protéger le worker. Veuillez augmenter REQUIRED_VRAM dans le fichier .cluster-ci"

            # Kill the container — this will cause docker exec to return 137
            docker kill "$CONTAINER_NAME" 2>/dev/null || true
            exit 0
        fi
    else
        if [ "$CONSECUTIVE_OVER" -gt 0 ]; then
            echo "[GPU Watchdog] ✅ VRAM usage back to normal: ${VRAM_USED_GB}GB / ${VRAM_LIMIT_GB}GB"
        fi
        CONSECUTIVE_OVER=0
    fi
done
