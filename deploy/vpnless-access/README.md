# VPN-Less Headnode Ingress & Zero-Trust Governance

This directory contains the production ingress deployment kit enabling **zero-cost (0 CHF), VPN-less access** to the Cluster-CI headnode scheduler and dashboard.

## 🏛️ Governance & Handover Principles (100% UNIL-DESI, 0 CHF)

1. **Zero External VPS & Zero Personal Accounts**: No private hosting fees, no personal VPS (Hetzner VPS decommissioned). The solution relies solely on Cloudflare Tunnel + GitHub organisation gates.
2. **Organization-Anchored Authentication**: All access control is strictly enforced at the application layer by the GitHub organization **`UNIL-DESI`**:
   - OAuth 2.0 App registered under the `UNIL-DESI` organization.
   - Fallback `POST /auth/token` accepts any GitHub Personal Access Token (`gh auth token` / `ghp_...`) and verifies active membership in `UNIL-DESI`.
   - Even when exposed via a public URL, non-members are blocked with `403 Forbidden`.
3. **Seamless Handover**: When members rotate or leave UNIL, the cluster remains functional under the laboratory's GitHub organization without transferring personal credentials or server subscriptions.

---

## 🚀 Deployment Modes

### Mode 1: Cloudflare Named Tunnel (Fixed Domain)
Uses a persistent tunnel token from a Cloudflare account (free tier, 0 CHF):
```bash
sudo ./setup_vpnless_access.sh --token "<CLOUDFLARE_TUNNEL_TOKEN>"
```
Routes directly to your chosen subdomain (e.g. `cluster.unil-desi.org` or similar) mapping to `http://127.0.0.1:5000`.

### Mode 2: Cloudflare Quick Tunnel (Zero-Account & Automatic GitHub Sync)
Operates without creating any Cloudflare account:
```bash
sudo ./setup_vpnless_access.sh --quick-zero-account
```
1. Starts a temporary secure tunnel on `https://*.trycloudflare.com`.
2. The companion script `publish_tunnel_url.py` reads the active URL from systemd logs and updates the GitHub repository variable `CLUSTER_PUBLIC_URL` on `UNIL-DESI/cluster-ci`.
3. Researchers and CI workflows retrieve the live headnode URL via GitHub Actions without hardcoded IPs.

---

## 🔒 Verification & Health Check

Once the tunnel is active, test the Zero-Trust gate:
```bash
# 1. Anonymous healthcheck must return 200
curl -i https://<TUNNEL_URL>/healthz

# 2. Anonymous API call must return 401
curl -i https://<TUNNEL_URL>/scheduler_status

# 3. Authenticated CLI call with UNIL-DESI token must return 200
curl -i -H "Authorization: Bearer $(gh auth token)" https://<TUNNEL_URL>/scheduler_status
```
