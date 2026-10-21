import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Configure environment before imports
os.environ["CLUSTER_ZERO_TRUST"] = "1"
os.environ["CLUSTER_TOKEN"] = "test-cluster-token-123"
os.environ["GITHUB_ALLOWED_ORG"] = "UNIL-DESI"

# Ensure imports resolve to worktree
_worktree = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _worktree not in sys.path:
    sys.path.insert(0, _worktree)
_src = os.path.join(_worktree, "src")
if _src not in sys.path:
    sys.path.insert(0, _src)
_scheduler = os.path.join(_src, "scheduler")
if _scheduler not in sys.path:
    sys.path.insert(0, _scheduler)

import headnode_service
from headnode_service import app
import worker_agent


class TestZeroTrustGitHubOrgAuth(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.app.config["SECRET_KEY"] = "test-secret-key"
        self.client = self.app.test_client()
        headnode_service._GITHUB_AUTH_CACHE.clear()

    def test_anonymous_healthz_allowed(self):
        """Anonymous access to /healthz returns 200 OK."""
        res = self.client.get("/healthz")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"status": "ok"})

    def test_anonymous_dashboard_shows_login_page(self):
        """Anonymous access to / returns 200 with login.html."""
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Cluster-CI", res.data)
        self.assertIn(b"Continue with GitHub", res.data)

    def test_anonymous_access_denied_on_protected_endpoints(self):
        """Anonymous requests to protected endpoints return 401."""
        endpoints = [
            ("GET", "/scheduler_status"),
            ("GET", "/workers"),
            ("GET", "/job_status/j1"),
            ("GET", "/api/jobs/j1/logs"),
            ("POST", "/api/jobs/j1/logs"),
            ("POST", "/clean_ghosts"),
            ("GET", "/check_space"),
            ("GET", "/artifacts/test_owner/test_repo/main/file.txt"),
            ("GET", "/api/runs/j1/files"),
            ("GET", "/api/queue"),
            ("GET", "/api/projects"),
        ]
        for method, path in endpoints:
            with self.subTest(method=method, path=path):
                if method == "GET":
                    res = self.client.get(path)
                else:
                    res = self.client.post(path)
                self.assertEqual(res.status_code, 401, f"{method} {path} should return 401 but returned {res.status_code}")

    @patch("headnode_service.oauth.github.authorize_access_token")
    @patch("headnode_service.oauth.github.get")
    @patch("headnode_service.verify_github_org_membership")
    def test_authorize_non_member_rejected_403(self, mock_verify, mock_get, mock_token):
        """Account not in UNIL-DESI is rejected with 403 and no session created."""
        mock_token.return_value = {"access_token": "gho_fake_token"}
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"login": "attacker_user"}
        mock_get.return_value = mock_resp
        mock_verify.return_value = False

        res = self.client.get("/authorize?code=123")
        self.assertEqual(res.status_code, 403)
        self.assertIn(b"Forbidden", res.data)
        self.assertIn(b"attacker_user", res.data)

        # Ensure session is empty
        with self.client.session_transaction() as sess:
            self.assertNotIn("user", sess)
            self.assertNotIn("org_verified", sess)

    @patch("headnode_service.oauth.github.authorize_access_token")
    @patch("headnode_service.oauth.github.get")
    @patch("headnode_service.verify_github_org_membership")
    def test_authorize_member_redirects_and_grants_access(self, mock_verify, mock_get, mock_token):
        """Account member of UNIL-DESI is granted session and accesses scheduler_status and queue."""
        mock_token.return_value = {"access_token": "gho_member_token"}
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"login": "unil_researcher"}
        mock_get.return_value = mock_resp
        mock_verify.return_value = True

        res = self.client.get("/authorize?code=123")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers.get("Location"), "/")

        # Access scheduler_status with this authenticated session
        res_status = self.client.get("/scheduler_status")
        self.assertEqual(res_status.status_code, 200)

        res_queue = self.client.get("/api/queue")
        self.assertEqual(res_queue.status_code, 200)

    def test_user_session_cannot_access_worker_only_endpoints(self):
        """A valid user session cannot call machine/worker endpoints."""
        with self.client.session_transaction() as sess:
            sess["user"] = {"login": "unil_researcher"}
            sess["org_verified"] = "unil-desi"

        worker_endpoints = [
            ("POST", "/register_worker", {"worker_id": "w1", "hostname": "node1"}),
            ("POST", "/api/jobs/j1/logs", {"logs": "hello"}),
            ("GET", "/worker_poll/w1", None),
        ]
        for method, path, data in worker_endpoints:
            with self.subTest(path=path):
                if method == "POST":
                    res = self.client.post(path, json=data)
                else:
                    res = self.client.get(path)
                self.assertEqual(res.status_code, 401, f"{path} should reject user session with 401")

    def test_cluster_token_allows_api_and_worker_routes(self):
        """Machine requests with Bearer CLUSTER_TOKEN succeed on API and worker routes."""
        headers = {"Authorization": "Bearer test-cluster-token-123"}
        res_space = self.client.get("/check_space", headers=headers)
        self.assertEqual(res_space.status_code, 200)

        res_status = self.client.get("/scheduler_status", headers=headers)
        self.assertEqual(res_status.status_code, 200)

    @patch("headnode_service.requests.get")
    def test_github_bearer_token_member_allowed_cli(self, mock_get):
        """A GitHub PAT with active UNIL-DESI membership accesses scheduler_status."""
        def fake_get(url, *args, **kwargs):
            mock_r = MagicMock()
            if "/user/orgs" in url or "/orgs/UNIL-DESI/memberships/" in url:
                mock_r.status_code = 200
                mock_r.json.return_value = {"state": "active"}
            elif url.endswith("/user"):
                mock_r.status_code = 200
                mock_r.json.return_value = {"login": "cli_researcher"}
            else:
                mock_r.status_code = 404
            return mock_r

        mock_get.side_effect = fake_get
        headers = {"Authorization": "Bearer ghp_valid_member_token"}
        res = self.client.get("/scheduler_status", headers=headers)
        self.assertEqual(res.status_code, 200)

    @patch("headnode_service.requests.get")
    def test_github_bearer_token_non_member_rejected(self, mock_get):
        """A GitHub PAT outside UNIL-DESI is rejected with 401."""
        def fake_get(url, *args, **kwargs):
            mock_r = MagicMock()
            if url.endswith("/user"):
                mock_r.status_code = 200
                mock_r.json.return_value = {"login": "outsider"}
            else:
                mock_r.status_code = 404
                mock_r.json.return_value = []
            return mock_r

        mock_get.side_effect = fake_get
        headers = {"Authorization": "Bearer ghp_outsider_token"}
        res = self.client.get("/scheduler_status", headers=headers)
        self.assertIn(res.status_code, (401, 403))

    @patch("headnode_service.requests.get")
    def test_auth_token_endpoint(self, mock_get):
        """POST /auth/token authenticates UNIL-DESI member and rejects non-member."""
        # Non-member test
        def non_member_get(url, *args, **kwargs):
            mock_r = MagicMock()
            if url.endswith("/user"):
                mock_r.status_code = 200
                mock_r.json.return_value = {"login": "outsider"}
            else:
                mock_r.status_code = 404
                mock_r.json.return_value = []
            return mock_r

        mock_get.side_effect = non_member_get
        res_fail = self.client.post("/auth/token", json={"token": "ghp_outsider_token"})
        self.assertEqual(res_fail.status_code, 403)

        # Member test
        headnode_service._GITHUB_AUTH_CACHE.clear()
        def member_get(url, *args, **kwargs):
            mock_r = MagicMock()
            if url.endswith("/user"):
                mock_r.status_code = 200
                mock_r.json.return_value = {"login": "unil_member"}
            else:
                mock_r.status_code = 200
                mock_r.json.return_value = {"state": "active"}
            return mock_r

        mock_get.side_effect = member_get
        res_ok = self.client.post("/auth/token", json={"token": "ghp_member_token"})
        self.assertEqual(res_ok.status_code, 200)
        self.assertEqual(res_ok.get_json()["status"], "ok")

        # Session should now allow access to scheduler_status
        res_sched = self.client.get("/scheduler_status")
        self.assertEqual(res_sched.status_code, 200)

    def test_worker_agent_control_endpoints_require_token(self):
        """Worker agent control routes reject requests without CLUSTER_TOKEN."""
        worker_app = worker_agent.app
        worker_app.config["TESTING"] = True
        wclient = worker_app.test_client()

        # Without token
        res_crash = wclient.get("/crash_report")
        self.assertEqual(res_crash.status_code, 401)

        res_cancel = wclient.post("/cancel/j1")
        self.assertEqual(res_cancel.status_code, 401)

        # With valid token
        headers = {"Authorization": "Bearer test-cluster-token-123"}
        res_valid = wclient.get("/crash_report", headers=headers)
        self.assertIn(res_valid.status_code, (200, 500))  # May fail shell dmesg in test env, but token auth passed


if __name__ == "__main__":
    unittest.main()
