#!/bin/bash
# GPU/Memory Watchdog — Runs on the HOST (not inside Docker)
# Monitors memory usage and kills the container if it exceeds the declared limit.
#
# On discrete GPUs: monitors nvidia-smi memory.used
# On unified memory (GB10/Grace): nvidia-smi reports [N/A], so we monitor
# system RAM via /proc/meminfo (since CPU+GPU share the same pool).
#
# Usage: gpu_watchdog.sh <container_name> <vram_limit_gb>
# Example: gpu_watchdog.sh cluster-job-abc123 70
#
# The watchdog polls every 5 seconds. If usage exceeds the limit for
# 3 consecutive checks (15s grace period), the container is killed.

CONTAINER_NAME="$1"
VRAM_LIMIT_GB="$2"

if [ -z "$CONTAINER_NAME" ] || [ -z "$VRAM_LIMIT_GB" ]; then
    echo "[GPU Watchdog] Usage: gpu_watchdog.sh <container_name> <vram_limit_gb>"
    exit 1
fi

# Detect memory monitoring mode
# On unified memory systems (GB10, Grace-Blackwell), nvidia-smi reports [N/A]
# for memory.total. In that case, we monitor system RAM instead.
MONITORING_MODE="nvidia-smi"
NVIDIA_MEM_CHECK=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1)

if [ -z "$NVIDIA_MEM_CHECK" ] || echo "$NVIDIA_MEM_CHECK" | grep -qi "N/A"; then
    MONITORING_MODE="system-ram"
    echo "[GPU Watchdog] Unified memory detected (nvidia-smi reports [N/A]). Monitoring system RAM via /proc/meminfo."
else
    echo "[GPU Watchdog] Discrete GPU detected. Monitoring via nvidia-smi."
fi

# Convert GB to MiB for comparison
VRAM_LIMIT_MIB=$((VRAM_LIMIT_GB * 1024))

echo "[GPU Watchdog] Started — Container: $CONTAINER_NAME, Memory limit: ${VRAM_LIMIT_GB}GB (${VRAM_LIMIT_MIB} MiB), Mode: $MONITORING_MODE"

CONSECUTIVE_OVER=0
THRESHOLD=3  # Kill after 3 consecutive violations (15s grace)

get_used_memory_mib() {
    if [ "$MONITORING_MODE" = "nvidia-smi" ]; then
        # Discrete GPU: query nvidia-smi
        nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | awk '{sum+=$1} END {print int(sum)}'
    else
        # Unified memory: read system RAM usage from /proc/meminfo
        # MemUsed = MemTotal - MemAvailable (includes GPU allocations on unified systems)
        awk '/^MemTotal:/ {total=$2} /^MemAvailable:/ {avail=$2} END {printf "%d", (total - avail) / 1024}' /proc/meminfo
    fi
}

while true; do
    sleep 5

    # Check if container is still running
    if ! docker inspect "$CONTAINER_NAME" --format '{{.State.Running}}' 2>/dev/null | grep -q "true"; then
        echo "[GPU Watchdog] Container $CONTAINER_NAME is no longer running. Exiting."
        exit 0
    fi

    # Query memory usage
    USED_MIB=$(get_used_memory_mib)

    if [ -z "$USED_MIB" ] || [ "$USED_MIB" = "0" ]; then
        CONSECUTIVE_OVER=0
        continue
    fi

    USED_GB=$(awk "BEGIN {printf \"%.1f\", $USED_MIB / 1024}")

    if [ "$USED_MIB" -gt "$VRAM_LIMIT_MIB" ]; then
        CONSECUTIVE_OVER=$((CONSECUTIVE_OVER + 1))
        echo "[GPU Watchdog] ⚠️  Memory usage ${USED_GB}GB > ${VRAM_LIMIT_GB}GB limit (violation $CONSECUTIVE_OVER/$THRESHOLD) [mode: $MONITORING_MODE]"

        if [ "$CONSECUTIVE_OVER" -ge "$THRESHOLD" ]; then
            echo "[GPU Watchdog] ❌ VRAM limit exceeded for ${THRESHOLD} consecutive checks. Killing container to protect the worker."
            echo "[GPU Watchdog] ❌ Erreur: Le job a dépassé la limite mémoire allouée (${VRAM_LIMIT_GB} GB, utilisé: ${USED_GB} GB). Le conteneur a été arrêté préventivement pour protéger le worker. Veuillez réduire la consommation mémoire ou augmenter REQUIRED_VRAM dans .cluster-ci"

            # Kill the container — this will cause docker exec to return 137
            docker kill "$CONTAINER_NAME" 2>/dev/null || true
            exit 0
        fi
    else
        if [ "$CONSECUTIVE_OVER" -gt 0 ]; then
            echo "[GPU Watchdog] ✅ Memory usage back to normal: ${USED_GB}GB / ${VRAM_LIMIT_GB}GB"
        fi
        CONSECUTIVE_OVER=0
    fi
done
