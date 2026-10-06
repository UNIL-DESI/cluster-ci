#!/bin/bash
# GPU/Memory Watchdog — Runs on the HOST (not inside Docker)
# Monitors memory usage and kills the container if it exceeds the declared limit
# or breaches host memory safety reserves (Grace-Blackwell GB10 Guard).
#
# On discrete GPUs: monitors per-GPU nvidia-smi memory.used AND host MemAvailable.
# On unified memory (GB10/Grace): nvidia-smi reports [N/A] or unified pool, so we
# monitor host MemAvailable via /proc/meminfo as the primary defense against UVM OOM.
#
# Usage: gpu_watchdog.sh <container_name> <vram_limit_gb>
# Example: gpu_watchdog.sh cluster-job-abc123 70
#
# SAFETY STRATEGY:
#   1. Host Reserve Guard (MemAvailable < HOST_MEMORY_RESERVE_GB, default 12 GiB):
#      IMMEDIATE kill on first breach. Prevents host lockup / kernel OOM crash.
#   2. Hard limit (90% of total system RAM):
#      IMMEDIATE kill on first violation.
#   3. Soft limit (user-declared VRAM_LIMIT):
#      Kill after 2 consecutive violations (grace period).
#
# Polling interval is <= 1s by default for ultra-fast reaction time.

CONTAINER_NAME="$1"
VRAM_LIMIT_GB="$2"

if [ -z "$CONTAINER_NAME" ] || [ -z "$VRAM_LIMIT_GB" ]; then
    echo "[GPU Watchdog] Usage: gpu_watchdog.sh <container_name> <vram_limit_gb>"
    exit 1
fi

# Configuration via environment variables
HOST_MEMORY_RESERVE_GB="${HOST_MEMORY_RESERVE_GB:-12}"
POLL_INTERVAL="${WATCHDOG_POLL_INTERVAL:-1}"
MARKER_FILE="${HOST_GUARD_MARKER_FILE:-host_guard_killed.marker}"

# Fail-fast: verify /proc/meminfo readability and required metrics
if [ ! -r /proc/meminfo ]; then
    echo "[GPU Watchdog] ❌ FATAL: /proc/meminfo is missing or unreadable. Host memory guard cannot operate safely!" >&2
    exit 1
fi

if ! grep -q "^MemTotal:" /proc/meminfo; then
    echo "[GPU Watchdog] ❌ FATAL: MemTotal entry missing in /proc/meminfo." >&2
    exit 1
fi

if ! grep -q "^MemAvailable:" /proc/meminfo; then
    echo "[GPU Watchdog] ❌ FATAL: MemAvailable entry missing in /proc/meminfo. Host memory guard cannot operate safely!" >&2
    exit 1
fi

# Detect memory monitoring mode
# On unified memory systems (GB10, Grace-Blackwell), nvidia-smi reports [N/A]
# for memory.total. In that case, we monitor system RAM instead.
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
HOST_RESERVE_MIB=$((HOST_MEMORY_RESERVE_GB * 1024))

# HARD LIMIT: 90% of total system RAM (absolute ceiling to protect the OS)
TOTAL_RAM_MIB=$(awk '/^MemTotal:/ {printf "%d", $2 / 1024}' /proc/meminfo)
HARD_LIMIT_MIB=$((TOTAL_RAM_MIB * 90 / 100))
HARD_LIMIT_GB=$(awk "BEGIN {printf \"%.0f\", $HARD_LIMIT_MIB / 1024}")

# Use the LOWER of user limit and hard limit
if [ "$VRAM_LIMIT_MIB" -gt "$HARD_LIMIT_MIB" ]; then
    echo "[GPU Watchdog] ⚠️  User limit (${VRAM_LIMIT_GB}GB) exceeds 90% of system RAM (${HARD_LIMIT_GB}GB). Capping soft limit to ${HARD_LIMIT_GB}GB."
    VRAM_LIMIT_MIB=$HARD_LIMIT_MIB
    VRAM_LIMIT_GB=$HARD_LIMIT_GB
fi

if [ "$VRAM_LIMIT_MIB" -gt 0 ]; then
    SOFT_DESC="${VRAM_LIMIT_GB}GB"
else
    SOFT_DESC="disabled (0GB)"
fi
echo "[GPU Watchdog] Started — Container: $CONTAINER_NAME, Soft limit: $SOFT_DESC, Hard limit: ${HARD_LIMIT_GB}GB (90% of ${TOTAL_RAM_MIB}MiB), Host reserve: ${HOST_MEMORY_RESERVE_GB}GB, Mode: $MONITORING_MODE"
echo "[GPU Watchdog] Poll interval: ${POLL_INTERVAL}s, Soft threshold: 2 violations, Hard/Reserve threshold: IMMEDIATE"

CONSECUTIVE_OVER=0
SOFT_THRESHOLD=2  # Kill after 2 consecutive soft violations

get_used_memory_mib() {
    if [ "$MONITORING_MODE" = "nvidia-smi" ]; then
        # Enforce the reservation on every GPU, without summing or averaging cards.
        nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | awk '$1 > maximum {maximum=$1} END {print int(maximum)}'
    else
        # Unified memory: read system RAM usage from /proc/meminfo
        # MemUsed = MemTotal - MemAvailable (includes GPU allocations on unified systems)
        awk '/^MemTotal:/ {total=$2} /^MemAvailable:/ {avail=$2} END {printf "%d", (total - avail) / 1024}' /proc/meminfo
    fi
}

get_available_memory_mib() {
    awk '/^MemAvailable:/ {printf "%d", $2 / 1024}' /proc/meminfo
}

get_culprit_container() {
    # In multi-job scenarios (multiple containers on the same host),
    # identify the highest memory consumer to avoid killing innocent jobs.
    local running_containers
    running_containers=$(docker ps --filter "name=cluster-" --format "{{.Names}}" 2>/dev/null)
    local count
    count=$(echo "$running_containers" | grep -v '^$' | wc -l 2>/dev/null || echo 0)

    if [ "$count" -le 1 ]; then
        echo "$CONTAINER_NAME"
        return
    fi

    local max_container="$CONTAINER_NAME"
    local max_bytes=0
    for c in $running_containers; do
        local cid
        cid=$(docker inspect "$c" --format '{{.Id}}' 2>/dev/null)
        local cur_bytes=0
        if [ -n "$cid" ]; then
            if [ -f "/sys/fs/cgroup/system.slice/docker-${cid}.scope/memory.current" ]; then
                cur_bytes=$(cat "/sys/fs/cgroup/system.slice/docker-${cid}.scope/memory.current" 2>/dev/null || echo 0)
            elif [ -f "/sys/fs/cgroup/docker/${cid}/memory.current" ]; then
                cur_bytes=$(cat "/sys/fs/cgroup/docker/${cid}/memory.current" 2>/dev/null || echo 0)
            elif [ -f "/sys/fs/cgroup/memory/docker/${cid}/memory.usage_in_bytes" ]; then
                cur_bytes=$(cat "/sys/fs/cgroup/memory/docker/${cid}/memory.usage_in_bytes" 2>/dev/null || echo 0)
            fi
        fi
        if [ "$cur_bytes" -gt "$max_bytes" ] 2>/dev/null; then
            max_bytes="$cur_bytes"
            max_container="$c"
        fi
    done
    echo "$max_container"
}

kill_container() {
    local reason="$1"
    local used_gb="$2"
    local avail_gb="$3"

    echo "[GPU Watchdog] ❌ $reason"
    echo "[GPU Watchdog] ❌ Error: Job exceeded allocated memory limit (used: ${used_gb}GB, available: ${avail_gb}GB). Container was preemptively stopped to protect the worker."
    echo "[GPU Watchdog] ❌ VRAM limit exceeded: Host memory guard enforced."

    # Write marker file for runner detection
    cat > "$MARKER_FILE" 2>/dev/null << EOF
{
  "status": "killed",
  "reason": "$reason",
  "container": "$CONTAINER_NAME",
  "used_gb": "${used_gb}",
  "available_gb": "${avail_gb}",
  "reserve_gb": "${HOST_MEMORY_RESERVE_GB}",
  "exit_code": 137
}
EOF

    # Kill the container — this will cause docker exec to return 137
    docker kill "$CONTAINER_NAME" 2>/dev/null || true
    exit 0
}

while true; do
    sleep "$POLL_INTERVAL"

    # Check if container is still running
    if ! docker inspect "$CONTAINER_NAME" --format '{{.State.Running}}' 2>/dev/null | grep -q "true"; then
        echo "[GPU Watchdog] Container $CONTAINER_NAME is no longer running. Exiting."
        exit 0
    fi

    # Read current MemAvailable
    AVAIL_MIB=$(get_available_memory_mib)
    if [ -z "$AVAIL_MIB" ]; then
        echo "[GPU Watchdog] ❌ FATAL: Failed to read MemAvailable from /proc/meminfo." >&2
        exit 1
    fi
    AVAIL_GB=$(awk "BEGIN {printf \"%.2f\", $AVAIL_MIB / 1024}")

    # Query memory usage
    USED_MIB=$(get_used_memory_mib)
    if [ -z "$USED_MIB" ] || [ "$USED_MIB" = "0" ]; then
        CONSECUTIVE_OVER=0
        continue
    fi
    USED_GB=$(awk "BEGIN {printf \"%.1f\", $USED_MIB / 1024}")

    # 1. HOST MEMORY RESERVE CHECK (MemAvailable < reserve) — IMMEDIATE KILL
    if [ "$AVAIL_MIB" -lt "$HOST_RESERVE_MIB" ]; then
        CULPRIT_CONTAINER=$(get_culprit_container)
        if [ "$CULPRIT_CONTAINER" != "$CONTAINER_NAME" ]; then
            echo "[GPU Watchdog] ⚠️ Host memory pressure detected (MemAvailable=${AVAIL_GB} GiB < reserve ${HOST_MEMORY_RESERVE_GB} GiB), but highest consumer is $CULPRIT_CONTAINER (sparing $CONTAINER_NAME)."
        else
            kill_container "HARD LIMIT BREACHED: killed by host memory guard: MemAvailable=${AVAIL_GB} GiB < reserve ${HOST_MEMORY_RESERVE_GB} GiB" "$USED_GB" "$AVAIL_GB"
        fi
    fi

    # 2. HARD LIMIT CHECK (90% of total RAM) — IMMEDIATE KILL, no grace period
    if [ "$USED_MIB" -gt "$HARD_LIMIT_MIB" ]; then
        kill_container "HARD LIMIT BREACHED: ${USED_GB}GB > ${HARD_LIMIT_GB}GB (90% of system RAM). Immediate kill to prevent system freeze." "$USED_GB" "$AVAIL_GB"
    fi

    # 3. SOFT LIMIT CHECK (user-declared limit) — Kill after consecutive violations
    if [ "$VRAM_LIMIT_MIB" -gt 0 ] && [ "$USED_MIB" -gt "$VRAM_LIMIT_MIB" ]; then
        CONSECUTIVE_OVER=$((CONSECUTIVE_OVER + 1))
        echo "[GPU Watchdog] ⚠️  Memory usage ${USED_GB}GB > ${VRAM_LIMIT_GB}GB limit (violation $CONSECUTIVE_OVER/$SOFT_THRESHOLD) [mode: $MONITORING_MODE]"

        if [ "$CONSECUTIVE_OVER" -ge "$SOFT_THRESHOLD" ]; then
            kill_container "Soft limit exceeded for ${SOFT_THRESHOLD} consecutive checks (${USED_GB}GB > ${VRAM_LIMIT_GB}GB)." "$USED_GB" "$AVAIL_GB"
            echo "[GPU Watchdog] ❌ VRAM limit exceeded"
        fi
    else
        if [ "$CONSECUTIVE_OVER" -gt 0 ]; then
            echo "[GPU Watchdog] ✅ Memory usage back to normal: ${USED_GB}GB / ${VRAM_LIMIT_GB}GB"
        fi
        CONSECUTIVE_OVER=0
    fi
done
