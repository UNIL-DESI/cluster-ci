import unittest
import os
os.environ["CLUSTER_TOKEN"] = ""
import sys
import time
import threading
import requests
import sqlite3

# Add src/scheduler to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from persistence import init_db
from headnode_service import app
from scheduler_loop import schedule_jobs

class TestScheduler(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.is_e2e = os.environ.get("E2E_TEST") == "1"
        cls.headnode_url = os.environ.get("HEADNODE_URL", "http://localhost:5001")

        if not cls.is_e2e:
            os.environ["CLUSTER_DB_PATH"] = "test_cluster.db"
            if os.path.exists("test_cluster.db"):
                os.remove("test_cluster.db")
            init_db()

            # Start Headnode API in a thread using werkzeug.serving for clean shutdown
            from werkzeug.serving import make_server
            import socket
            def get_free_port():
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(('', 0))
                port = s.getsockname()[1]
                s.close()
                return port

            cls.test_port = get_free_port()
            cls.headnode_url = f"http://localhost:{cls.test_port}"

            cls.server = make_server('0.0.0.0', cls.test_port, app)
            cls.api_thread = threading.Thread(target=cls.server.serve_forever)
            cls.api_thread.daemon = True
            cls.api_thread.start()

            # Start Scheduler Loop in a thread
            cls.stop_event = threading.Event()
            cls.loop_thread = threading.Thread(target=schedule_jobs, args=(cls.stop_event,))
            cls.loop_thread.daemon = True
            cls.loop_thread.start()

            # Wait for API to be ready
            time.sleep(2)

    def test_full_flow(self):
        headnode_url = self.headnode_url

        # 1. Register a worker
        resp = requests.post(f"{headnode_url}/register_worker", json={
            "worker_id": "worker-1",
            "hostname": "lenovo-1",
            "total_ram_gb": 64.0,
            "available_ram_gb": 64.0
        })
        self.assertEqual(resp.status_code, 200)

        # 2. Submit a job
        resp = requests.post(f"{headnode_url}/submit_job", json={
            "repo": "owner/repo",
            "branch": "main",
            "ram_required_gb": 16.0
        })
        self.assertEqual(resp.status_code, 200)
        job_id = resp.json()['job_id']

        # 3. Wait for scheduler to assign the job
        time.sleep(7)

        # 4. Check worker poll
        resp = requests.get(f"{headnode_url}/worker_poll/worker-1")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['job_id'], job_id)

        # 5. Update job status
        resp = requests.post(f"{headnode_url}/update_job_status", json={
            "job_id": job_id,
            "status": "completed",
            "exit_code": 0
        })
        self.assertEqual(resp.status_code, 200)

        # 6. Verify final status
        resp = requests.get(f"{headnode_url}/job_status/{job_id}")
        self.assertEqual(resp.json()['status'], 'completed')

    def test_e2e_real_worker(self):
        if not self.is_e2e:
            self.skipTest("Only for E2E mode")

        headnode_url = self.headnode_url

        # (a) Verify a worker is registered
        print("Waiting for a worker to register...")
        worker = None
        for _ in range(30):
            resp = requests.get(f"{headnode_url}/workers")
            workers = resp.json()
            if workers:
                worker = workers[0]
                break
            time.sleep(1)

        self.assertIsNotNone(worker, "No worker registered in time")
        initial_ram = worker['available_ram_gb']
        worker_id = worker['worker_id']
        print(f"Worker {worker_id} found with {initial_ram}GB RAM")

        # (b) Submit a job
        ram_required = 4.0
        print(f"Submitting job requiring {ram_required}GB RAM...")
        resp = requests.post(f"{headnode_url}/submit_job", json={
            "repo": "owner/repo-e2e",
            "branch": "main",
            "ram_required_gb": ram_required
        })
        self.assertEqual(resp.status_code, 200)
        job_id = resp.json()['job_id']

        # (c) Monitor job status until completed
        print(f"Monitoring job {job_id}...")
        final_status = None
        for _ in range(60):
            resp = requests.get(f"{headnode_url}/job_status/{job_id}")
            status_data = resp.json()
            final_status = status_data['status']
            if final_status in ['completed', 'failed']:
                # (d) Confirm job was assigned to a worker (Bin-Packing check)
                self.assertIsNotNone(status_data['worker_id'], "Job was never assigned to a worker")
                break
            time.sleep(2)

        self.assertEqual(final_status, 'completed', f"Job failed or timed out with status: {final_status}")
        print("Job completed successfully")

        # (e) Verify RAM restoration
        resp = requests.get(f"{headnode_url}/workers")
        workers = resp.json()
        target_worker = next(w for w in workers if w['worker_id'] == worker_id)
        print(f"Worker {worker_id} now has {target_worker['available_ram_gb']}GB RAM")
        self.assertEqual(target_worker['available_ram_gb'], initial_ram, "RAM was not correctly restored")

    @classmethod
    def tearDownClass(cls):
        if not cls.is_e2e:
            cls.stop_event.set()
            cls.loop_thread.join(timeout=5)
            cls.server.shutdown()
            cls.api_thread.join(timeout=5)
            try:
                if os.path.exists("test_cluster.db"):
                    os.remove("test_cluster.db")
            except PermissionError:
                pass

if __name__ == '__main__':
    unittest.main()
