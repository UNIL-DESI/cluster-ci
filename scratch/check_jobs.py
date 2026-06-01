#!/usr/bin/env python3
"""Remote script to check active jobs on the headnode."""
import sys
sys.path.insert(0, '/home/henri/cluster-ci')
from src.scheduler.persistence import get_db_conn

conn = get_db_conn()
cursor = conn.cursor()

# Active jobs
cursor.execute('''
    SELECT job_id, repo, branch, status, worker_id, created_at, started_at, finished_at, exit_code
    FROM jobs 
    WHERE status IN ('running', 'assigned', 'pending') 
    ORDER BY created_at DESC
''')
rows = cursor.fetchall()
print("=== ACTIVE JOBS ===")
for row in rows:
    d = dict(row)
    print(f"  JOB: {d['job_id']}")
    print(f"    repo={d['repo']} branch={d['branch']} status={d['status']}")
    print(f"    worker={d['worker_id']} created={d['created_at']} started={d['started_at']}")
    print()

# Recent jobs (last 10)
cursor.execute('''
    SELECT job_id, repo, branch, status, worker_id, created_at, started_at, finished_at, exit_code
    FROM jobs 
    ORDER BY created_at DESC
    LIMIT 10
''')
rows = cursor.fetchall()
print("=== RECENT 10 JOBS ===")
for row in rows:
    d = dict(row)
    print(f"  JOB: {d['job_id'][:12]}... status={d['status']} repo={d['repo']} branch={d['branch']}")
    print(f"    worker={d['worker_id']} exit_code={d['exit_code']} created={d['created_at']}")
    print()

# Workers
cursor.execute('SELECT worker_id, hostname, service_url, status, last_seen FROM workers')
rows = cursor.fetchall()
print("=== WORKERS ===")
for row in rows:
    d = dict(row)
    print(f"  WORKER: {d['worker_id'][:12]}... host={d['hostname']} status={d['status']}")
    print(f"    url={d['service_url']} last_seen={d['last_seen']}")
    print()

conn.close()
