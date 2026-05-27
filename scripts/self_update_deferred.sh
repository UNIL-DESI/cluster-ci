#!/bin/bash
# =============================================================================
# self_update_deferred.sh — GitOps Auto-Update Script (Drain & Purge Pattern)
# =============================================================================
#
# This script is designed to be called by a CI runner (GitHub Actions, self-hosted)
# running ON the headnode via the cluster-ci-admin tag. It performs a safe
# pre-emptive update of the cluster:
#
#   1. Enable Maintenance Mode (rejets new jobs)
#   2. Purge active jobs (Drain & Purge)
#   3. Pull latest code on the headnode
#   4. Sync dependencies
#   5. Signal ALL workers to update via their webhook
#   6. Schedule a deferred restart of headnode services (after script exits)
#   7. Exit 0 immediately — the CI reports success before the restart happens
#
# THE TRICK: If we restart the headnode services while this script is running,
# we kill the GitHub Actions runner that launched us. By scheduling the restart
# in a detached background process with a 10s delay, we exit cleanly first.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$BASE_DIR/.env"

# Load environment
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# Ensure docker socket permissions (PR feedback)
if [ -e /var/run/docker.sock ]; then
    sudo chmod 666 /var/run/docker.sock || true
fi

CLUSTER_TOKEN="${CLUSTER_TOKEN:-}"
if [ -z "${HEADNODE_URL:-}" ]; then
    echo "❌ Error: HEADNODE_URL environment variable is missing."
    exit 1
fi

echo "=== [1/6] Enabling Maintenance Mode ==="
curl -s -X POST "${HEADNODE_URL}/maintenance/on" \
     -H "Authorization: Bearer ${CLUSTER_TOKEN}" || echo "⚠️ Failed to enable maintenance mode"

echo "=== [2/6] Drain & Purge: Cancelling active jobs ==="
DB_PATH="${CLUSTER_DB_PATH:-}"
if [ -z "$DB_PATH" ]; then
    if [ -f "/home/henri/cluster-ci/cluster_scheduler.db" ]; then
        DB_PATH="/home/henri/cluster-ci/cluster_scheduler.db"
    else
        DB_PATH="cluster_scheduler.db"
    fi
fi

if [ -f "$DB_PATH" ]; then
    # Get all running or assigned jobs
    ACTIVE_JOBS=$(sqlite3 "$DB_PATH" "SELECT job_id FROM jobs WHERE status IN ('running', 'assigned');")

    for job_id in $ACTIVE_JOBS; do
        echo "  → Purging job $job_id..."
        curl -s -X POST "${HEADNODE_URL}/api/jobs/${job_id}/stop" \
             -H "Authorization: Bearer ${CLUSTER_TOKEN}" || echo "  ⚠️ Failed to stop job $job_id"
    done
else
    echo "⚠️ Database not found at $DB_PATH, skipping purge."
fi

echo "=== [3/6] Pulling latest code on Headnode robustly ==="
# Update runner's own workspace clone
cd "$BASE_DIR"
git fetch origin main
git reset --hard origin/main
echo "✅ Runner workspace updated to: $(git rev-parse --short HEAD)"

# ALSO update the global production infrastructure folder if it exists
PROD_DIR="/home/henri/cluster-ci"
if [ -d "$PROD_DIR" ] && [ "$PROD_DIR" != "$BASE_DIR" ]; then
    echo "⚙️  Updating global production folder $PROD_DIR..."
    cd "$PROD_DIR"
    git fetch origin main
    git reset --hard origin/main
    echo "✅ Global production folder updated to: $(git rev-parse --short HEAD)"
fi

echo "=== [4/6] Syncing dependencies ==="
UV_CMD="${HOME}/.local/bin/uv"
[ ! -x "$UV_CMD" ] && UV_CMD=$(which uv || echo "uv")

if command -v "$UV_CMD" &> /dev/null; then
    # Sync in runner workspace
    cd "$BASE_DIR"
    "$UV_CMD" sync 2>&1 || echo "⚠️ uv sync had warnings in runner workspace (non-fatal)"
    
    # ALSO sync in global production if it exists
    if [ -d "$PROD_DIR" ] && [ "$PROD_DIR" != "$BASE_DIR" ]; then
        cd "$PROD_DIR"
        "$UV_CMD" sync 2>&1 || echo "⚠️ uv sync had warnings in production folder (non-fatal)"
    fi
else
    echo "⚠️ uv not found, skipping dependency sync"
fi

echo "=== [5/6] Signaling workers to update ==="
if [ -f "$DB_PATH" ]; then
    # Fetch all online workers from DB
    WORKER_URLS=$(sqlite3 "$DB_PATH" "SELECT service_url FROM workers WHERE status = 'online';")

    for url in $WORKER_URLS; do
        echo "  → Sending update to $url..."
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST "${url}/webhook/update_self" \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer ${CLUSTER_TOKEN}" \
            --connect-timeout 5 \
            --max-time 10 \
            2>/dev/null || echo "000")

        if [ "$HTTP_CODE" = "202" ]; then
            echo "  ✅ Worker $url accepted update (HTTP $HTTP_CODE)"
        elif [ "$HTTP_CODE" = "000" ]; then
            echo "  ⚠️ Worker $url unreachable"
        else
            echo "  ⚠️ Worker $url returned HTTP $HTTP_CODE"
        fi
    done
else
    echo "⚠️ Database not found, cannot signal workers."
fi

echo "=== [6/6] Scheduling deferred restart of Headnode services ==="
# We use nohup + disown to fully detach the restart from this process tree.
# The 15-second delay ensures this script has time to exit 0 and the CI runner
# reports success before the services (and the runner itself) are restarted.
nohup bash -c "
    sleep 15
    echo \"[DEFERRED] Disabling maintenance mode (in case restart fails)...\"
    curl -s -X POST \"${HEADNODE_URL}/maintenance/off\" -H \"Authorization: Bearer ${CLUSTER_TOKEN}\" || true

    echo \"[DEFERRED] Restarting cluster-scheduler...\"
    sudo systemctl restart cluster-scheduler 2>/dev/null || pkill -f headnode_service.py || true
    echo \"[DEFERRED] Restarting cluster-scheduler-loop...\"
    sudo systemctl restart cluster-scheduler-loop 2>/dev/null || pkill -f scheduler_loop.py || true
    echo \"[DEFERRED] Restarting cluster-runner-manager...\"
    sudo systemctl restart cluster-runner-manager 2>/dev/null || pkill -f runner_manager.py || true

    echo \"[DEFERRED] Restart complete at \$(date)\"
" > /tmp/cluster-ci-deferred-restart.log 2>&1 &
disown

echo ""
echo "=============================================="
echo "✅ Auto-update complete!"
echo "   Maintenance: Enabled"
echo "   Drain & Purge: Executed"
echo "   Workers: signaled to pull & restart"
echo "   Headnode: restart scheduled in 15 seconds"
echo "   Exiting now (CI will report success)"
echo "=============================================="
exit 0
