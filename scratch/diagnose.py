"""Diagnostic script to check active jobs on the headnode via API."""
import requests
import json

HEADNODE = "http://130.223.73.209:5000"
TOKEN = "VyrGjOvgDzuLHJHm4st0yh9yKIfUCbZS"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# 1. Workers
print("=== WORKERS ===")
r = requests.get(f"{HEADNODE}/workers", headers=HEADERS)
workers = r.json()
for w in workers:
    avail = w['available_ram_gb']
    total = w['total_ram_gb']
    used = total - avail - 2.0  # OS margin
    print(f"  {w['hostname']} ({w['worker_id'][:12]}...): RAM used by jobs={used:.1f}GB, status={w['status']}")

# 2. Check all running/assigned jobs across all repos
# Query the DB directly through an available endpoint
# The /api/runs/active endpoint requires a session, so let's use a workaround
# Try listing projects
print("\n=== CHECKING REPOS WITH KNOWN JOBS ===")
# Check known repos
for repo in ["UNIL-DESI/LLM-as-a-Recommender", "UNIL-DESI/cluster-ci", "UNIL-DESI/dvc-viewer"]:
    r = requests.get(f"{HEADNODE}/api/projects/{repo}/runs")
    runs = r.json()
    active = [r for r in runs if r.get('status') in ('running', 'assigned', 'pending')]
    if active:
        print(f"\n  {repo}: {len(active)} active job(s)")
        for run in active:
            print(f"    JOB: {run['job_id']}")
            print(f"      status={run['status']} branch={run['branch']}")
            print(f"      created={run['created_at']} started={run['started_at']}")
            print(f"      commit={run.get('commit_hash', 'N/A')}")
    else:
        all_recent = runs[:3] if runs else []
        print(f"\n  {repo}: No active jobs. Recent {len(all_recent)} jobs:")
        for run in all_recent:
            print(f"    {run['job_id'][:12]}... status={run['status']} branch={run['branch']} exit={run.get('exit_code')}")

# 3. Check workers for running Docker containers
print("\n=== CHECKING WORKERS FOR DOCKER CONTAINERS ===")
for w in workers:
    url = w['service_url']
    try:
        # Try to get current job info from worker
        r = requests.get(f"{url}/status", timeout=5)
        if r.status_code == 200:
            print(f"  {w['hostname']} /status: {json.dumps(r.json(), indent=2)[:500]}")
        else:
            print(f"  {w['hostname']} /status: HTTP {r.status_code}")
    except Exception as e:
        print(f"  {w['hostname']} /status: Error - {e}")

# 4. Try to find any job with running status
print("\n=== CHECKING INDIVIDUAL WORKERS FOR ACTIVE JOBS ===")
for w in workers:
    url = w['service_url']
    wid = w['worker_id']
    try:
        r = requests.get(f"{HEADNODE}/worker_poll/{wid}", headers=HEADERS, timeout=5)
        data = r.json()
        print(f"  {w['hostname']} worker_poll: {json.dumps(data)[:300]}")
    except Exception as e:
        print(f"  {w['hostname']} worker_poll: Error - {e}")
