"""Deep diagnostic: query all jobs with running/assigned status and find the zombie Docker containers."""
import requests
import json

HEADNODE = "http://130.223.73.209:5000"
TOKEN = "VyrGjOvgDzuLHJHm4st0yh9yKIfUCbZS"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# The available_ram_gb is calculated as:
# total_ram_gb - 2.0 - SUM(ram_required_gb WHERE status IN ('running', 'assigned'))
# If total_ram=121.6 and available=4.6, then SUM(ram_required)=115.0
# This means there ARE jobs with status running/assigned even though worker_poll says no_job

# Let's check all repos by querying workers directly
print("=== WORKER DETAILS ===")
workers = requests.get(f"{HEADNODE}/workers", headers=HEADERS).json()
for w in workers:
    wid = w['worker_id']
    url = w['service_url']
    avail = w['available_ram_gb']
    total = w['total_ram_gb']
    used = total - avail - 2.0
    print(f"\n{w['hostname']} (wid={wid}): used_ram_by_jobs={used:.1f}GB")
    
    # Check all known endpoints on worker
    for endpoint in ['/health', '/current_job', '/status', '/info']:
        try:
            r = requests.get(f"{url}{endpoint}", timeout=3)
            if r.status_code == 200:
                print(f"  {endpoint}: {json.dumps(r.json(), indent=2)[:500]}")
            else:
                print(f"  {endpoint}: HTTP {r.status_code}")
        except Exception as e:
            print(f"  {endpoint}: {e}")

# Let's try to find ALL repos that have jobs in the system
# Use a known pattern - try to query common repos
print("\n=== SEARCHING ALL REPOS FOR ACTIVE JOBS ===")
# We can also check job_status for specific workers by looking at what
# jobs are consuming the RAM
# The DB query for available_ram counts jobs WHERE worker_id=X AND status IN ('running', 'assigned')

# Let's try a broader search - check all known org repos
import subprocess
try:
    result = subprocess.run(
        ['gh', 'api', 'orgs/UNIL-DESI/repos', '--paginate', '-q', '.[].full_name'],
        capture_output=True, text=True, timeout=15
    )
    repos = result.stdout.strip().split('\n') if result.stdout.strip() else []
    print(f"Found {len(repos)} repos in UNIL-DESI org")
    for repo in repos:
        if not repo:
            continue
        r = requests.get(f"{HEADNODE}/api/projects/{repo}/runs", timeout=5)
        runs = r.json()
        active = [run for run in runs if run.get('status') in ('running', 'assigned', 'pending')]
        if active:
            print(f"\n  >>> {repo}: {len(active)} active job(s)!")
            for run in active:
                print(f"      JOB: {run['job_id']}")
                print(f"        status={run['status']} branch={run['branch']}")
                print(f"        created={run['created_at']} started={run['started_at']}")
except Exception as e:
    print(f"Error listing repos: {e}")

# Also try direct job_status for specific IDs if we know them
# For now, let's check the job that's blocking RAM on each worker
print("\n=== TRYING TO FIND ZOMBIE JOBS VIA WORKER ENDPOINTS ===")
for w in workers:
    url = w['service_url']
    # Try common worker API patterns
    for ep in ['/api/worker/current', '/job/current', '/jobs/active', '/jobs']:
        try:
            r = requests.get(f"{url}{ep}", timeout=3)
            if r.status_code == 200:
                print(f"  {w['hostname']} {ep}: {r.text[:500]}")
        except:
            pass
