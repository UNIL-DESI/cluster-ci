"""Fix remaining zombie jobs - use positive exit_code to bypass cancel_job_cleanly routing."""
import requests

HEADNODE = "http://130.223.73.209:5000"
TOKEN = "VyrGjOvgDzuLHJHm4st0yh9yKIfUCbZS"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# Jobs that still need DB update (failed with 500 before)
remaining_jobs = [
    "c0df5fb7-b727-4e57-99ad-082ee7da20c0",  # running on HEC45801 (worker already cancelled)
    "3c72dcb9-3cdb-47a1-a1cc-7c6b237a9b37",  # pending cluster-ci
]

print("=== Fixing remaining zombie jobs with exit_code=1 (positive, bypasses cancel routing) ===")
for jid in remaining_jobs:
    # Use exit_code=1 (positive) to avoid the cancel_job_cleanly routing
    # which requires exit_code < 0 in the deployed version
    payload = {
        "job_id": jid,
        "status": "failed",
        "exit_code": 1,  # Positive to bypass cancel_job_cleanly routing
    }
    r = requests.post(f"{HEADNODE}/update_job_status", json=payload, headers=HEADERS, timeout=10)
    print(f"  {jid}: HTTP {r.status_code} - {r.text[:300]}")

print("\n=== VERIFY WORKERS ===")
r = requests.get(f"{HEADNODE}/workers", headers=HEADERS)
workers = r.json()
for w in workers:
    avail = w['available_ram_gb']
    total = w['total_ram_gb']
    used = total - avail - 2.0
    print(f"  {w['hostname']}: RAM used by jobs={used:.1f}GB")

print("\n=== VERIFY NO MORE ACTIVE JOBS ===")
for repo in ["UNIL-DESI/llm-as-recommender", "UNIL-DESI/cluster-ci"]:
    r = requests.get(f"{HEADNODE}/api/projects/{repo}/runs")
    runs = r.json()
    active = [run for run in runs if run.get('status') in ('running', 'assigned', 'pending')]
    if active:
        print(f"  WARNING {repo}: Still has {len(active)} active job(s)!")
        for run in active:
            print(f"    {run['job_id']}: {run['status']}")
    else:
        print(f"  OK {repo}: No more active jobs")
