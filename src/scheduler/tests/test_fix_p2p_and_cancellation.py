import unittest
import os
import sys
import json
import sqlite3
import datetime
from unittest.mock import patch, MagicMock

# Add src/scheduler to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from persistence import init_db, get_db_conn
import headnode_service
import scheduler_loop
import worker_agent

class TestFixP2PAndCancellation(unittest.TestCase):
    def setUp(self):
        self.db_path = f"test_fix_p2p_and_cancellation_{self._testMethodName}.db"
        os.environ["CLUSTER_DB_PATH"] = self.db_path
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        init_db()
        
        # Disable token verification for unit testing
        self.saved_token = headnode_service.CLUSTER_TOKEN
        headnode_service.CLUSTER_TOKEN = None
        
        headnode_service.app.config['TESTING'] = True
        self.client = headnode_service.app.test_client()

    def tearDown(self):
        headnode_service.CLUSTER_TOKEN = self.saved_token
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_register_worker_typo_correction(self):
        # Register a worker with the typo IP
        payload = {
            "worker_id": "test-worker-typo",
            "hostname": "test-host",
            "service_url": "http://1300.223.169.200:6000",
            "total_ram_gb": 64.0,
            "total_storage_gb": 1000.0,
            "available_storage_gb": 500.0
        }
        resp = self.client.post('/register_worker', json=payload)
        self.assertEqual(resp.status_code, 200)

        # Check DB to ensure it was corrected to 130.223.169.200
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT service_url FROM workers WHERE worker_id = "test-worker-typo"')
            worker = cursor.fetchone()
            self.assertEqual(worker['service_url'], "http://130.223.169.200:6000")

    @patch('requests.post')
    def test_scheduler_loop_p2p_typo_correction(self, mock_post):
        # 1. Register two workers, one with typo IP
        # We give worker-B more RAM so that it is ordered first in case of equal scores
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, available_ram_gb, last_seen, status)
                VALUES (?, ?, ?, ?, ?, datetime('now'), 'online')
            ''', ("worker-A", "host-A", "http://1300.223.169.200:6000", 64.0, 64.0))
            cursor.execute('''
                INSERT INTO workers (worker_id, hostname, service_url, total_ram_gb, available_ram_gb, last_seen, status)
                VALUES (?, ?, ?, ?, ?, datetime('now'), 'online')
            ''', ("worker-B", "host-B", "http://worker-B:6000", 128.0, 128.0))

            # 2. Submit a job with 2 required hashes
            hashes = ["hash1", "hash2"]
            cursor.execute('''
                INSERT INTO jobs (job_id, repo, branch, ram_required_gb, required_hashes, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
            ''', ("job-p2p-test", "owner/repo", "main", 16.0, json.dumps(hashes)))
            conn.commit()

        # 3. Mock worker responses:
        # Both workers return 1 hash to have a score of 1.
        # Since scores are equal, the tie-breaker is total_ram_gb, assigning the job to worker-B (128GB).
        # worker-A (64GB) is the peer with score > 0, so p2p_url will point to it.
        def side_effect(url, **kwargs):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            if "worker-B" in url:
                mock_resp.json.return_value = ["hash1"]
            else:
                mock_resp.json.return_value = ["hash2"]
            return mock_resp

        mock_post.side_effect = side_effect

        with patch('time.sleep', side_effect=InterruptedError):
            try:
                scheduler_loop.schedule_jobs()
            except InterruptedError:
                pass

        # Verify job was assigned to B and has corrected p2p_url pointing to A
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT worker_id, p2p_url FROM jobs WHERE job_id = "job-p2p-test"')
            job = cursor.fetchone()
            self.assertEqual(job['worker_id'], "worker-B")
            self.assertEqual(job['p2p_url'], "http://130.223.169.200:6000/fetch_artifact")

    @patch('headnode_service.cancel_job_cleanly')
    def test_auto_cancellation_log_injection(self, mock_cancel):
        # 1. Insert an active job in DB
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO jobs (job_id, repo, branch, username, status)
                VALUES (?, ?, ?, ?, 'running')
            ''', ("active-job-1", "owner/repo", "main", "henri"))
            conn.commit()

        # 2. Submit a new job on the exact same branch and user
        payload = {
            "repo": "owner/repo",
            "branch": "main",
            "username": "henri",
            "ram_required_gb": 4.0
        }
        
        # Mock metadata extraction to avoid git clone attempts during testing
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=1) # force no dvc.lock logic
            resp = self.client.post('/submit_job', json=payload)
            self.assertEqual(resp.status_code, 200)
            new_job_id = resp.json['job_id']

        # Verify cancel_job_cleanly was called for active-job-1
        mock_cancel.assert_called_with("active-job-1", exit_code=-15)

        # Verify new job has env_vars populated with CLUSTER_CANCELLED_RUNS
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT env_vars FROM jobs WHERE job_id = ?', (new_job_id,))
            job = cursor.fetchone()
            env_vars = json.loads(job['env_vars']) if job['env_vars'] else {}
            self.assertIn("CLUSTER_CANCELLED_RUNS", env_vars)
            self.assertEqual(env_vars["CLUSTER_CANCELLED_RUNS"], "active-job-1")

    @patch('requests.get')
    @patch('subprocess.Popen')
    @patch('worker_agent.purge_orphan_runners_and_containers')
    @patch('worker_agent.update_job_status')
    def test_worker_agent_writes_cancellation_log(self, mock_status, mock_purge, mock_popen, mock_get):
        # Prepare mock process
        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.__iter__ = MagicMock(return_value=iter([]))
        mock_proc.poll.side_effect = [None, 0] # loop runs once, then terminates
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        # Mock requests.get for watchdog self-healing to avoid HTTP failures
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "running"}
        mock_get.return_value = mock_resp

        # Create job dict
        job = {
            "job_id": "new-job-with-cancellations",
            "repo": "owner/repo",
            "branch": "main",
            "ram_required_gb": 4.0,
            "env_vars": json.dumps({
                "CLUSTER_CANCELLED_RUNS": "cancelled-run-A,cancelled-run-B"
            })
        }

        # Run execute_job
        worker_agent.execute_job(job)

        # Verify log file contents
        log_path = os.path.join(worker_agent.LOGS_DIR, f"{job['job_id']}.log")
        self.assertTrue(os.path.exists(log_path))
        
        with open(log_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        self.assertEqual(len(lines), 2)
        self.assertIn("ℹ️  Previous active run [cancelled-run-A] has been cancelled by this new submission.", lines[0])
        self.assertIn("ℹ️  Previous active run [cancelled-run-B] has been cancelled by this new submission.", lines[1])

        # Clean up log file
        if os.path.exists(log_path):
            os.remove(log_path)

if __name__ == '__main__':
    unittest.main()
