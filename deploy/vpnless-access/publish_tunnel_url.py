#!/usr/bin/env python3
"""
Publishes the dynamic Cloudflare Quick Tunnel URL (https://*.trycloudflare.com)
to the GitHub repository (UNIL-DESI/cluster-ci) as a repository variable, release, or issue.
Runs in Mode 2 (Zero External Account).
"""

import sys
import os
import re
import time
import subprocess
import requests

GITHUB_REPO = os.environ.get("GITHUB_REPO", "UNIL-DESI/cluster-ci")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

def extract_tunnel_url():
    """Extracts trycloudflare.com URL from systemd journal of cluster-ci-cloudflared."""
    try:
        proc = subprocess.run(
            ["journalctl", "-u", "cluster-ci-cloudflared", "-n", "100", "--no-pager"],
            capture_output=True, text=True, timeout=10
        )
        matches = re.findall(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", proc.stdout)
        if matches:
            return matches[-1]
    except Exception as e:
        print(f"Error reading journalctl: {e}", file=sys.stderr)
    return None

def publish_to_github(tunnel_url):
    """Updates repository variable CLUSTER_PUBLIC_URL via GitHub REST API."""
    if not GITHUB_TOKEN:
        print(f"⚠️ GITHUB_TOKEN not set. Active Tunnel URL: {tunnel_url}")
        return

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    var_name = "CLUSTER_PUBLIC_URL"
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/variables/{var_name}"

    # Try updating existing variable
    resp = requests.patch(api_url, json={"name": var_name, "value": tunnel_url}, headers=headers, timeout=10)
    if resp.status_code == 204:
        print(f"✅ Updated GitHub variable {var_name} on {GITHUB_REPO}: {tunnel_url}")
        return

    # If not found, try creating it
    create_url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/variables"
    resp_create = requests.post(create_url, json={"name": var_name, "value": tunnel_url}, headers=headers, timeout=10)
    if resp_create.status_code in (201, 204):
        print(f"✅ Created GitHub variable {var_name} on {GITHUB_REPO}: {tunnel_url}")
    else:
        print(f"⚠️ Could not set GitHub variable: HTTP {resp_create.status_code} - {resp_create.text}")

def main():
    print("🔍 Looking for Cloudflare Quick Tunnel URL...")
    for _ in range(12):
        url = extract_tunnel_url()
        if url:
            print(f"🎉 Detected Cloudflare Tunnel URL: {url}")
            publish_to_github(url)
            return
        time.sleep(5)
    print("❌ Could not detect tunnel URL within 60s.")
    sys.exit(1)

if __name__ == "__main__":
    main()
