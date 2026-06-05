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
# SAFETY STRATEGY (two thresholds):
#   1. Soft limit (user-declared VRAM_LIMIT): Kill after 2 consecutive violations (4s grace).
#   2. Hard limit (90% of total system RAM): IMMEDIATE kill on first violation.
#      This prevents the system from freezing even if the user declares a limit
#      close to total RAM.
#
# The watchdog polls every 2 seconds for fast reaction time.

CONTAINER_NAME="$1"
VRAM_LIMIT_GB="$2"

if [ -z "$CONTAINER_NAME" ] || [ -z "$VRAM_LIMIT_GB" ]; then
    echo "[GPU Watchdog] Usage: gpu_watchdog.sh <container_name> <vram_limit_gb>"
    exit 1
fi

# Detect memory monitoring mode
# On unified memory systems (GB10, Grace-Blackwell), nvidia-smi reports [N/A]
# for memory.total. In that case, we monitor system RAM instead.
# Detection strategy: nvidia-smi --query-gpu=memory.total with nounits should return
# a numeric value (e.g., "24576") on discrete GPUs. On unified memory (GB10/Grace),
# it returns either "[N/A]", an empty string, or the nvidia-smi timestamp header.
# We check if the result is a valid positive integer to determine the mode.
MONITORING_MODE="nvidia-smi"
NVIDIA_MEM_CHECK=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d '[:space:]')

if echo "$NVIDIA_MEM_CHECK" | grep -qE '^[0-9]+$' && [ "$NVIDIA_MEM_CHECK" -gt 0 ] 2>/dev/null; then
    echo "[GPU Watchdog] Discrete GPU detected (VRAM: ${NVIDIA_MEM_CHECK} MiB). Monitoring via nvidia-smi."
else
    MONITORING_MODE="system-ram"
    echo "[GPU Watchdog] Unified memory detected (nvidia-smi memory.total=[$NVIDIA_MEM_CHECK]). Monitoring system RAM via /proc/meminfo."
fi

# Convert GB to MiB for comparison
VRAM_LIMIT_MIB=$((VRAM_LIMIT_GB * 1024))

# HARD LIMIT: 90% of total system RAM (absolute ceiling to protect the OS)
# On unified memory, this prevents the system from freezing even if the user
# declares a VRAM limit close to total RAM.
TOTAL_RAM_MIB=$(awk '/^MemTotal:/ {printf "%d", $2 / 1024}' /proc/meminfo)
HARD_LIMIT_MIB=$((TOTAL_RAM_MIB * 90 / 100))
HARD_LIMIT_GB=$(awk "BEGIN {printf \"%.0f\", $HARD_LIMIT_MIB / 1024}")

# Use the LOWER of user limit and hard limit
if [ "$VRAM_LIMIT_MIB" -gt "$HARD_LIMIT_MIB" ]; then
    echo "[GPU Watchdog] ⚠️  User limit (${VRAM_LIMIT_GB}GB) exceeds 90% of system RAM (${HARD_LIMIT_GB}GB). Capping soft limit to ${HARD_LIMIT_GB}GB."
    VRAM_LIMIT_MIB=$HARD_LIMIT_MIB
    VRAM_LIMIT_GB=$HARD_LIMIT_GB
fi

echo "[GPU Watchdog] Started — Container: $CONTAINER_NAME, Soft limit: ${VRAM_LIMIT_GB}GB, Hard limit: ${HARD_LIMIT_GB}GB (90% of ${TOTAL_RAM_MIB}MiB), Mode: $MONITORING_MODE"
echo "[GPU Watchdog] Poll interval: 2s, Soft threshold: 2 violations (4s), Hard threshold: IMMEDIATE"

CONSECUTIVE_OVER=0
SOFT_THRESHOLD=2  # Kill after 2 consecutive soft violations (4s grace)

POLL_INTERVAL=2   # Poll every 2 seconds (was 5)

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

kill_container() {
    local reason="$1"
    local used_gb="$2"
    echo "[GPU Watchdog] ❌ $reason"
    echo "[GPU Watchdog] ❌ Erreur: Le job a dépassé la limite mémoire allouée (utilisé: ${used_gb}GB). Le conteneur a été arrêté préventivement pour protéger le worker."
    echo "[GPU Watchdog] ❌ Veuillez réduire la consommation mémoire ou augmenter REQUIRED_VRAM dans .cluster-ci"

    # Kill the container — this will cause docker exec to return 137
    docker kill "$CONTAINER_NAME" 2>/dev/null || true
    exit 0
}

while true; do
    sleep $POLL_INTERVAL

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

    # HARD LIMIT CHECK (90% of total RAM) — IMMEDIATE KILL, no grace period
    if [ "$USED_MIB" -gt "$HARD_LIMIT_MIB" ]; then
        kill_container "HARD LIMIT BREACHED: ${USED_GB}GB > ${HARD_LIMIT_GB}GB (90% of system RAM). Immediate kill to prevent system freeze." "$USED_GB"
    fi

    # SOFT LIMIT CHECK (user-declared limit) — Kill after consecutive violations
    if [ "$USED_MIB" -gt "$VRAM_LIMIT_MIB" ]; then
        CONSECUTIVE_OVER=$((CONSECUTIVE_OVER + 1))
        echo "[GPU Watchdog] ⚠️  Memory usage ${USED_GB}GB > ${VRAM_LIMIT_GB}GB limit (violation $CONSECUTIVE_OVER/$SOFT_THRESHOLD) [mode: $MONITORING_MODE]"

        if [ "$CONSECUTIVE_OVER" -ge "$SOFT_THRESHOLD" ]; then
            kill_container "Soft limit exceeded for ${SOFT_THRESHOLD} consecutive checks (${USED_GB}GB > ${VRAM_LIMIT_GB}GB)." "$USED_GB"
        fi
    else
        if [ "$CONSECUTIVE_OVER" -gt 0 ]; then
            echo "[GPU Watchdog] ✅ Memory usage back to normal: ${USED_GB}GB / ${VRAM_LIMIT_GB}GB"
        fi
        CONSECUTIVE_OVER=0
    fi
done
