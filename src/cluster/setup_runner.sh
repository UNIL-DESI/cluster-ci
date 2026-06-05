#!/bin/bash
set -e

ROLE=${2:-headnode}
TARGET=$1

if [ "$ROLE" == "headnode" ] && [ -z "$TARGET" ]; then
    echo "Usage: $0 <target_repo_or_org> headnode"
    exit 1
fi

# Go to project root
BASE_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/../.." >/dev/null 2>&1 && pwd )"
cd "$BASE_DIR"

if [ -n "$SUDO_PASSWORD" ]; then
    ASKPASS_SCRIPT=$(mktemp)
    echo '#!/bin/bash' > "$ASKPASS_SCRIPT"
    echo 'echo "$SUDO_PASSWORD"' >> "$ASKPASS_SCRIPT"
    chmod +x "$ASKPASS_SCRIPT"
    export SUDO_ASKPASS="$ASKPASS_SCRIPT"
    
    sudo() {
        command sudo -A "$@"
    }
    
    # Cleanup trap
    trap 'rm -f "$ASKPASS_SCRIPT"' EXIT
fi

echo "🎯 Preparing the Cluster for target: $TARGET"

# 2. Environment loading (Needed for DOCKER_BASE_IMAGE)
if [ -f ".env" ]; then
    source .env
fi

# 0. Docker Check / Installation
if ! command -v docker &> /dev/null; then
    echo "📦 Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    echo "⚠️ Docker has been installed. You may need to reconnect for group changes to take effect."
else
    echo "✅ Docker is already installed."
fi

# Always ensure user is in docker group
sudo usermod -aG docker $USER
# Force docker socket permissions in case group membership requires a relogin
if [ -e /var/run/docker.sock ]; then
    sudo chmod 666 /var/run/docker.sock || true
fi

# Pre-pull the base image with explicit platform to avoid arch mismatch
HOST_ARCH=$(uname -m)
if [ "$HOST_ARCH" = "x86_64" ] || [ "$HOST_ARCH" = "amd64" ]; then
    PULL_PLATFORM="linux/amd64"
    PULL_IMAGE=${DOCKER_IMAGE_AMD64:-${DOCKER_BASE_IMAGE:-""}}
elif [ "$HOST_ARCH" = "aarch64" ] || [ "$HOST_ARCH" = "arm64" ]; then
    PULL_PLATFORM="linux/arm64"
    PULL_IMAGE=${DOCKER_IMAGE_ARM64:-${DOCKER_BASE_IMAGE:-""}}
else
    PULL_PLATFORM=""
    PULL_IMAGE=${DOCKER_BASE_IMAGE:-""}
fi
if [ -n "$PULL_IMAGE" ]; then
    echo "🐳 Pre-loading Docker image: $PULL_IMAGE (platform: ${PULL_PLATFORM:-auto})..."
    PLATFORM_ARG=""
    [ -n "$PULL_PLATFORM" ] && PLATFORM_ARG="--platform $PULL_PLATFORM"
    sudo docker pull $PLATFORM_ARG "$PULL_IMAGE" || echo "⚠️ Failed to pull image $PULL_IMAGE, it will be downloaded during the first job."
fi

# 1. uv Check / Installation
if ! command -v uv &> /dev/null; then
    echo "📦 Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    source $HOME/.local/bin/env || true
else
    echo "✅ uv is already installed."
fi

# 1.5 DVC Check / Installation
if ! command -v dvc &> /dev/null || ! dvc remote list --help &> /dev/null; then
    echo "📦 Installing DVC (globally via uv)..."
    uv tool install 'dvc' --force
else
    echo "✅ dvc is installed."
fi

# 2.5 Prerequisites check by role
if [ "$ROLE" == "headnode" ]; then
    if [ -z "$GITHUB_PAT" ]; then
        echo "❌ Error: GITHUB_PAT not defined. Required for headnode role."
        exit 1
    fi
fi

# 3. Prepare the runner folder
# Download the runner once into a template folder
TEMPLATE_DIR="runners/template"
mkdir -p "$TEMPLATE_DIR"

if [ ! -f "$TEMPLATE_DIR/config.sh" ]; then
    echo "⬇️ Downloading GitHub Actions Runner binary..."
    RUNNER_VERSION="2.321.0"
    curl -o actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz -L https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz
    tar xzf ./actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz -C "$TEMPLATE_DIR"
    rm actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz
fi

# Prepare slots for ephemeral runners (2 standard + 1 admin)
for i in 1 2; do
    SLOT_DIR="runners/slot$i"
    if [ ! -d "$SLOT_DIR" ]; then
        echo "📂 Initializing slot $i..."
        cp -r "$TEMPLATE_DIR" "$SLOT_DIR"
    fi
done

# Provision exclusive Admin Runner slot
ADMIN_SLOT_DIR="runners/admin"
if [ ! -d "$ADMIN_SLOT_DIR" ]; then
    echo "📂 Initializing Admin slot..."
    cp -r "$TEMPLATE_DIR" "$ADMIN_SLOT_DIR"
fi

# 4.5. Sudoers Configuration for Auto-Update
echo "🔐 Configuring sudoers for cluster-ci CI privileges..."
cat <<EOF | sudo tee /etc/sudoers.d/cluster-ci > /dev/null
Defaults:$USER !requiretty
$USER ALL=(ALL) NOPASSWD: /bin/systemctl restart cluster-runner-manager, /bin/systemctl restart cluster-scheduler, /bin/systemctl restart cluster-scheduler-loop, /bin/systemctl restart cluster-worker, /usr/bin/systemctl restart cluster-runner-manager, /usr/bin/systemctl restart cluster-scheduler, /usr/bin/systemctl restart cluster-scheduler-loop, /usr/bin/systemctl restart cluster-worker, /usr/bin/dmesg, /bin/dmesg, /usr/bin/journalctl, /bin/journalctl
EOF
sudo chmod 0440 /etc/sudoers.d/cluster-ci
echo "✅ Sudoers configured."

# 4.6. Hardware Watchdog — Auto-reboot on system freeze
# On unified memory GPU systems (NVIDIA GB10/Grace), a CUDA driver crash can freeze
# the entire system. The hardware watchdog timer forces a reboot if systemd can't
# "pet" the timer within the configured interval. This is the industry-standard
# solution for unattended server recovery.
if ! grep -q "^RuntimeWatchdogSec=" /etc/systemd/system.conf 2>/dev/null; then
    echo "🐕 Configuring hardware watchdog (auto-reboot on system freeze)..."
    sudo sed -i.bak \
        -e 's/^#\?RuntimeWatchdogSec=.*/RuntimeWatchdogSec=30/' \
        -e 's/^#\?RebootWatchdogSec=.*/RebootWatchdogSec=60/' \
        /etc/systemd/system.conf
    # If the keys were not found (not even commented), append them
    grep -q "^RuntimeWatchdogSec=" /etc/systemd/system.conf || echo "RuntimeWatchdogSec=30" | sudo tee -a /etc/systemd/system.conf > /dev/null
    grep -q "^RebootWatchdogSec=" /etc/systemd/system.conf || echo "RebootWatchdogSec=60" | sudo tee -a /etc/systemd/system.conf > /dev/null
    sudo rm -f /etc/systemd/system.conf.bak
    echo "✅ Hardware watchdog configured (30s freeze → auto-reboot)."
else
    echo "✅ Hardware watchdog already configured."
fi

# 4.7. Ensure SSH starts on boot (no user login required for remote access after reboot)
if systemctl is-enabled ssh 2>/dev/null | grep -q "enabled" || systemctl is-enabled sshd 2>/dev/null | grep -q "enabled"; then
    echo "✅ SSH daemon already enabled on boot."
else
    echo "🔑 Enabling SSH daemon on boot..."
    sudo systemctl enable ssh 2>/dev/null || sudo systemctl enable sshd 2>/dev/null || true
    echo "✅ SSH daemon enabled on boot."
fi

# 5. Systemd Installation
if [ "$ROLE" == "headnode" ]; then
    echo "⚙️ Installing systemd service for Ephemeral Runner Manager..."

    # Create systemd service for runner manager
    cat <<EOF | sudo tee /etc/systemd/system/cluster-runner-manager.service
[Unit]
Description=Cluster-CI Ephemeral Runner Manager
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$BASE_DIR
EnvironmentFile=$BASE_DIR/.env
ExecStart=$(uv python find) $BASE_DIR/src/scheduler/runner_manager.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    echo "⚙️ Configuring Headnode Scheduler..."
    # Install dependencies for the scheduler from pyproject.toml
    uv pip install -e $BASE_DIR

    # Create systemd service for scheduler API
    cat <<EOF | sudo tee /etc/systemd/system/cluster-scheduler.service
[Unit]
Description=Cluster-CI Scheduler API
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$BASE_DIR
EnvironmentFile=$BASE_DIR/.env
ExecStart=$(uv python find) $BASE_DIR/src/scheduler/headnode_service.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    # Create systemd service for scheduler loop
    cat <<EOF | sudo tee /etc/systemd/system/cluster-scheduler-loop.service
[Unit]
Description=Cluster-CI Scheduler Loop
After=cluster-scheduler.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$BASE_DIR
EnvironmentFile=$BASE_DIR/.env
ExecStart=$(uv python find) $BASE_DIR/src/scheduler/scheduler_loop.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable cluster-scheduler cluster-scheduler-loop cluster-runner-manager
    sudo systemctl restart cluster-scheduler cluster-scheduler-loop cluster-runner-manager
    echo "🚀 Scheduler and Runner Manager services started."

    # Also install the Worker Agent on the headnode so it can execute jobs too
    echo "⚙️ Also installing Worker Agent on headnode (dual role)..."
    cat <<EOF | sudo tee /etc/systemd/system/cluster-worker.service
[Unit]
Description=Cluster-CI Worker Agent
After=network.target cluster-scheduler.service
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=$USER
WorkingDirectory=$BASE_DIR
EnvironmentFile=$BASE_DIR/.env
ExecStart=$(uv python find) $BASE_DIR/src/scheduler/worker_agent.py
ExecStopPost=/bin/bash -c 'docker rm -f \$(docker ps -q --filter name=cluster-job- 2>/dev/null) 2>/dev/null; docker rm -f \$(docker ps -q --filter name=cluster-viewer- 2>/dev/null) 2>/dev/null; exit 0'
Restart=always
RestartSec=10
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable cluster-worker
    sudo systemctl restart cluster-worker
    echo "🚀 Worker Agent also started on headnode (dual mode)."

else
    echo "⚙️ Configuring Worker Agent..."
    uv pip install -e $BASE_DIR

    cat <<EOF | sudo tee /etc/systemd/system/cluster-worker.service
[Unit]
Description=Cluster-CI Worker Agent
After=network.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=$USER
WorkingDirectory=$BASE_DIR
EnvironmentFile=$BASE_DIR/.env
ExecStart=$(uv python find) $BASE_DIR/src/scheduler/worker_agent.py
ExecStopPost=/bin/bash -c 'docker rm -f \$(docker ps -q --filter name=cluster-job- 2>/dev/null) 2>/dev/null; docker rm -f \$(docker ps -q --filter name=cluster-viewer- 2>/dev/null) 2>/dev/null; exit 0'
Restart=always
RestartSec=10
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

    # Pre-restart cleanup: kill ALL running cluster containers to avoid orphans
    echo "🧹 Pre-restart cleanup: Stopping all running cluster containers..."
    running_jobs=$(docker ps -q --filter name=cluster-job- 2>/dev/null)
    running_viewers=$(docker ps -q --filter name=cluster-viewer- 2>/dev/null)
    if [ -n "$running_jobs" ] || [ -n "$running_viewers" ]; then
        echo "⚠️  Active containers detected. Force-killing before restart..."
        [ -n "$running_jobs" ] && docker rm -f $running_jobs 2>/dev/null || true
        [ -n "$running_viewers" ] && docker rm -f $running_viewers 2>/dev/null || true
        echo "✅ All cluster containers destroyed."
    else
        echo "✅ No active cluster containers."
    fi

    sudo systemctl daemon-reload
    sudo systemctl enable cluster-worker
    sudo systemctl restart cluster-worker
    echo "🚀 Worker Agent service installed and started."
fi
echo "   Useful commands:"
echo "   - sudo ./svc.sh status  : View status"
echo "   - sudo ./svc.sh stop    : Stop"
echo "   - sudo ./svc.sh start   : Start"

# 6. Global link for orchestrator
echo "🔗 Creating global symbolic link /usr/local/bin/cluster-ci-run..."
sudo ln -sf "$BASE_DIR/src/runner/run_research_pipeline.sh" /usr/local/bin/cluster-ci-run
