#!/bin/bash
# Asynchronous Watchdog to incrementally backup DVC.

MAIN_CONTAINER_NAME="$1"

if [ -z "$MAIN_CONTAINER_NAME" ]; then
    echo "Usage: $0 <MAIN_CONTAINER_NAME>"
    exit 1
fi

LAST_MOD=0
LOCK_FILE="dvc.lock"

# Wait a few seconds initially to let things settle
sleep 2

while true; do
    if [ -f "$LOCK_FILE" ]; then
        CURRENT_MOD=$(stat -c %Y "$LOCK_FILE" 2>/dev/null || echo 0)
        if [ "$CURRENT_MOD" -gt "$LAST_MOD" ]; then
            if [ "$LAST_MOD" -ne 0 ]; then
                # Wait 2 seconds to ensure DVC finishes writing to lock file
                sleep 2
                echo "[Watchdog] dvc.lock modification detected. Syncing to Git..."
                docker exec \
                    -e HEADNODE_URL="$HEADNODE_URL" \
                    -e CLUSTER_CI_MODE=executor \
                    -e CLUSTER_CI_GPU_REQUIRED="$CLUSTER_CI_GPU_REQUIRED" \
                    "${MAIN_CONTAINER_NAME}" bash -c "export PATH=/home/user/shims:\$PATH:/home/user/.local/bin && uv run --with ruamel.yaml python3 /cluster-ci/src/runner/dvc_git_helper.py sync" || echo "[Watchdog] Warning: sync failed, will retry next modification."
                
                # Re-fetch CURRENT_MOD in case the sync itself modified dvc.lock or took a long time
                CURRENT_MOD=$(stat -c %Y "$LOCK_FILE" 2>/dev/null || echo "$CURRENT_MOD")
            fi
            LAST_MOD=$CURRENT_MOD
        fi
    fi
    sleep 2
done
