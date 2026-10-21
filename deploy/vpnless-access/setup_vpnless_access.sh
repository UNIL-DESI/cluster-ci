#!/usr/bin/env bash
# ==============================================================================
# Setup VPN-Less Headnode Access via Cloudflare Tunnel (0 CHF, 100% UNIL-DESI)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="quick"
TOKEN=""

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Options:
  --quick-zero-account         Configure Cloudflare Quick Tunnel (no account required, 0 CHF)
  --token <CF_TUNNEL_TOKEN>    Configure Cloudflare Named Tunnel with account token
  -h, --help                   Show this help message
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --quick-zero-account)
            MODE="quick"
            shift
            ;;
        --token)
            MODE="named"
            TOKEN="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown argument: $1"
            usage
            ;;
    esac
done

echo "=== 1. Installing cloudflared ==="
ARCH="$(uname -m)"
case "$ARCH" in
    x86_64) CF_ARCH="amd64" ;;
    aarch64|arm64) CF_ARCH="arm64" ;;
    *) echo "Unsupported architecture: $ARCH"; exit 1 ;;
esac

if ! command -v cloudflared &>/dev/null; then
    echo "Downloading cloudflared (${CF_ARCH})..."
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" -o /usr/local/bin/cloudflared
    chmod +x /usr/local/bin/cloudflared
    echo "cloudflared installed successfully: $(cloudflared --version)"
else
    echo "cloudflared already installed: $(cloudflared --version)"
fi

echo "=== 2. Configuring /etc/default/cluster-ci-tunnel ==="
mkdir -p /etc/default
if [[ "$MODE" == "named" ]]; then
    if [[ -z "$TOKEN" ]]; then
        echo "Error: --token is required in named mode."
        exit 1
    fi
    cat <<EOF > /etc/default/cluster-ci-tunnel
# Cloudflare Named Tunnel Token
CLOUDFLARE_TUNNEL_TOKEN="${TOKEN}"
EOF
    echo "Configured Named Tunnel mode."
else
    cat <<EOF > /etc/default/cluster-ci-tunnel
# Cloudflare Quick Tunnel (Zero-Account Mode)
CLOUDFLARE_TUNNEL_TOKEN=""
EOF
    echo "Configured Zero-Account Quick Tunnel mode."
fi

echo "=== 3. Installing and starting systemd service ==="
cp "${SCRIPT_DIR}/cluster-ci-cloudflared.service" /etc/systemd/system/cluster-ci-cloudflared.service
systemctl daemon-reload
systemctl enable --now cluster-ci-cloudflared.service

echo "=== 4. Service Status & Connectivity Check ==="
sleep 3
systemctl status cluster-ci-cloudflared.service --no-pager

if [[ "$MODE" == "quick" ]]; then
    echo "Extracting Quick Tunnel URL..."
    python3 "${SCRIPT_DIR}/publish_tunnel_url.py" || true
fi

echo "✅ VPN-Less Ingress Setup Complete!"
