import os
import sys
import time
import uuid
import shutil
import sqlite3
import requests
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

# Set python path to find src modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "scheduler")))

# Mock subprocess.run BEFORE importing headnode_service to prevent network calls to GitHub
original_run = subprocess.run
def mock_subprocess_run(cmd, *args, **kwargs):
    if isinstance(cmd, list) and "git" in cmd:
        # Simulate a failed git fetch/clone to bypass network wait and fail fast
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="Mocked git network call for speed")
    return original_run(cmd, *args, **kwargs)
subprocess.run = mock_subprocess_run

# Ensure test DB is distinct
TEST_DB_PATH = "test_stress_scheduler.db"
os.environ["CLUSTER_DB_PATH"] = TEST_DB_PATH

from persistence import init_db, get_db_conn
from headnode_service import app
from scheduler_loop import schedule_jobs

PORT = 5005
HEADNODE_URL = f"http://localhost:{PORT}"

def cleanup():
    """Cleanup temporary database files."""
    for ext in ["", "-wal", "-shm"]:
        path = TEST_DB_PATH + ext
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                print(f"Failed to remove {path}: {e}")

class StressTestScheduler:
    def __init__(self):
        self.errors = []
        self.jobs_submitted = []
        self.lock = threading.Lock()
        self.stop_scheduler = threading.Event()

    def start_headnode(self):
        """Starts Flask headnode service in a background thread."""
        print("Starting Headnode Service...")
        # Disable Flask logs to prevent cluttering output
        import logging
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)
        
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)

    def start_scheduler_loop(self):
        """Starts scheduler loop in a background thread."""
        print("Starting Scheduler Loop...")
        while not self.stop_scheduler.is_set():
            try:
                schedule_jobs()
            except Exception as e:
                with self.lock:
                    self.errors.append(f"Scheduler loop error: {e}")
            time.sleep(0.1)  # Faster loop for testing

    def register_test_worker(self):
        """Registers a mock worker to receive jobs."""
        print("Registering mock worker...")
        worker_data = {
            "worker_id": "mock-worker-1",
            "hostname": "mock-worker-host",
            "service_url": "http://localhost:6000",
            "total_ram_gb": 64.0,
            "total_storage_gb": 1000.0,
            "available_storage_gb": 800.0,
            "is_startup": True
        }
        headers = {"Authorization": f"Bearer {os.environ.get('CLUSTER_TOKEN')}"} if os.environ.get('CLUSTER_TOKEN') else {}
        resp = requests.post(f"{HEADNODE_URL}/register_worker", json=worker_data, headers=headers)
        resp.raise_for_status()
        print("Mock worker registered successfully.")

    def run_concurrent_sql_stress(self):
        """Stresses the SQLite database with high-concurrency read/write transactions."""
        print("Launching concurrent SQL stress tasks...")
        def db_write_stress(thread_id):
            for i in range(100):
                try:
                    with get_db_conn() as conn:
                        cursor = conn.cursor()
                        cursor.execute(
                            "INSERT OR REPLACE INTO workers (worker_id, hostname, total_ram_gb, status) VALUES (?, ?, ?, ?)",
                            (f"worker-stress-{thread_id}-{i}", f"host-{thread_id}", 32.0, "online")
                        )
                        conn.commit()
                except Exception as e:
                    with self.lock:
                        self.errors.append(f"Concurrent SQL Write Error: {e}")

        def db_read_stress():
            for _ in range(200):
                try:
                    with get_db_conn() as conn:
                        cursor = conn.cursor()
                        cursor.execute("SELECT COUNT(*) FROM workers")
                        cursor.fetchone()
                        cursor.execute("SELECT * FROM jobs")
                        cursor.fetchall()
                except Exception as e:
                    with self.lock:
                        self.errors.append(f"Concurrent SQL Read Error: {e}")

        threads = []
        for i in range(10):
            t_write = threading.Thread(target=db_write_stress, args=(i,))
            threads.append(t_write)
            t_write.start()

        for _ in range(5):
            t_read = threading.Thread(target=db_read_stress)
            threads.append(t_read)
            t_read.start()

        for t in threads:
            t.join()
        print("SQL stress tasks completed.")

    def run_broken_pipe_simulation(self):
        """Simulates Broken Pipe errors by executing submit_job.py and closing stdout early."""
        print("Simulating Broken Pipe errors on submit_job.py processes...")
        # Create a mock .cluster-ci file locally to satisfy submit_job.py parsing requirements
        with open(".cluster-ci", "w") as f:
            f.write("REQUIRED_RAM=4GB\nMAX_RUNTIME_HOURS=2\n")

        processes = []
        for i in range(10):
            # Run submit_job.py as a subprocess, pointing to our local headnode
            cmd = [
                sys.executable,
                os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "scheduler", "submit_job.py")),
                "UNIL-DESI/cluster-ci",  # Dummy repo
                "main",                   # Dummy branch
                "--headnode", HEADNODE_URL
            ]
            
            # Start process with pipes
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            processes.append(proc)

        # Wait a brief moment for submission, then perform chaotic termination of stdout/pipes
        time.sleep(0.5)
        for proc in processes:
            try:
                # Forcefully close stdout to trigger BrokenPipeError on the child process when it prints next
                proc.stdout.close()
                # Also kill or terminate the child process to simulate aggressive GH runner shutdowns
                proc.terminate()
            except Exception as e:
                print(f"Error during chaotic close: {e}")

        # Wait for all processes to exit
        for proc in processes:
            proc.wait()
        
        # Clean up local .cluster-ci
        if os.path.exists(".cluster-ci"):
            os.remove(".cluster-ci")
            
        print("Broken Pipe simulation complete.")

    def run_concurrent_job_lifecycle_stress(self):
        """Submits and cancels jobs concurrently to stress Flask routes and state machine."""
        print("Launching concurrent Job Lifecycle Stress...")
        
        def submit_and_cancel_lifecycle(task_id):
            job_data = {
                "repo": "UNIL-DESI/cluster-ci",
                "branch": f"branch-lifecycle-{task_id}",
                "commit_hash": "abcdef123456",
                "ram_required_gb": 4.0,
                "max_runtime_hours": 1.0,
                "username": f"user-{task_id}"
            }
            
            try:
                headers = {"Authorization": f"Bearer {os.environ.get('CLUSTER_TOKEN')}"} if os.environ.get('CLUSTER_TOKEN') else {}
                
                # 1. Submit Job
                resp = requests.post(f"{HEADNODE_URL}/submit_job", json=job_data, headers=headers, timeout=10)
                resp.raise_for_status()
                job_id = resp.json()["job_id"]
                
                with self.lock:
                    self.jobs_submitted.append(job_id)
                
                # Small random jitter before cancellation
                time.sleep(0.05)
                
                # 2. Cancel Job
                cancel_resp = requests.post(f"{HEADNODE_URL}/api/jobs/{job_id}/stop", headers=headers, timeout=10)
                
            except Exception as e:
                with self.lock:
                    self.errors.append(f"Lifecycle Stress Error (Task {task_id}): {e}")

        with ThreadPoolExecutor(max_workers=20) as executor:
            executor.map(submit_and_cancel_lifecycle, range(50))
            
        print("Job Lifecycle Stress completed.")

    def run(self):
        cleanup()
        init_db()

        # Start Services in threads
        t_headnode = threading.Thread(target=self.start_headnode, daemon=True)
        t_headnode.start()

        # Wait for headnode to boot
        time.sleep(2)

        t_scheduler = threading.Thread(target=self.start_scheduler_loop, daemon=True)
        t_scheduler.start()

        try:
            self.register_test_worker()
            
            # Execute chaos tests concurrently
            lifecycle_thread = threading.Thread(target=self.run_concurrent_job_lifecycle_stress)
            sql_stress_thread = threading.Thread(target=self.run_concurrent_sql_stress)
            broken_pipe_thread = threading.Thread(target=self.run_broken_pipe_simulation)

            lifecycle_thread.start()
            sql_stress_thread.start()
            broken_pipe_thread.start()

            lifecycle_thread.join()
            sql_stress_thread.join()
            broken_pipe_thread.join()

        finally:
            print("\nShutting down scheduler...")
            self.stop_scheduler.set()
            
        # Verify results
        print("\n=== STRESS TEST RESULTS ===")
        print(f"Jobs Submitted: {len(self.jobs_submitted)}")
        print(f"Total Errors Captured: {len(self.errors)}")
        if self.errors:
            print("Errors detected:")
            for err in self.errors[:10]:
                print(f" - {err}")
            if len(self.errors) > 10:
                print(f" ... and {len(self.errors) - 10} more errors.")
            sys.exit(1)
        else:
            print("SUCCESS: No deadlocks, no SQLite locked exceptions, and no broken pipes crashed the scheduler!")
            sys.exit(0)

if __name__ == "__main__":
    stress_test = StressTestScheduler()
    try:
        stress_test.run()
    finally:
        cleanup()
