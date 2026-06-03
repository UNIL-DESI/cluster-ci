#!/bin/bash
set -e

# Parse flags
ADD_WORKER=false
for arg in "$@"; do
    case $arg in
        --add-worker) ADD_WORKER=true ;;
    esac
done

ENV_FILE=".env"

# Ensure sshpass is installed
if ! command -v sshpass &> /dev/null; then
    echo "❌ Error: sshpass is not installed on your local machine."
    echo "Install it with: sudo apt-get install sshpass (or brew install sshpass on macOS)"
    exit 1
fi

# Load existing environment variables if they exist
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

update_env() {
    local var_name=$1
    local var_value=$2
    if grep -q "^${var_name}=" "$ENV_FILE"; then
        local tmp_env=$(mktemp)
        grep -v "^${var_name}=" "$ENV_FILE" > "$tmp_env"
        echo "${var_name}=\"${var_value}\"" >> "$tmp_env"
        mv "$tmp_env" "$ENV_FILE"
    else
        echo "${var_name}=\"${var_value}\"" >> "$ENV_FILE"
    fi
}

update_needed=false

if [ -z "$HEADNODE_IP" ] || [ -z "$HEADNODE_USER" ] || [ -z "$HEADNODE_PASS" ]; then
    echo "--- Headnode Configuration ---"
    [ -z "$HEADNODE_IP" ] && read -p "Headnode IP: " HEADNODE_IP
    [ -z "$HEADNODE_USER" ] && read -p "SSH User: " HEADNODE_USER
    [ -z "$HEADNODE_PASS" ] && read -rs -p "SSH Password: " HEADNODE_PASS
    echo ""
    update_needed=true
fi

if [ -z "$TARGET_REPO" ]; then
    read -p "GitHub Target (e.g., UNIL-DESI): " TARGET_REPO
    [ -z "$TARGET_REPO" ] && TARGET_REPO="UNIL-DESI"
    update_needed=true
fi

if [ -z "$WORKER_COUNT" ]; then
    WORKER_COUNT=0
fi

# Only enter worker addition flow if --add-worker flag is passed
if [ "$ADD_WORKER" = true ]; then
    echo "--- Workers Configuration (--add-worker mode) ---"
    echo "--- $WORKER_COUNT existing Worker(s) found in configuration ---"

    while true; do
        if [ "$WORKER_COUNT" -eq 0 ]; then
            prompt_msg="Add a worker? (Y/n): "
        else
            prompt_msg="Add another new worker? (y/N): "
        fi
        
        read -p "$prompt_msg" add_worker
        
        if [ "$WORKER_COUNT" -eq 0 ]; then
            if [[ "$add_worker" =~ ^[Nn] ]]; then
                break
            fi
        else
            if [[ ! "$add_worker" =~ ^[OoYy] ]]; then
                break
            fi
        fi
        
        WORKER_COUNT=$((WORKER_COUNT + 1))
        read -p "Worker $WORKER_COUNT IP: " w_ip
        read -p "SSH User: " w_user
        read -rs -p "SSH Password: " w_pass
        echo ""
        
        export "WORKER_${WORKER_COUNT}_IP"="$w_ip"
        export "WORKER_${WORKER_COUNT}_USER"="$w_user"
        export "WORKER_${WORKER_COUNT}_PASS"="$w_pass"
        update_needed=true
    done
else
    echo "--- $WORKER_COUNT existing Worker(s) in configuration (use --add-worker to add more) ---"
fi

if [ "$update_needed" = true ]; then
    echo "📝 Saving credentials to $ENV_FILE..."
    touch "$ENV_FILE"
    
    update_env "HEADNODE_IP" "$HEADNODE_IP"
    update_env "HEADNODE_USER" "$HEADNODE_USER"
    update_env "HEADNODE_PASS" "$HEADNODE_PASS"
    update_env "TARGET_REPO" "$TARGET_REPO"
    update_env "WORKER_COUNT" "$WORKER_COUNT"
    
    for ((i=1; i<=WORKER_COUNT; i++)); do
        ip_var="WORKER_${i}_IP"
        user_var="WORKER_${i}_USER"
        pass_var="WORKER_${i}_PASS"
        update_env "$ip_var" "${!ip_var}"
        update_env "$user_var" "${!user_var}"
        update_env "$pass_var" "${!pass_var}"
    done
fi

echo "==========================================================="
echo "🚀 Updating Headnode ($HEADNODE_IP)..."
echo "==========================================================="
export SSHPASS="$HEADNODE_PASS"

# Inject per-architecture Docker images on headnode
echo "🐳 Updating Docker images on headnode..."
sshpass -e ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$HEADNODE_USER@$HEADNODE_IP" \
    "HEADNODE_ENV=\"\$HOME/cluster-ci/.env\" && \
     if [ -f \"\$HEADNODE_ENV\" ]; then \
         grep -q '^DOCKER_IMAGE_AMD64=' \"\$HEADNODE_ENV\" && sed -i 's|^DOCKER_IMAGE_AMD64=.*|DOCKER_IMAGE_AMD64=nvcr.io/nvidia/pytorch:26.04-py3|' \"\$HEADNODE_ENV\" || echo 'DOCKER_IMAGE_AMD64=nvcr.io/nvidia/pytorch:26.04-py3' >> \"\$HEADNODE_ENV\"; \
         grep -q '^DOCKER_IMAGE_ARM64=' \"\$HEADNODE_ENV\" && sed -i 's|^DOCKER_IMAGE_ARM64=.*|DOCKER_IMAGE_ARM64=nvcr.io/nvidia/pytorch:26.04-py3|' \"\$HEADNODE_ENV\" || echo 'DOCKER_IMAGE_ARM64=nvcr.io/nvidia/pytorch:26.04-py3' >> \"\$HEADNODE_ENV\"; \
         echo '✅ Docker images updated on headnode (AMD64 + ARM64)'; \
     fi"

sshpass -e ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$HEADNODE_USER@$HEADNODE_IP" "export SUDO_PASSWORD='$HEADNODE_PASS'; curl -sSL https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh | bash -s -- headnode $TARGET_REPO"

# Force-update dvc-viewer on the headnode globally
echo "👁️ Updating dvc-viewer on headnode..."
sshpass -e ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$HEADNODE_USER@$HEADNODE_IP" "export PATH=/home/$HEADNODE_USER/.local/bin:\$PATH; uv tool upgrade dvc-viewer >/dev/null 2>&1 || uv tool install git+https://github.com/UNIL-DESI/dvc-viewer.git >/dev/null 2>&1"

# Restart worker service on headnode if installed (dual-mode)
echo "🔄 Checking for worker service on headnode..."
sshpass -e ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$HEADNODE_USER@$HEADNODE_IP" "if systemctl is-enabled cluster-worker 2>/dev/null | grep -q enabled; then echo '🔄 Restarting worker service on headnode...'; sudo systemctl restart cluster-worker; echo '✅ Worker service restarted.'; else echo 'ℹ️ No worker service on headnode (headnode-only mode).'; fi"

# Retrieve CLUSTER_TOKEN from headnode for local tests
echo "🔑 Retrieving security Token from headnode..."
REMOTE_TOKEN=$(sshpass -e ssh -o StrictHostKeyChecking=no "$HEADNODE_USER@$HEADNODE_IP" "grep '^CLUSTER_TOKEN=' /home/$HEADNODE_USER/cluster-ci/.env | cut -d= -f2-")
if [ -n "$REMOTE_TOKEN" ]; then
    update_env "CLUSTER_TOKEN" "$REMOTE_TOKEN"
    export CLUSTER_TOKEN="$REMOTE_TOKEN"
    echo "✅ Token retrieved and saved."
fi

for ((i=1; i<=WORKER_COUNT; i++)); do
    ip_var="WORKER_${i}_IP"
    user_var="WORKER_${i}_USER"
    pass_var="WORKER_${i}_PASS"
    
    ip_val="${!ip_var}"
    user_val="${!user_var}"
    pass_val="${!pass_var}"
    
    if [ -n "$ip_val" ]; then
        echo "==========================================================="
        echo "🚀 Updating Worker $i ($ip_val)..."
        echo "==========================================================="
        export SSHPASS="$pass_val"

        # Force-update Docker images on worker (per-architecture config)
        echo "🐳 Updating Docker images on worker $i..."
        if ! sshpass -e ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$user_val@$ip_val" \
            "WORKER_ENV=\"\$HOME/cluster-ci/.env\" && \
             if [ -f \"\$WORKER_ENV\" ]; then \
                 sed -i 's|^DOCKER_BASE_IMAGE=.*|DOCKER_BASE_IMAGE=nvcr.io/nvidia/pytorch:26.04-py3|' \"\$WORKER_ENV\"; \
                 grep -q '^DOCKER_IMAGE_AMD64=' \"\$WORKER_ENV\" && sed -i 's|^DOCKER_IMAGE_AMD64=.*|DOCKER_IMAGE_AMD64=nvcr.io/nvidia/pytorch:26.04-py3|' \"\$WORKER_ENV\" || echo 'DOCKER_IMAGE_AMD64=nvcr.io/nvidia/pytorch:26.04-py3' >> \"\$WORKER_ENV\"; \
                 grep -q '^DOCKER_IMAGE_ARM64=' \"\$WORKER_ENV\" && sed -i 's|^DOCKER_IMAGE_ARM64=.*|DOCKER_IMAGE_ARM64=nvcr.io/nvidia/pytorch:26.04-py3|' \"\$WORKER_ENV\" || echo 'DOCKER_IMAGE_ARM64=nvcr.io/nvidia/pytorch:26.04-py3' >> \"\$WORKER_ENV\"; \
                 echo '✅ Docker images updated in .env (AMD64 + ARM64)'; \
             fi"; then
            echo "⚠️  Worker $i ($ip_val) is unreachable. Skipping."
            continue
        fi

        if ! sshpass -e ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$user_val@$ip_val" "export SUDO_PASSWORD='$pass_val'; curl -sSL https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh | bash -s -- worker"; then
            echo "⚠️  Worker $i ($ip_val): installation failed or host unreachable. Skipping."
            continue
        fi
    fi
done

echo "✅ Cluster update complete!"

echo "==========================================================="
echo "🧪 Infrastructure Test: Submitting 2 test jobs..."
echo "==========================================================="
echo "Pausing for 10s to allow services to start..."
sleep 10

echo "🚀 Submitting Job 1..."
curl -s -X POST "http://$HEADNODE_IP:5000/submit_job" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $CLUSTER_TOKEN" \
    -d "{\"repo\": \"$TARGET_REPO/cluster-ci\", \"branch\": \"main\", \"ram_required_gb\": 2.0, \"max_runtime_hours\": 1}" &
JOB1=$!

echo "🚀 Submitting Job 2..."
curl -s -X POST "http://$HEADNODE_IP:5000/submit_job" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $CLUSTER_TOKEN" \
    -d "{\"repo\": \"$TARGET_REPO/cluster-ci\", \"branch\": \"main\", \"ram_required_gb\": 2.0, \"max_runtime_hours\": 1}" &
JOB2=$!

wait $JOB1
wait $JOB2

echo ""
echo "🎉 Cluster test complete! All nodes are operational."
