#!/bin/bash
set -e

if [ "$#" -lt 2 ]; then
    echo "Usage: $0 <owner/repo> <branch_name>"
    echo "Example: $0 hjamet/llm-as-recommender main"
    exit 1
fi

CLI_TARGET_REPO=$1
CLI_TARGET_BRANCH=$2
CLI_GH_TOKEN="${3:-$GH_TOKEN}"

# Capture the caller's commit hash before changing directory
if [ -z "$CALLER_COMMIT_SHA" ]; then
    if [ -n "$GITHUB_SHA" ]; then
        CALLER_COMMIT_SHA="$GITHUB_SHA"
    elif git rev-parse HEAD &>/dev/null; then
        CALLER_COMMIT_SHA=$(git rev-parse HEAD)
    fi
fi
export CALLER_COMMIT_SHA

if [ "$CLI_TARGET_BRANCH" = "cluster-run" ]; then
    echo "Detecting original draft branch for tag cluster-run in caller repository..."
    git fetch origin "+refs/heads/cluster-draft/*:refs/remotes/origin/cluster-draft/*" --quiet || true
    DRAFT_BRANCH=$(git branch -r --contains HEAD | grep -o 'origin/cluster-draft/[^ ]*' | head -n 1 | sed 's|origin/||')
    if [ -n "$DRAFT_BRANCH" ]; then
        echo "Resolved draft branch: $DRAFT_BRANCH"
        CLI_TARGET_BRANCH="$DRAFT_BRANCH"
    else
        echo "Error: Failed to resolve original draft branch for tag cluster-run. Cannot proceed." >&2
        exit 1
    fi
fi

# Go to cluster-ci project root
SCRIPT_PATH=$(readlink -f "${BASH_SOURCE[0]}")
BASE_DIR="$( cd "$( dirname "$SCRIPT_PATH" )/../.." >/dev/null 2>&1 && pwd )"
cd "$BASE_DIR"

# Injection des variables d'environnement globales (.env et .env.secrets)
if [ -f "$BASE_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$BASE_DIR/.env" || true
    set +a
fi
if [ -f "$BASE_DIR/.env.secrets" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$BASE_DIR/.env.secrets" || true
    set +a
fi

# Log utilities defined early to support logs during early setup/delegation steps.
function log_info() {
    echo -e "[$(date +'%Y-%m-%d %H:%M:%S')] ℹ️  $1"
}

function log_warn() {
    echo -e "[$(date +'%Y-%m-%d %H:%M:%S')] ⚠️  $1"
}

function log_success() {
    echo -e "[$(date +'%Y-%m-%d %H:%M:%S')] ✅ $1"
}

function log_error() {
    echo -e "[$(date +'%Y-%m-%d %H:%M:%S')] ❌ $1"
}

TARGET_REPO=${CLI_TARGET_REPO:-$TARGET_REPO}
TARGET_BRANCH=${CLI_TARGET_BRANCH:-$TARGET_BRANCH}

# Prioritize local GITHUB_PAT or GH_TOKEN from local environment files over the CLI token argument
if [ -n "$GITHUB_PAT" ]; then
    GH_TOKEN="$GITHUB_PAT"
elif [ -z "$GH_TOKEN" ]; then
    GH_TOKEN="$CLI_GH_TOKEN"
fi
JOB_ID=${JOB_ID:-"manual-$(date +%s)"}

# Robust Container Naming & Labeling
SAFE_JOB_ID=$(echo "$JOB_ID" | tr '/' '-')
MAIN_CONTAINER_NAME="cluster-job-${SAFE_JOB_ID}"
VIEWER_CONTAINER_NAME="cluster-viewer-${SAFE_JOB_ID}"
COMMON_LABELS="--label cluster-ci-job=${JOB_ID} --label cluster-ci-repo=${TARGET_REPO}"

# Delegation mode: If not explicitly in executor mode,
# delegate the task to the scheduler via submit_job.py
if [ "$CLUSTER_CI_MODE" != "executor" ]; then
    if [ -z "$HEADNODE_URL" ]; then
        echo "Error: HEADNODE_URL is not set. In delegation mode, the GHA runner must know the scheduler address." >&2
        echo "   Please define HEADNODE_URL in the host environment or in .env." >&2
        exit 1
    fi
    echo "🌐 Delegation Mode enabled. Submitting job to scheduler..."

    # JIT Network Connectivity check (timeout: 3s)
    # Fail fast and gracefully without spawning ppng log pipes if the headnode is offline
    echo "Connecting to headnode at $HEADNODE_URL (checking connectivity)..."
    if ! python3 -c "import requests, sys; requests.get('$HEADNODE_URL/check_space', timeout=3)" &>/dev/null; then
        echo "Error: Connection to headnode at $HEADNODE_URL timed out or failed (limit: 3s)." >&2
        echo "   Please check that the headnode service is running and accessible." >&2
        exit 1
    fi
    
    # TRAP: Prevent bash from exiting instantly on GitHub Action cancellation (SIGTERM)
    # This ensures the python script receives the signal and completes its graceful cancellation HTTP call.
    LOG_FILE="/tmp/cluster_job_${CALLER_COMMIT_SHA}.log"
    STREAM_PIPE="/tmp/cluster_pipe_${CALLER_COMMIT_SHA}"
    
    rm -f "$LOG_FILE" "$STREAM_PIPE"
    touch "$LOG_FILE"
    mkfifo "$STREAM_PIPE"

    # Start curl reading from the pipe
    curl -s -X POST -H "Content-Type: text/plain" -T "$STREAM_PIPE" -N "https://ppng.io/cluster-ci-log-${CALLER_COMMIT_SHA}" >/dev/null &
    CURL_PID=$!

    # Start tail pushing logs to the pipe
    tail -f "$LOG_FILE" > "$STREAM_PIPE" &
    TAIL_PID=$!

    # Start heartbeat pushing to the pipe
    (
        while true; do
            sleep 10
            echo "♥" 2>/dev/null || break
        done
    ) > "$STREAM_PIPE" &
    HEARTBEAT_PID=$!

    cleanup_delegation() {
        echo "Cleaning up delegation resources..."
        if [ -n "$TAIL_PID" ]; then kill "$TAIL_PID" 2>/dev/null || true; fi
        if [ -n "$HEARTBEAT_PID" ]; then kill "$HEARTBEAT_PID" 2>/dev/null || true; fi
        if [ -n "$CURL_PID" ]; then kill "$CURL_PID" 2>/dev/null || true; fi
        rm -f "$LOG_FILE" "$STREAM_PIPE" 2>/dev/null || true
    }
    trap 'echo "🛑 Bash received termination signal. Waiting for python to gracefully cancel the job..."; cleanup_delegation' TERM INT EXIT

    set +e
    if [ -n "$GH_TOKEN" ]; then
        python3 -u "$BASE_DIR/src/scheduler/submit_job.py" "$TARGET_REPO" "$TARGET_BRANCH" --gh-token "$GH_TOKEN" 2>&1 | stdbuf -oL -eL tee "$LOG_FILE"
        SUBMIT_RET=${PIPESTATUS[0]}
    else
        python3 -u "$BASE_DIR/src/scheduler/submit_job.py" "$TARGET_REPO" "$TARGET_BRANCH" 2>&1 | stdbuf -oL -eL tee "$LOG_FILE"
        SUBMIT_RET=${PIPESTATUS[0]}
    fi
    set -e

    cleanup_delegation
    trap - TERM INT EXIT
    exit $SUBMIT_RET
fi

REPO_WORK_DIR="repositories/$TARGET_REPO"

# Graceful kill with timeout fallback to SIGKILL
_kill_with_timeout() {
    local pid=$1
    local grace=${2:-5}
    if [ -z "$pid" ]; then return 0; fi
    kill "$pid" 2>/dev/null || return 0
    local i=0
    while [ $i -lt $grace ] && kill -0 "$pid" 2>/dev/null; do
        sleep 1
        i=$((i + 1))
    done
    kill -9 "$pid" 2>/dev/null || true
}

echo "=========================================================================="
log_info "CLUSTER-CI: GitOps Runner Orchestration Start"
log_info "   Target Repo   : $TARGET_REPO"
log_info "   Target Branch : $TARGET_BRANCH"
log_info "   Run Folder    : $BASE_DIR/$REPO_WORK_DIR"
log_info "   Machine       : $(hostname)"
log_info "   IP Address    : $(hostname -I 2>/dev/null | awk '{print $1}' || echo 'unknown')"
log_info "   Total RAM     : $(free -g 2>/dev/null | awk '/Mem/{print $2}' || echo '?') GB"
log_info "   GPU           : $(nvidia-smi --query-gpu=gpu_name --format=csv,noheader 2>/dev/null | head -1 || echo 'none')"
log_info "   VRAM          : $(nvidia-smi --query-gpu=memory.total --format=csv,noheader 2>/dev/null | head -1 || echo 'N/A')"
echo "=========================================================================="

echo "===STAGE:setup:BEGIN==="

# 1. Creation / switch to repositories/
log_info "[Step 1/3] Initializing local cache..."
mkdir -p "$BASE_DIR/repositories/$(dirname "$TARGET_REPO")"
cd "$BASE_DIR/repositories/$(dirname "$TARGET_REPO")"

# Extract just the final repo name for the folder (e.g., llm-as-recommender)
REPO_BASENAME=$(basename "$TARGET_REPO")

if [ -n "$GH_TOKEN" ]; then
    # Silent https authentication for GitHub Actions
    REPO_URL="https://x-access-token:${GH_TOKEN}@github.com/${TARGET_REPO}.git"
else
    REPO_URL="https://github.com/${TARGET_REPO}.git"
fi

# 1.5 JIT Garbage Collection & Metadata update
log_info "[Step 1.5/3] JIT Garbage Collection (GC) Management..."
if [ -n "$JOB_ID" ]; then
    SAFE_JOB_ID=$(echo "$JOB_ID" | tr -dc 'a-zA-Z0-9_-')
    log_info "Preventive purge of containers for job $SAFE_JOB_ID..."
    docker rm -f "cluster-job-$SAFE_JOB_ID" "cluster-viewer-$SAFE_JOB_ID" 2>/dev/null || true
fi
log_info "Scanning for zombie containers (JIT Zombie GC)..."
python3 "$BASE_DIR/src/runner/gc_orchestrator.py" run-zombie-gc
python3 "$BASE_DIR/src/runner/gc_orchestrator.py" run-gc
python3 "$BASE_DIR/src/runner/gc_orchestrator.py" update-running "$TARGET_REPO"

function cleanup_job_resources() {
    log_info "Cleaning up job resources for ${JOB_ID}..."
    # Graceful stop then force remove
    docker stop "${MAIN_CONTAINER_NAME}" "${VIEWER_CONTAINER_NAME}" 2>/dev/null || true
    docker rm -f "${MAIN_CONTAINER_NAME}" "${VIEWER_CONTAINER_NAME}" 2>/dev/null || true

    log_info "Updating metadata (idle status)..."
    if [ -n "$SAFE_JOB_ID" ]; then
        docker stop "cluster-viewer-$SAFE_JOB_ID" 2>/dev/null || true
        docker rm -f "cluster-viewer-$SAFE_JOB_ID" 2>/dev/null || true
        rm -f "/tmp/tmate_${SAFE_JOB_ID}.sock" "/tmp/tmate_${SAFE_JOB_ID}.conf" 2>/dev/null || true
    fi

    # Kill host dvc-viewer process associated with this job's port or job ID
    if [ -n "$VIEWER_PORT" ]; then
        log_info "Cleaning up host dvc-viewer processes on port ${VIEWER_PORT}..."
        for pid in $(pgrep -f "dvc-viewer.*--port ${VIEWER_PORT}" || true); do
            log_info "Killing host dvc-viewer process (PID: $pid) on port ${VIEWER_PORT}..."
            kill -9 "$pid" 2>/dev/null || true
        done
    fi
    # Backup cleanup of any leftover dvc-viewer process globally on host since this job is ending
    for pid in $(pgrep -f "dvc-viewer" || true); do
        log_info "Force-cleaning residual dvc-viewer process on host (PID: $pid)..."
        kill -9 "$pid" 2>/dev/null || true
    done

    [ -n "$DVC_VIEWER_PID" ] && kill -9 "$DVC_VIEWER_PID" 2>/dev/null || true
    [ -n "$WATCHDOG_PID" ] && kill -9 "$WATCHDOG_PID" 2>/dev/null || true
    [ -n "$GPU_WATCHDOG_PID" ] && kill -9 "$GPU_WATCHDOG_PID" 2>/dev/null || true
    # Kill pipeline siblings (gpu_watchdog.sh survives when only tee PID is killed)
    pkill -9 -f "gpu_watchdog.sh" 2>/dev/null || true
    pkill -9 -f "dvc_watchdog.sh" 2>/dev/null || true
    python3 "$BASE_DIR/src/runner/gc_orchestrator.py" update-idle "$TARGET_REPO" "$BASE_DIR/repositories/$TARGET_REPO"
    log_info "Running post-flight Maintenance GC (Lazy Transfer)..."
    python3 "$BASE_DIR/src/runner/gc_orchestrator.py" run-transfer-gc
}
# Trap EXIT, SIGINT, and SIGTERM to ensure cleanup
trap cleanup_job_resources EXIT SIGINT SIGTERM

# 2. Preventive Purge & Git State Management
log_info "[Step 2/3] Preventive purge of residual containers and processes..."
# 2.1 Cleanup containers for this specific job ID
# This ensures that if a previous attempt of the SAME job failed/crashed, we clean it up.
docker rm -f "${MAIN_CONTAINER_NAME}" "${VIEWER_CONTAINER_NAME}" 2>/dev/null || true

# 2.2 Cleanup residual dvc-viewer processes globally on the host worker.
# Since the worker only runs one research job at a time, any leftover dvc-viewer
# process on the host is a stale orphan from a previous job and must be purged.
for pid in $(pgrep -f "dvc-viewer" || true); do
    log_info "Cleaning up leftover dvc-viewer process on host (PID: $pid)..."
    kill -9 "$pid" 2>/dev/null || true
done

if [ ! -d "$REPO_BASENAME/.git" ]; then
    log_info "[Step 2.1/3] First repository fetch. Cloning in progress..."
    git clone "$REPO_URL" "$REPO_BASENAME"
else
    log_info "[Step 2.1/3] Existing repository found. Updating..."
fi

cd "$REPO_BASENAME"

# Force remote URL in case it changed (ephemeral token)
git remote set-url origin "$REPO_URL"

# Force fetching latest references (explicitly specify branch mapping to origin/branch
# as GitHub Actions conditional fetch sometimes omits it)
if [ "$TARGET_BRANCH" = "cluster-run" ]; then
    log_info "Triggered by tag 'cluster-run'. Fetching tag reference and draft branches..."
    for i in {1..5}; do
        if git fetch origin "+refs/tags/cluster-run:refs/tags/cluster-run"; then
            git fetch origin "+refs/heads/cluster-draft/*:refs/remotes/origin/cluster-draft/*" || true
            break
        fi
        log_warn "Failed to fetch tag cluster-run (attempt $i/5). Retrying in 5 seconds..."
        sleep 5
    done

    # Switch and hard reset to ensure clean Git tree
    # Preventive cleanup: remove stale lock files left by a killed git process (OOM, crash, etc.)
    rm -f .git/index.lock .git/refs/heads/*.lock .git/HEAD.lock 2>/dev/null || true
    log_info "Checking out tag cluster-run..."
    git checkout -f "refs/tags/cluster-run"
    git reset --hard "refs/tags/cluster-run"

    # Detect the original draft branch associated with this commit
    log_info "Detecting draft branch associated with tag cluster-run..."
    # Allow git branch to output branches containing HEAD
    DRAFT_BRANCH=$(git branch -r --contains HEAD | grep -o 'origin/cluster-draft/[^ ]*' | head -n 1 | sed 's|origin/||')
    if [ -n "$DRAFT_BRANCH" ]; then
        log_info "Detected draft branch: $DRAFT_BRANCH"
        TARGET_BRANCH="$DRAFT_BRANCH"
        # Reset and check out the local branch tracking the remote draft branch
        git checkout -f -B "$TARGET_BRANCH" "origin/$TARGET_BRANCH"
        git reset --hard "origin/$TARGET_BRANCH"
    else
        log_warn "No draft branch containing this commit was found on origin. Running directly on detached tag cluster-run."
    fi
else
    log_info "Synchronizing remote reference origin/$TARGET_BRANCH..."
    for i in {1..5}; do
        if git fetch origin "+refs/heads/$TARGET_BRANCH:refs/remotes/origin/$TARGET_BRANCH"; then
            break
        fi
        log_warn "Failed to fetch origin/$TARGET_BRANCH (attempt $i/5). Retrying in 5 seconds..."
        sleep 5
    done

    # Security validation: does the branch exist on remote?
    if ! git rev-parse --verify "origin/$TARGET_BRANCH" >/dev/null 2>&1; then
        log_error "Branch origin/$TARGET_BRANCH does not exist or was not found."
        exit 1
    fi

    # Switch and hard reset to ensure clean Git tree
    # Preventive cleanup: remove stale lock files left by a killed git process (OOM, crash, etc.)
    rm -f .git/index.lock .git/refs/heads/*.lock .git/HEAD.lock 2>/dev/null || true
    log_info "Forced branch checkout and re-synchronization..."
    git checkout -f -B "$TARGET_BRANCH" "origin/$TARGET_BRANCH"
    git reset --hard "origin/$TARGET_BRANCH"
fi

# Register current commit hash for traceability
git rev-parse HEAD > .cluster-ci-commit

log_success "Git tree synchronized. Artifacts (.dvc/cache etc.) preserved for reuse."

# Register current commit hash for traceability
git rev-parse HEAD > .cluster-ci-commit

# 3. Launch Dockerized Execution
log_info "[Step 3/3] Preparing Dockerized execution..."

if [ ! -f ".cluster-ci" ]; then
    log_error ".cluster-ci file not found at repository root. Execution aborted."
    exit 1
fi

# Extract RAM limit from .cluster-ci (--ram 16 or REQUIRED_RAM=16GB)
RAM_LIMIT=$(grep -oE -e 'REQUIRED_RAM=[0-9.]+' .cluster-ci | cut -d= -f2 | head -n 1)
[ -z "$RAM_LIMIT" ] && RAM_LIMIT=$(grep -oE -e '--ram [0-9.]+' .cluster-ci | awk '{print $2}' | head -n 1)
[ -z "$RAM_LIMIT" ] && RAM_LIMIT="2"
log_info "RAM limit detected (placement constraint): ${RAM_LIMIT}GB"

# Docker cgroups memory enforcement: the container is hard-limited to REQUIRED_RAM.
# If user code exceeds this, Docker OOM-kills the container process — NOT the host.
# --memory-swap equal to --memory disables swap (prevents silent degradation).
# NOTE: The scheduler already rejects jobs where REQUIRED_RAM > (worker_total_ram - 8GB),
# so by the time we get here, RAM_LIMIT is always safe for this worker.
DOCKER_MEMORY_FLAG="--memory=${RAM_LIMIT}g --memory-swap=${RAM_LIMIT}g"
log_info "Docker memory hard-limit: ${RAM_LIMIT}GB (cgroups enforced, no swap)"

# Extract VRAM limit from .cluster-ci (REQUIRED_VRAM=70GB)
# This is enforced by the GPU watchdog (nvidia-smi monitoring), not by Docker.
VRAM_LIMIT=$(grep -oE -e 'REQUIRED_VRAM=[0-9.]+' .cluster-ci | cut -d= -f2 | head -n 1)
[ -z "$VRAM_LIMIT" ] && VRAM_LIMIT="0"

# On unified memory systems, ALWAYS enable the watchdog even if REQUIRED_VRAM=0.
# Use REQUIRED_RAM as the VRAM limit since RAM and VRAM share the same pool.
if [ "$VRAM_LIMIT" = "0" ]; then
    # Check if this is a unified memory system
    NVIDIA_MEM_TEST=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d '[:space:]')
    if ! echo "$NVIDIA_MEM_TEST" | grep -qE '^[0-9]+$'; then
        VRAM_LIMIT="$RAM_LIMIT"
        log_info "Unified memory detected: auto-enabling GPU watchdog with VRAM limit = RAM limit (${VRAM_LIMIT}GB)"
    fi
fi

if [ "$VRAM_LIMIT" != "0" ]; then
    log_info "VRAM limit detected: ${VRAM_LIMIT}GB (GPU watchdog will enforce)"
else
    log_info "No VRAM limit declared. GPU watchdog disabled."
fi


# Configuration Docker — Architecture-aware image selection
HOST_ARCH=$(uname -m)
if [ "$HOST_ARCH" = "x86_64" ] || [ "$HOST_ARCH" = "amd64" ]; then
    DOCKER_PLATFORM="linux/amd64"
    DOCKER_IMAGE=${DOCKER_IMAGE_AMD64:-${DOCKER_BASE_IMAGE:-"nvcr.io/nvidia/pytorch:26.05-py3"}}

    # Surcharges projets dans .cluster-ci
    PROJECT_IMAGE_AMD64=$(grep -oE 'DOCKER_IMAGE_AMD64=[^ ]+' .cluster-ci 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" | head -n 1)
    PROJECT_IMAGE_GLOBAL=$(grep -oE 'DOCKER_IMAGE=[^ ]+' .cluster-ci 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" | head -n 1)
    PROJECT_DOCKER_IMAGE=${PROJECT_IMAGE_AMD64:-$PROJECT_IMAGE_GLOBAL}

    PROJECT_PLATFORM_AMD64=$(grep -oE 'DOCKER_PLATFORM_AMD64=[^ ]+' .cluster-ci 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" | head -n 1)
    PROJECT_PLATFORM_GLOBAL=$(grep -oE 'DOCKER_PLATFORM=[^ ]+' .cluster-ci 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" | head -n 1)
    PROJECT_DOCKER_PLATFORM=${PROJECT_PLATFORM_AMD64:-$PROJECT_PLATFORM_GLOBAL}

    PROJECT_FLAGS_AMD64=$(grep -oE 'DOCKER_FLAGS_AMD64=[^#\n]+' .cluster-ci 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs)
    PROJECT_FLAGS_GLOBAL=$(grep -oE 'DOCKER_FLAGS=[^#\n]+' .cluster-ci 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs)
    PROJECT_DOCKER_FLAGS=${PROJECT_FLAGS_AMD64:-$PROJECT_FLAGS_GLOBAL}

elif [ "$HOST_ARCH" = "aarch64" ] || [ "$HOST_ARCH" = "arm64" ]; then
    DOCKER_PLATFORM="linux/arm64"
    DOCKER_IMAGE=${DOCKER_IMAGE_ARM64:-${DOCKER_BASE_IMAGE:-"nvcr.io/nvidia/pytorch:26.05-py3"}}

    # Surcharges projets dans .cluster-ci
    PROJECT_IMAGE_ARM64=$(grep -oE 'DOCKER_IMAGE_ARM64=[^ ]+' .cluster-ci 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" | head -n 1)
    PROJECT_IMAGE_GLOBAL=$(grep -oE 'DOCKER_IMAGE=[^ ]+' .cluster-ci 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" | head -n 1)
    PROJECT_DOCKER_IMAGE=${PROJECT_IMAGE_ARM64:-$PROJECT_IMAGE_GLOBAL}

    PROJECT_PLATFORM_ARM64=$(grep -oE 'DOCKER_PLATFORM_ARM64=[^ ]+' .cluster-ci 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" | head -n 1)
    PROJECT_PLATFORM_GLOBAL=$(grep -oE 'DOCKER_PLATFORM=[^ ]+' .cluster-ci 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" | head -n 1)
    PROJECT_DOCKER_PLATFORM=${PROJECT_PLATFORM_ARM64:-$PROJECT_PLATFORM_GLOBAL}

    PROJECT_FLAGS_ARM64=$(grep -oE 'DOCKER_FLAGS_ARM64=[^#\n]+' .cluster-ci 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs)
    PROJECT_FLAGS_GLOBAL=$(grep -oE 'DOCKER_FLAGS=[^#\n]+' .cluster-ci 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs)
    PROJECT_DOCKER_FLAGS=${PROJECT_FLAGS_ARM64:-$PROJECT_FLAGS_GLOBAL}

else
    log_warn "Unknown architecture: $HOST_ARCH. Falling back to default image."
    DOCKER_PLATFORM=""
    DOCKER_IMAGE=${DOCKER_BASE_IMAGE:-"nvcr.io/nvidia/pytorch:26.05-py3"}
    PROJECT_DOCKER_IMAGE=$(grep -oE 'DOCKER_IMAGE=[^ ]+' .cluster-ci 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" | head -n 1)
    PROJECT_DOCKER_PLATFORM=$(grep -oE 'DOCKER_PLATFORM=[^ ]+' .cluster-ci 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" | head -n 1)
    PROJECT_DOCKER_FLAGS=$(grep -oE 'DOCKER_FLAGS=[^#\n]+' .cluster-ci 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs)
fi

if [ -n "$PROJECT_DOCKER_IMAGE" ]; then
    log_info "Project-level Docker image override: $PROJECT_DOCKER_IMAGE (was: $DOCKER_IMAGE)"
    DOCKER_IMAGE="$PROJECT_DOCKER_IMAGE"
fi

if [ -n "$PROJECT_DOCKER_PLATFORM" ]; then
    log_info "Project-level Docker platform override: $PROJECT_DOCKER_PLATFORM (was: $DOCKER_PLATFORM)"
    DOCKER_PLATFORM="$PROJECT_DOCKER_PLATFORM"
fi

PLATFORM_FLAG=""
if [ -n "$DOCKER_PLATFORM" ]; then
    PLATFORM_FLAG="--platform $DOCKER_PLATFORM"
fi
log_info "Host architecture: $HOST_ARCH → Docker platform: ${DOCKER_PLATFORM:-auto}"
ENV_FILE_FLAG=""
if [ -f "$BASE_DIR/.env.secrets" ]; then
    ENV_FILE_FLAG="--env-file $BASE_DIR/.env.secrets"
fi

if [ -n "$CLUSTER_CI_SECRETS_FILE" ] && [ -f "$CLUSTER_CI_SECRETS_FILE" ]; then
    log_info "Injecting secure job secrets from $CLUSTER_CI_SECRETS_FILE"
    ENV_FILE_FLAG="$ENV_FILE_FLAG --env-file $CLUSTER_CI_SECRETS_FILE"
fi

# Create a volume for the user's home to avoid redownloading dvc every time and to keep uv/pip caches
HOME_CACHE_VOLUME="cluster-ci-home-$(echo "$TARGET_REPO" | tr '/' '-')"
if ! docker volume inspect "$HOME_CACHE_VOLUME" >/dev/null 2>&1; then
    docker volume create "$HOME_CACHE_VOLUME" >/dev/null
fi

# Ensure a clean state
docker rm -f "${MAIN_CONTAINER_NAME}" 2>/dev/null || true

# Launch the persistent main container
DOCKER_PORT_MAPPING=""

log_info "Searching for a free port for web interface..."
# Use EXPOSED_PORT if defined in .cluster-ci, otherwise find a free port
EXPOSED_PORT=$(grep -oE -e 'EXPOSED_PORT=[0-9]+' .cluster-ci | cut -d= -f2 | head -n 1)
if [ -n "$EXPOSED_PORT" ]; then
    # Validate: reject reserved ports that would collide with cluster-ci services
    if [ "$EXPOSED_PORT" -lt 1024 ] || [ "$EXPOSED_PORT" -eq 5000 ] || [ "$EXPOSED_PORT" -eq 6000 ]; then
        log_error "EXPOSED_PORT=$EXPOSED_PORT is reserved (ports <1024, 5000=scheduler, 6000=worker agent)."
        log_error "Please choose a port >= 1024 and not 5000 or 6000 in your .cluster-ci file."
        exit 1
    fi
    VIEWER_PORT=$EXPOSED_PORT
    log_info "Using explicit EXPOSED_PORT from .cluster-ci: $VIEWER_PORT"
    DOCKER_PORT_MAPPING="-p 0.0.0.0:$VIEWER_PORT:$VIEWER_PORT"
    log_info "Main container will expose port $VIEWER_PORT (Web Application mode)"
else
    VIEWER_PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("", 0)); print(s.getsockname()[1]); s.close()')
    log_info "No EXPOSED_PORT found. Dynamic port selected for dvc-viewer: $VIEWER_PORT"
    DOCKER_PORT_MAPPING=""
fi
echo "$VIEWER_PORT" > .cluster-ci-viewer-port

docker run -d \
    --init \
    $PLATFORM_FLAG \
    --name "${MAIN_CONTAINER_NAME}" \
    $COMMON_LABELS \
    $DOCKER_PORT_MAPPING \
    $PROJECT_DOCKER_FLAGS \
    $DOCKER_MEMORY_FLAG \
    --entrypoint "tail" \
    --gpus all \
    -v "$(pwd):/workspace" \
    -v "$HOME_CACHE_VOLUME:/home/user" \
    -v /home/henri/ollama_poc:/home/user/.ollama \
    -v "$BASE_DIR:/cluster-ci:ro" \
    -v /etc/passwd:/etc/passwd:ro \
    -v /etc/group:/etc/group:ro \
    -w /workspace \
    --ulimit memlock=-1 \
    --ulimit stack=67108864 \
    --ipc=host \
    --user "$(id -u):$(id -g)" \
    -e HOME=/home/user \
    -e UV_CACHE_DIR=/home/user/.cache/uv \
    $ENV_FILE_FLAG \
    -e HEADNODE_URL="$HEADNODE_URL" \
    -e CLUSTER_CI_MODE=executor \
    -e CLUSTER_CI_GPU_REQUIRED="$CLUSTER_CI_GPU_REQUIRED" \
    -e CLUSTER_CI_VRAM_LIMIT_GB="$VRAM_LIMIT" \
    -e PYTHONSTARTUP=/cluster-ci/src/runner/gpu_memory_guard.py \
    "$DOCKER_IMAGE" -f /dev/null >/dev/null

# Ensure the volume and workspace are owned by the current user (must be run as root)
# Also create a .pth file in system site-packages so Python discovers pip --prefix packages.
# This MUST run as root because system site-packages is read-only for normal users.
# pip --prefix /home/user/.local installs to /home/user/.local/local/lib/python3.X/dist-packages/
# The .pth is ephemeral (lost on container rebuild) so we recreate it every time.
# We use a temp script to avoid all escaping issues with docker exec.
cat > /tmp/_cluster_ci_init.sh << 'INIT_SCRIPT'
#!/bin/bash
chown -R "$1" /home/user && chown -R "$1" /workspace
[ -d /opt/Automodel ] && chmod -R a+rX /opt/Automodel 2>/dev/null || true
[ -d /opt/venv ] && chmod -R a+rX /opt/venv 2>/dev/null || true
SITE=$(python3 -c "import site; print(site.getsitepackages()[0])" 2>/dev/null)
if [ -n "$SITE" ]; then
    find /home/user -path "*/lib/python3.*/site-packages" -o -path "*/lib/python3.*/dist-packages" 2>/dev/null > "$SITE/cluster-ci-prefix.pth"
fi
# Fix NVSHMEM symbol errors on single-GPU systems.
# libtorch_nvshmem.so expects nvshmem symbols to already be in the process
# (not via NEEDED deps). We compile a stub and register it in /etc/ld.so.preload
# so the dynamic linker preloads it for EVERY process (like system-wide LD_PRELOAD).
GPU_COUNT=$(nvidia-smi -L 2>/dev/null | wc -l)
TORCH_NVSHMEM=$(find /usr/local/lib -name "libtorch_nvshmem.so" 2>/dev/null | head -1)
if [ "${GPU_COUNT:-0}" -le 1 ] && [ -n "$TORCH_NVSHMEM" ]; then
    echo "🔧 [Cluster-CI] Creating NVSHMEM stub for single-GPU system..."
    cat > /tmp/_nvshmem_stub.c << 'STUBEOF'
void nvshmem_selected_device_transport() {}
void nvshmem_init() {}
void nvshmem_finalize() {}
void nvshmem_my_pe() {}
void nvshmem_n_pes() {}
void nvshmem_malloc() {}
void nvshmem_free() {}
void nvshmem_barrier_all() {}
STUBEOF
    cat > /tmp/_nvshmem_stub.ver << 'STUBEOF'
NVSHMEM { global: *; };
STUBEOF
    STUB_PATH="/usr/local/lib/libnvshmem_stub.so"
    if gcc -shared -o "$STUB_PATH" /tmp/_nvshmem_stub.c \
         -Wl,--version-script=/tmp/_nvshmem_stub.ver 2>/dev/null; then
        # Register in /etc/ld.so.preload for system-wide preloading
        echo "$STUB_PATH" >> /etc/ld.so.preload
        ldconfig 2>/dev/null || true
        echo "  ✓ NVSHMEM stub compiled and registered in /etc/ld.so.preload"
    else
        echo "  ⚠ gcc failed, stub not created"
    fi
fi
INIT_SCRIPT
docker cp /tmp/_cluster_ci_init.sh "${MAIN_CONTAINER_NAME}":/tmp/_cluster_ci_init.sh
docker exec --user root "${MAIN_CONTAINER_NAME}" bash /tmp/_cluster_ci_init.sh "$(id -u):$(id -g)"

# Detect Docker image change: if the cached image marker differs from the
# current image, purge stale tool binaries to force a clean reinstall.
MARKER_CMD="cat /home/user/.cluster-ci-image-marker 2>/dev/null || echo 'none'"
CACHED_IMAGE=$(docker exec "${MAIN_CONTAINER_NAME}" bash -c "$MARKER_CMD")
if [ "$CACHED_IMAGE" != "$DOCKER_IMAGE" ]; then
    log_info "Docker image changed ($CACHED_IMAGE → $DOCKER_IMAGE). Purging stale tool cache..."
    docker exec --user "$(id -u):$(id -g)" "${MAIN_CONTAINER_NAME}" \
        bash -c "rm -rf /home/user/.local /home/user/.cache/uv /home/user/.cluster-ci-deps-hash 2>/dev/null; echo '$DOCKER_IMAGE' > /home/user/.cluster-ci-image-marker"
fi

function docker_exec() {
docker exec \
        -e HEADNODE_URL="$HEADNODE_URL" \
        -e CLUSTER_CI_MODE=executor \
        -e CLUSTER_CI_GPU_REQUIRED="$CLUSTER_CI_GPU_REQUIRED" \
        "${MAIN_CONTAINER_NAME}" bash -c "export PATH=/home/user/shims:\$PATH:/home/user/.local/bin && $1"
}

log_info "Image used: $DOCKER_IMAGE"

log_info "GPU Hardware Validation..."
# We check CUDA but only fail if CLUSTER_CI_GPU_REQUIRED is set to 1.
# This prevents breaking CPU-only environments (local debug, etc.) while keeping
# enforcement on production workers if desired.
GPU_REQ_CMD="import torch, os;
avail=torch.cuda.is_available();
print(f'CUDA available: {avail}');
if avail:
    props=torch.cuda.get_device_properties(0);
    free,total=torch.cuda.mem_get_info(0);
    print(f'GPU Device: {props.name}');
    print(f'GPU Memory (CUDA reports): {total/(1024**3):.1f} GB total, {free/(1024**3):.1f} GB free');
    print(f'Compute Capability: {props.major}.{props.minor}');
required=os.environ.get('CLUSTER_CI_GPU_REQUIRED', '0') == '1';
if required and not avail:
    print('❌ Error: GPU required but not found!');
    exit(1)"
docker_exec "python3 -c \"$GPU_REQ_CMD\""

log_info "Preparing smart environment shims (uv/poetry)..."
docker exec --user "$(id -u):$(id -g)" "${MAIN_CONTAINER_NAME}" bash -c 'SHIM_DIR=/home/user/shims && mkdir -p $SHIM_DIR &&

# UV Shim
cat > $SHIM_DIR/uv << '"'"'SHIMEOF'"'"'
#!/bin/bash
if [ "$1" = "run" ]; then
    shift
    # Collect --with packages and strip uv-specific flags
    WITH_PKGS=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --with) WITH_PKGS="$WITH_PKGS $2"; shift 2 ;;
            --python) shift 2 ;;
            --no-project|--no-sync) shift ;;
            *) break ;;
        esac
    done
    if [ -n "$WITH_PKGS" ]; then
        pip install --quiet --break-system-packages $WITH_PKGS 2>/dev/null || true
    fi
    echo "🚀 [Cluster-CI Shim] Intercepting uv run, executing natively: $@"
    exec "$@"
elif [ "$1" = "sync" ]; then
    echo "ℹ️  [Cluster-CI Shim] Ignoring uv sync, dependencies are pre-installed in system."
    exit 0
else
    # Fallback to real uv — strip shim dir from PATH to avoid infinite recursion
    if [ -x "/home/user/.local/bin/uv" ]; then
        exec /home/user/.local/bin/uv "$@"
    else
        REAL_UV=$(PATH=${PATH#/home/user/shims:} command -v uv 2>/dev/null || true)
        if [ -n "$REAL_UV" ]; then
            exec "$REAL_UV" "$@"
        else
            echo "❌ [Cluster-CI Shim] uv not found. Install it first." >&2
            exit 1
        fi
    fi
fi
SHIMEOF
chmod +x $SHIM_DIR/uv &&

# Poetry Shim
cat > $SHIM_DIR/poetry << '"'"'SHIMEOF'"'"'
#!/bin/bash
if [ "$1" = "run" ]; then
    shift
    echo "🚀 [Cluster-CI Shim] Intercepting poetry run, executing natively: $@"
    exec "$@"
elif [ "$1" = "install" ] || [ "$1" = "sync" ]; then
    echo "ℹ️  [Cluster-CI Shim] Ignoring poetry install, dependencies are pre-installed."
    exit 0
else
    if [ -x "/home/user/.local/bin/poetry" ]; then
        exec /home/user/.local/bin/poetry "$@"
    else
        REAL_POETRY=$(PATH=${PATH#/home/user/shims:} command -v poetry 2>/dev/null || true)
        if [ -n "$REAL_POETRY" ]; then
            exec "$REAL_POETRY" "$@"
        else
            echo "❌ [Cluster-CI Shim] poetry not found. Install it first." >&2
            exit 1
        fi
    fi
fi
SHIMEOF
chmod +x $SHIM_DIR/poetry'

log_info "Installing base dependencies in persistent volume..."
# Bootstrap commands MUST bypass shims — use a raw docker run without /home/user/shims in PATH.
# Shims are only for user pipeline execution, not for installing the tools themselves.
function docker_exec_bootstrap() {
    docker exec \
        "${MAIN_CONTAINER_NAME}" bash -c "export PATH=\$PATH:/home/user/.local/bin && $1"
}
docker_exec_bootstrap "uv --version || curl -LsSf https://astral.sh/uv/install.sh | sh || python3 -m pip install uv --user --break-system-packages"
docker_exec_bootstrap "dvc version && uv tool upgrade dvc || uv tool install dvc --with dvc-http"
docker_exec_bootstrap "uv tool upgrade dvc-viewer || uv tool install git+https://github.com/UNIL-DESI/dvc-viewer.git || true"

log_info "Reading DVC parameters from .cluster-ci..."

# Check if STAGES is defined in .cluster-ci
if grep -q "^STAGES=" .cluster-ci; then
    STAGES_RAW_VAL=$(grep "^STAGES=" .cluster-ci | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs)
else
    STAGES_RAW_VAL=""
fi

# Clean comments and remove internal flags like --ram
RAW_ARGS=$(grep -v '^\s*#' .cluster-ci | sed 's/--ram [0-9.]*//g')

# Extract arguments without '='
DVC_REGULAR_ARGS=$(echo "$RAW_ARGS" | grep -v '=' | tr '\n' ' ' | xargs)

# Extract STAGES=... value
STAGES_ARGS=$(echo "$RAW_ARGS" | grep -oE '^STAGES=.*' | cut -d= -f2- | tr -d '"' | tr -d "'" | tr ',' ' ' | xargs 2>/dev/null || echo "")

if [ "$STAGES_ARGS" = "all" ] || [ -z "$STAGES_ARGS" ]; then
    log_info "Executing the full pipeline by default (STAGES=all or empty/missing)."
    DVC_ARGS=$(echo "$DVC_REGULAR_ARGS" | xargs)
else
    log_info "Specific stages execution requested."
    DVC_ARGS=$(echo "$DVC_REGULAR_ARGS $STAGES_ARGS" | xargs)
    log_info "Arguments detected: $DVC_ARGS"
fi

log_info "Preventive cleanup of DVC lock file..."
docker_exec "rm -f .dvc/tmp/lock"

if [ -n "$DVC_REMOTE_P2P_URL" ]; then
    log_info "Data Plane: Configuring dynamic P2P remote to $DVC_REMOTE_P2P_URL..."
    PEER_REMOTE_URL="$DVC_REMOTE_P2P_URL/$TARGET_REPO/.dvc/cache/files/md5"

    docker_exec "dvc remote add -f peer_remote '$PEER_REMOTE_URL' --local"

    log_info "Fetching data from peer (best-effort P2P pull)..."
    rm -f /tmp/p2p_pull.log
    if docker_exec "dvc pull --force --allow-missing -r peer_remote" 2>/tmp/p2p_pull.log; then
        log_success "P2P transfer successful."
    else
        log_warn "P2P pull incomplete or failed."
        # Only attempt fallback if a default remote is actually configured
        if docker_exec "dvc remote list 2>/dev/null | head -1 | grep -q ."; then
            log_info "Attempting fallback pull from default remote..."
            if docker_exec "dvc pull --force --allow-missing" >>/tmp/p2p_pull.log 2>&1; then
                log_success "Fallback pull from default remote successful."
            else
                log_warn "Fallback pull also failed or incomplete. Details of the error:"
                if [ -s /tmp/p2p_pull.log ]; then
                    log_warn "--- DVC Pull Error Diagnostic Log ---"
                    tail -n 20 /tmp/p2p_pull.log | while read -r line; do
                        log_warn "  $line"
                    done
                    log_warn "-------------------------------------"
                else
                    log_warn "No detailed error log available."
                fi
            fi
        else
            log_info "No default DVC remote configured. Skipping fallback pull."
        fi
        log_info "dvc repro will regenerate missing stages (best-effort)."
    fi
fi

log_info "AST analysis via dvc-viewer..."
docker_exec "dvc-viewer hash"

if [ -n "$EXPOSED_PORT" ]; then
    log_info "Skipping secondary dvc-viewer container (Main container handles web application on port $VIEWER_PORT)."
else
    log_info "Launching live dvc-viewer server on port $VIEWER_PORT..."
    # Pour le viewer en background, on expose le port
    # IMPORTANT: On utilise --pid=container:${MAIN_CONTAINER_NAME} pour voir les processus du job principal
    docker rm -f "$VIEWER_CONTAINER_NAME" 2>/dev/null || true
    docker run --rm \
        $PLATFORM_FLAG \
        --name "$VIEWER_CONTAINER_NAME" \
        $COMMON_LABELS \
        --pid=container:${MAIN_CONTAINER_NAME} \
        --entrypoint "" \
        -v "$(pwd):/workspace" -w /workspace \
        -v "$HOME_CACHE_VOLUME:/home/user" \
        -p "0.0.0.0:$VIEWER_PORT:$VIEWER_PORT" \
        --ulimit memlock=-1 \
        --ulimit stack=67108864 \
        --ipc=host \
        --user "$(id -u):$(id -g)" -e HOME=/home/user \
        -e CLUSTER_CI_MODE=executor \
        $ENV_FILE_FLAG \
        $DOCKER_IMAGE \
        bash -c "export PATH=/home/user/shims:\$PATH:/home/user/.local/bin && dvc-viewer --port $VIEWER_PORT" > "dvc-viewer.log" 2>&1 &
fi

log_info "Pre-flight Validation..."
# Run the validation script using uv to ensure dependencies (tomlkit) are present
docker_exec "uv run --with tomlkit python3 /cluster-ci/src/runner/validate_pyproject.py --ci"

log_info "DVC-Git-Helper: Injecting cache: false for metrics and plots..."
docker_exec "uv run --with ruamel.yaml python3 /cluster-ci/src/runner/dvc_git_helper.py inject"

log_info "DVC: Restoring all cached outputs to workspace..."
if ! docker_exec "dvc checkout --force 2>/dev/null"; then
    log_info "💡 Note: Certains gros fichiers ou modèles ne sont pas encore présents dans le cache local/P2P."
    log_info "   Ceci est normal (mode Best-Effort). Ils seront régénérés automatiquement par le pipeline."
    log_warn "DVC checkout failed or incomplete. Proceeding..."
fi

echo "===STAGE:setup:END==="
echo "===STAGE:dvc_repro:BEGIN==="

log_info "Starting DVC Watchdog (background)..."
bash "$BASE_DIR/src/runner/dvc_watchdog.sh" "${MAIN_CONTAINER_NAME}" > dvc_watchdog.log 2>&1 &
WATCHDOG_PID=$!

# GPU VRAM Watchdog: monitors nvidia-smi and kills container before driver crash
GPU_WATCHDOG_PID=""
if [ "$VRAM_LIMIT" != "0" ]; then
    log_info "Starting GPU VRAM Watchdog (limit: ${VRAM_LIMIT}GB)..."
    bash "$BASE_DIR/src/runner/gpu_watchdog.sh" "${MAIN_CONTAINER_NAME}" "$VRAM_LIMIT" 2>&1 | tee -a gpu_watchdog.log &
    GPU_WATCHDOG_PID=$!
fi

log_info "Launching: dvc repro $DVC_ARGS via Docker"
# Smart dependency installation: only re-install if pyproject.toml/uv.lock changed.
# The smart_install.sh script hashes dependency files and caches the result in the
# persistent Docker volume. Skips entirely if nothing changed → saves ~3GB bandwidth.
if [ -f "pyproject.toml" ]; then
    EXEC_CMD="bash /cluster-ci/src/runner/smart_install.sh && python3 -u /cluster-ci/src/runner/dvc_iterative_repro.py $DVC_ARGS"
else
    EXEC_CMD="python3 -u /cluster-ci/src/runner/dvc_iterative_repro.py $DVC_ARGS"
fi

log_info "🚀 Live Terminal Streaming enabled. Piping logs to server..."
set +e
docker_exec "${EXEC_CMD}" 2>&1 | stdbuf -oL -eL tee tmate_execution.log
EXEC_RET=${PIPESTATUS[0]}
set -e

echo "===STAGE:dvc_repro:END==="
echo "===STAGE:sync:BEGIN==="

if [ -n "$WATCHDOG_PID" ]; then
    log_info "Stopping DVC Watchdog..."
    _kill_with_timeout "$WATCHDOG_PID" 5
fi
if [ -n "$GPU_WATCHDOG_PID" ]; then
    log_info "Stopping GPU Watchdog..."
    _kill_with_timeout "$GPU_WATCHDOG_PID" 5
    # Kill the gpu_watchdog.sh process in the pipeline (tee was $GPU_WATCHDOG_PID)
    timeout 5 pkill -9 -f "gpu_watchdog.sh" 2>/dev/null || true
fi

if [ -n "$EXEC_RET" ] && [ "$EXEC_RET" -ne 0 ]; then
    OOM_KILLED=$(timeout 10 docker inspect "${MAIN_CONTAINER_NAME}" --format '{{.State.OOMKilled}}' 2>/dev/null || echo "false")
    GPU_WATCHDOG_KILLED=false
    if [ -f gpu_watchdog.log ] && grep -q "VRAM limit exceeded" gpu_watchdog.log 2>/dev/null; then
        GPU_WATCHDOG_KILLED=true
    fi

    if [ "$GPU_WATCHDOG_KILLED" = "true" ]; then
        EXEC_RET=137
        log_error "❌ Erreur: Le job a dépassé la limite REQUIRED_VRAM allouée (${VRAM_LIMIT} GB) et a été arrêté préventivement par le GPU Watchdog pour protéger le worker. Veuillez réduire la consommation VRAM ou augmenter REQUIRED_VRAM dans .cluster-ci"
    elif [ $EXEC_RET -eq 137 ] || [ "$OOM_KILLED" = "true" ]; then
        EXEC_RET=137
        log_error "❌ Erreur: Le job a dépassé la limite REQUIRED_RAM allouée (${RAM_LIMIT} GB) et a été tué par le système (OOM Killer). Veuillez augmenter cette limite dans le fichier .cluster-ci"
    elif [ $EXEC_RET -eq 255 ]; then
        log_error "❌ Critical Failure: Execution process aborted unexpectedly (Exit code: 255)."
    else
        log_error "Execution interrupted or failed (Exit code: $EXEC_RET). Forcing DVC sync before exiting..."
    fi
fi

log_info "DVC-Git-Helper: Syncing metrics and plots to Git..."
docker_exec "timeout -k 10 120 uv run --with ruamel.yaml python3 /cluster-ci/src/runner/dvc_git_helper.py sync" || log_warn "DVC-Git-Helper sync timed out or failed."

# Note: Synchronous dvc push has been removed to avoid saturating network bandwidth.
# Lazy GC (in gc_orchestrator.py) now handles asynchronous backups when worker disk space falls below 100 GB.

echo "=========================================================================="
if [ -z "$EXEC_RET" ] || [ "$EXEC_RET" -eq 0 ]; then
    log_success "CLUSTER-CI: GitOps execution completed successfully."
else
    log_error "CLUSTER-CI: GitOps execution failed with exit code $EXEC_RET."
fi
echo "=========================================================================="

echo "===STAGE:sync:END==="

# Truncate log to max 2000 lines (erases beginning to keep the end)
if [ -f "$LOG_FILE" ]; then
    tail -n 2000 "$LOG_FILE" > "${LOG_FILE}.tmp"
    mv "${LOG_FILE}.tmp" "$LOG_FILE"
fi

if [ -n "$GITHUB_STEP_SUMMARY" ]; then
    log_info "Generating GitHub Step Summary (Markdown Report)..."
    echo "# 🧪 Cluster-CI Run Report" >> "$GITHUB_STEP_SUMMARY"
    echo "## 📊 DVC Metrics" >> "$GITHUB_STEP_SUMMARY"
    timeout 30 docker_exec "dvc metrics diff --md" >> "$GITHUB_STEP_SUMMARY" 2>/dev/null || echo "No metric changes or error." >> "$GITHUB_STEP_SUMMARY"
    
    echo "## 📈 DVC Plots" >> "$GITHUB_STEP_SUMMARY"
    timeout 30 docker_exec "dvc plots diff --md" >> "$GITHUB_STEP_SUMMARY" 2>/dev/null || echo "No plot changes or error." >> "$GITHUB_STEP_SUMMARY"
fi

if [ -n "$EXEC_RET" ] && [ "$EXEC_RET" -ne 0 ]; then
    if [ $EXEC_RET -eq 137 ]; then
        # Already logged above (RAM or VRAM OOM), just exit
        true
    elif [ $EXEC_RET -eq 255 ]; then
        log_error "❌ Critical Failure: Execution process aborted unexpectedly (Exit code: 255)."
    else
        log_error "Exiting with error code $EXEC_RET due to previous failure."
    fi
    sync 2>/dev/null || true
    sleep 1
    exit $EXEC_RET
fi

sync 2>/dev/null || true
sleep 1
