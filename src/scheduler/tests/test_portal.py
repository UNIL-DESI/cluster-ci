import unittest
import os
import time
import subprocess
import requests
import threading
from unittest.mock import patch, MagicMock
# Import app AFTER setting env vars
os.environ['GITHUB_CLIENT_ID'] = 'fake_id'
os.environ['GITHUB_CLIENT_SECRET'] = 'fake_secret'
os.environ['CLUSTER_DB_PATH'] = 'test_cluster_scheduler.db'
os.environ['DVC_VIEWER_TIMEOUT_MIN'] = '0' # Force immediate timeout for testing cleanup
from src.scheduler.headnode_service import app, local_viewers, local_viewers_lock, cleanup_inactive_viewers
from src.scheduler.persistence import init_db, get_db_conn

class TestPortalAndProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        # Ensure session is used
        app.config['TESTING'] = True
        app.config['SECRET_KEY'] = 'test'
        cls.client = app.test_client()
        cls.app_context = app.app_context()
        cls.app_context.push()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists('test_cluster_scheduler.db'):
            os.remove('test_cluster_scheduler.db')
        cls.app_context.pop()

    def setUp(self):
        with local_viewers_lock:
            local_viewers.clear()
        from src.scheduler.headnode_service import remote_viewers, remote_viewers_lock
        with remote_viewers_lock:
            remote_viewers.clear()
            
        # Clear database tables to ensure isolation
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM workers')
            cursor.execute('DELETE FROM jobs')
            conn.commit()

        # Clear session for each test to ensure isolation
        with self.client.session_transaction() as sess:
            sess.clear()

    def test_dashboard_renders_login_template(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Cluster-CI Portal', response.data)

    def test_view_project_redirects_when_not_logged_in(self):
        # Let's try to fetch what exactly is in the url_map for view_project
        with app.test_request_context():
            from flask import url_for
            target = url_for('view_project', owner='someowner', repo='somerepo')

        response = self.client.get(target, follow_redirects=False)
        self.assertEqual(response.status_code, 302, f"Failed for {target}. Body: {response.data}")
        self.assertTrue(response.location.endswith('/'))

    def test_view_project_404_when_repo_missing(self):
        with self.client.session_transaction() as sess:
            sess['user'] = {'login': 'testuser'}

        # Insert an online worker so the headnode can query it
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO workers (worker_id, hostname, service_url, status)
                VALUES ('w1', 'worker1', 'http://worker1:6000', 'online')
            ''')
            conn.commit()

        with app.test_request_context():
            from flask import url_for
            target = url_for('view_project', owner='nonexistent', repo='repo')

        # Mock the worker agent response to return 404 (repo missing on worker)
        with patch('requests.post') as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_resp.text = "Repository nonexistent/repo not found on this worker"
            mock_post.return_value = mock_resp

            response = self.client.get(target, follow_redirects=True)
            self.assertEqual(response.status_code, 502) # worker returns non-200 -> 502 Bad Gateway
            body = response.data.decode('utf-8')
            self.assertIn('Failed to start historical dvc-viewer on worker', body)

    def test_historical_proxy_spawns_process(self):
        # Mock login
        with self.client.session_transaction() as sess:
            sess['user'] = {'login': 'testuser'}
            sess['token'] = {'access_token': 'fake_token'}

        # Insert an online worker so the headnode can query it
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO workers (worker_id, hostname, service_url, status)
                VALUES ('w1', 'worker1', 'http://worker1:6000', 'online')
            ''')
            conn.commit()

        with patch('requests.post') as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"status": "ok", "port": 8888}
            mock_post.return_value = mock_resp

            with patch('src.scheduler.headnode_service.proxy_request') as mock_proxy:
                mock_proxy.return_value = app.response_class("proxied")
                response = self.client.get('/view/testowner/testrepo/')

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, b'proxied')
                
                # Check requests.post was called to start visualizer on worker
                mock_post.assert_called_once_with(
                    'http://worker1:6000/api/worker/dvc-viewer/start',
                    json={'repo': 'testowner/testrepo', 'rev': 'main'},
                    timeout=60
                )
                
                from src.scheduler.headnode_service import remote_viewers, remote_viewers_lock
                with remote_viewers_lock:
                    self.assertIn('testowner/testrepo', remote_viewers)
                    self.assertEqual(remote_viewers['testowner/testrepo']['port'], 8888)

    def test_inactivity_cleanup(self):
        # Manually add a fake viewer to the registry
        mock_proc = MagicMock()
        with local_viewers_lock:
            local_viewers['old/repo'] = {
                'proc': mock_proc,
                'port': 12345,
                'last_access': time.time() - 10 # 10 seconds ago, and timeout is 0
            }

        from src.scheduler.headnode_service import cleanup_inactive_viewers
        import src.scheduler.headnode_service
        src.scheduler.headnode_service.DVC_VIEWER_TIMEOUT_MIN = 0

        with patch('time.sleep', side_effect=[None, Exception("Stop loop")]):
            try:
                cleanup_inactive_viewers()
            except Exception as e:
                if str(e) != "Stop loop":
                    raise e

        with local_viewers_lock:
            self.assertNotIn('old/repo', local_viewers)
        mock_proc.terminate.assert_called()

if __name__ == '__main__':
    unittest.main()
