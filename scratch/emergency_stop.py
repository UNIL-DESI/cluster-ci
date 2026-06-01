"""Stop new zombie jobs and verify cluster state."""
import requests
import json

HEADNODE = "http://130.223.73.209:5000"
TOKEN = "VyrGjOvgDzuLHJHm4st0yh9yKIfUCbZS"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

print("=== FINDING ACTIVE JOBS ===")
for repo in ["UNIL-DESI/llm-as-recommender", "UNIL-DESI/cluster-ci"]:
    r = requests.get(f"{HEADNODE}/api/projects/{repo}/runs")
    runs = r.json()
    active = [run for run in runs if run.get('status') in ('running', 'assigned', 'pending')]
    if active:
        print(f"  {repo}: {len(active)} active job(s)")
        for run in active:
            jid = run['job_id']
            print(f"    Stopping {jid} (status={run['status']})...")
            
            # 1. Try to cancel on worker
            try:
                worker_r = requests.get(f"{HEADNODE}/job_status/{jid}", headers=HEADERS, timeout=5)
                if worker_r.status_code == 200:
                    job_info = worker_r.json()
                    worker_url = job_info.get('worker_service_url')
                    if worker_url:
                        cancel_r = requests.post(f"{worker_url}/cancel/{jid}", timeout=10)
                        print(f"      Worker cancel: HTTP {cancel_r.status_code}")
            except Exception as e:
                print(f"      Worker cancel failed: {e}")
            
            # 2. Update DB with positive exit_code
            payload = {"job_id": jid, "status": "failed", "exit_code": 1}
            r2 = requests.post(f"{HEADNODE}/update_job_status", json=payload, headers=HEADERS, timeout=10)
            print(f"      DB update: HTTP {r2.status_code}")

print("\n=== VERIFY WORKERS ===")
r = requests.get(f"{HEADNODE}/workers", headers=HEADERS)
workers = r.json()
for w in workers:
    avail = w['available_ram_gb']
    total = w['total_ram_gb']
    used = total - avail - 2.0
    print(f"  {w['hostname']}: RAM used by jobs={used:.0f}GB")
