"""Force-stop zombie jobs by contacting workers directly and updating DB."""
import requests
import json

HEADNODE = "http://130.223.73.209:5000"
TOKEN = "VyrGjOvgDzuLHJHm4st0yh9yKIfUCbZS"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# Job -> Worker mapping (from diagnostic)
zombie_running_jobs = [
    {
        "job_id": "c0df5fb7-b727-4e57-99ad-082ee7da20c0",
        "worker_url": "http://130.223.170.123:6000",
        "worker_name": "HEC45801",
        "gh_run_id": "26631947930",
        "repo": "UNIL-DESI/llm-as-recommender",
    },
    {
        "job_id": "debf857a-67ca-406c-abff-cf3c7c41d218",
        "worker_url": "http://130.223.169.200:6000",
        "worker_name": "HEC45803",
        "gh_run_id": "26628868778",
        "repo": "UNIL-DESI/llm-as-recommender",
    },
]

pending_jobs = [
    "fe647de7-98c9-4613-bfe0-48d1ed6b580d",
    "8ad333ee-8804-43f1-8ea8-29a765e2958f",
    "3c72dcb9-3cdb-47a1-a1cc-7c6b237a9b37",
]

print("=== STEP 1: CANCEL RUNNING JOBS ON WORKERS DIRECTLY ===")
for job in zombie_running_jobs:
    jid = job["job_id"]
    worker_url = job["worker_url"]
    print(f"\nCancelling {jid} on {job['worker_name']} ({worker_url})...")
    try:
        r = requests.post(f"{worker_url}/cancel/{jid}", timeout=15)
        print(f"  Worker cancel response: HTTP {r.status_code} - {r.text[:500]}")
    except Exception as e:
        print(f"  Worker cancel failed: {e}")

print("\n=== STEP 2: UPDATE DB STATUS VIA update_job_status ===")
for job in zombie_running_jobs:
    jid = job["job_id"]
    print(f"\nUpdating {jid} to 'failed' in DB...")
    payload = {
        "job_id": jid,
        "status": "failed",
        "exit_code": -1,
    }
    try:
        r = requests.post(f"{HEADNODE}/update_job_status", json=payload, headers=HEADERS, timeout=10)
        print(f"  DB update response: HTTP {r.status_code} - {r.text[:500]}")
    except Exception as e:
        print(f"  DB update failed: {e}")

print("\n=== STEP 3: UPDATE PENDING JOBS TO FAILED ===")
for jid in pending_jobs:
    print(f"\nUpdating pending {jid} to 'failed' in DB...")
    payload = {
        "job_id": jid,
        "status": "failed",
        "exit_code": -1,
    }
    try:
        r = requests.post(f"{HEADNODE}/update_job_status", json=payload, headers=HEADERS, timeout=10)
        print(f"  DB update response: HTTP {r.status_code} - {r.text[:500]}")
    except Exception as e:
        print(f"  DB update failed: {e}")

import os

print("\n=== STEP 4: CANCEL GITHUB ACTIONS RUNS ===")
GH_PAT = os.environ.get("GH_PAT", "")
for job in zombie_running_jobs:
    run_id = job["gh_run_id"]
    repo = job["repo"]
    print(f"\nCancelling GHA run {run_id} for {repo}...")
    try:
        gh_headers = {
            "Authorization": f"token {GH_PAT}",
            "Accept": "application/vnd.github.v3+json",
        }
        r = requests.post(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/cancel", headers=gh_headers, timeout=10)
        print(f"  GHA cancel response: HTTP {r.status_code}")
    except Exception as e:
        print(f"  GHA cancel failed: {e}")

print("\n=== STEP 5: VERIFY ===")
r = requests.get(f"{HEADNODE}/workers", headers=HEADERS)
workers = r.json()
for w in workers:
    avail = w['available_ram_gb']
    total = w['total_ram_gb']
    used = total - avail - 2.0
    print(f"  {w['hostname']}: RAM used by jobs={used:.1f}GB (was 115.0GB)")

# Check for any remaining active jobs
for repo in ["UNIL-DESI/llm-as-recommender", "UNIL-DESI/cluster-ci"]:
    r = requests.get(f"{HEADNODE}/api/projects/{repo}/runs")
    runs = r.json()
    active = [run for run in runs if run.get('status') in ('running', 'assigned', 'pending')]
    if active:
        print(f"  ⚠️ {repo}: Still has {len(active)} active job(s)!")
        for run in active:
            print(f"    {run['job_id']}: {run['status']}")
    else:
        print(f"  ✅ {repo}: No more active jobs")
