"""Force-stop all zombie jobs on the cluster."""
import requests
import json

HEADNODE = "http://130.223.73.209:5000"
TOKEN = "VyrGjOvgDzuLHJHm4st0yh9yKIfUCbZS"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# Jobs to stop
zombie_jobs = [
    # Running zombies
    "c0df5fb7-b727-4e57-99ad-082ee7da20c0",  # running since 10:26
    "debf857a-67ca-406c-abff-cf3c7c41d218",  # running since 09:14
    # Pending jobs blocked by RAM
    "fe647de7-98c9-4613-bfe0-48d1ed6b580d",  # pending
    "8ad333ee-8804-43f1-8ea8-29a765e2958f",  # pending
    "3c72dcb9-3cdb-47a1-a1cc-7c6b237a9b37",  # pending cluster-ci
]

print("=== GETTING JOB DETAILS FIRST ===")
for jid in zombie_jobs[:2]:  # running jobs
    r = requests.get(f"{HEADNODE}/job_status/{jid}", headers=HEADERS, timeout=10)
    if r.status_code == 200:
        job = r.json()
        print(f"\nJOB {jid}:")
        print(f"  repo={job.get('repo')}, branch={job.get('branch')}")
        print(f"  status={job.get('status')}, worker_id={job.get('worker_id')}")
        print(f"  worker_service_url={job.get('worker_service_url')}")
        print(f"  gh_run_id={job.get('gh_run_id')}")
        print(f"  ram_required_gb={job.get('ram_required_gb')}")
    else:
        print(f"JOB {jid}: HTTP {r.status_code}")

print("\n=== ATTEMPTING TO STOP RUNNING JOBS VIA API ===")
for jid in zombie_jobs[:2]:
    print(f"\nStopping {jid}...")
    r = requests.post(f"{HEADNODE}/api/jobs/{jid}/stop", headers=HEADERS, timeout=15)
    print(f"  Response: HTTP {r.status_code}")
    print(f"  Body: {r.text[:500]}")

print("\n=== ATTEMPTING TO STOP PENDING JOBS VIA API ===")
for jid in zombie_jobs[2:]:
    print(f"\nStopping {jid}...")
    r = requests.post(f"{HEADNODE}/api/jobs/{jid}/stop", headers=HEADERS, timeout=15)
    print(f"  Response: HTTP {r.status_code}")
    print(f"  Body: {r.text[:500]}")
