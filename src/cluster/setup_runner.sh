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

# Pre-pull the base image to avoid timeouts
if [ -n "$DOCKER_BASE_IMAGE" ]; then
    echo "🐳 Pre-loading Docker image: $DOCKER_BASE_IMAGE..."
    # Use sudo here in case the user was just added to the group but hasn't restarted their session
    sudo docker pull "$DOCKER_BASE_IMAGE" || echo "⚠️ Failed to pull image $DOCKER_BASE_IMAGE, it will be downloaded during the first job."
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
KillMode=process

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
KillMode=process

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
KillMode=process

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable cluster-scheduler cluster-scheduler-loop cluster-runner-manager
    sudo systemctl restart cluster-scheduler cluster-scheduler-loop cluster-runner-manager
    echo "🚀 Scheduler and Runner Manager services started."

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
KillMode=process
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
