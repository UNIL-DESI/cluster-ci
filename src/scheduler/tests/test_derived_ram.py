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

class TestDerivedRAM(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["CLUSTER_DB_PATH"] = "test_derived_ram.db"
        if os.path.exists("test_derived_ram.db"):
            os.remove("test_derived_ram.db")
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

    def test_derived_ram_calculation(self):
        headnode_url = self.headnode_url
        worker_id = "worker-ram-test"

        # 1. Register a worker with 64GB
        resp = requests.post(f"{headnode_url}/register_worker", json={
            "worker_id": worker_id,
            "hostname": "test-host",
            "total_ram_gb": 64.0
        })
        self.assertEqual(resp.status_code, 200)

        # 2. Check initial available RAM (should be 64.0 - 2.0 safety margin = 62.0)
        resp = requests.get(f"{headnode_url}/workers")
        workers = resp.json()
        target_worker = next(w for w in workers if w['worker_id'] == worker_id)
        self.assertEqual(target_worker['available_ram_gb'], 62.0)

        # 3. Submit a job requiring 16GB
        resp = requests.post(f"{headnode_url}/submit_job", json={
            "repo": "owner/repo",
            "branch": "main",
            "ram_required_gb": 16.0
        })
        self.assertEqual(resp.status_code, 200)
        job_id = resp.json()['job_id']

        # 4. Wait for scheduler to assign the job
        time.sleep(7)

        # 5. Check derived available RAM (should be 64.0 - 2.0 margin - 16.0 = 46.0)
        resp = requests.get(f"{headnode_url}/workers")
        workers = resp.json()
        target_worker = next(w for w in workers if w['worker_id'] == worker_id)
        self.assertEqual(target_worker['available_ram_gb'], 46.0)

        # 6. Complete the job
        resp = requests.post(f"{headnode_url}/update_job_status", json={
            "job_id": job_id,
            "status": "completed",
            "exit_code": 0
        })
        self.assertEqual(resp.status_code, 200)

        # 7. Check derived available RAM again (should be back to 62.0)
        resp = requests.get(f"{headnode_url}/workers")
        workers = resp.json()
        target_worker = next(w for w in workers if w['worker_id'] == worker_id)
        self.assertEqual(target_worker['available_ram_gb'], 62.0)

    @classmethod
    def tearDownClass(cls):
        cls.stop_event.set()
        cls.loop_thread.join(timeout=5)
        cls.server.shutdown()
        cls.api_thread.join(timeout=5)
        try:
            if os.path.exists("test_derived_ram.db"):
                os.remove("test_derived_ram.db")
        except PermissionError:
            pass

if __name__ == '__main__':
    unittest.main()
