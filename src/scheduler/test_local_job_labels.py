import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import headnode_service
from persistence import get_db_conn, init_db


class TestLocalJobLabels(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.old_db_path = os.environ.get("CLUSTER_DB_PATH")
        self.old_repos_dir = headnode_service.REPOS_DIR
        self.old_cluster_token = headnode_service.CLUSTER_TOKEN
        os.environ["CLUSTER_DB_PATH"] = os.path.join(self.temp_dir, "scheduler.db")
        headnode_service.REPOS_DIR = os.path.join(self.temp_dir, "repositories")
        headnode_service.CLUSTER_TOKEN = None
        init_db()
        self.client = headnode_service.app.test_client()

    def tearDown(self):
        headnode_service.REPOS_DIR = self.old_repos_dir
        headnode_service.CLUSTER_TOKEN = self.old_cluster_token
        if self.old_db_path is None:
            os.environ.pop("CLUSTER_DB_PATH", None)
        else:
            os.environ["CLUSTER_DB_PATH"] = self.old_db_path
        shutil.rmtree(self.temp_dir)

    def _source_transfer(self, content=b"source"):
        create = self.client.post("/api/local_transfers", json={
            "purpose": "source",
            "total_size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
        self.assertEqual(create.status_code, 201)
        transfer = create.get_json()
        transfer_id = transfer["transfer_id"]
        upload = self.client.put(
            f"/api/local_transfers/{transfer_id}/chunks/0",
            data=content,
            headers={"X-Chunk-SHA256": hashlib.sha256(content).hexdigest()},
            content_type="application/octet-stream",
        )
        self.assertEqual(upload.status_code, 200)
        complete = self.client.post(f"/api/local_transfers/{transfer_id}/complete", json={})
        self.assertEqual(complete.status_code, 200)
        return transfer_id

    def _submit(self, username="alice", label=None, repo="lab/project", branch="untrusted"):
        payload = {
            "repo": repo,
            "branch": branch,
            "username": username,
            "is_local": True,
            "source_transfer_id": self._source_transfer(),
            "ram_required_gb": 1,
            "vram_required_gb": 0,
            "max_runtime_hours": 1,
        }
        if label is not None:
            payload["local_label"] = label
        return self.client.post("/submit_job", json=payload)

    def _jobs(self):
        with get_db_conn() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT job_id, repo, branch, username, status, env_vars FROM jobs ORDER BY created_at, rowid"
            ).fetchall()]

    def test_missing_label_is_backward_compatible_and_branch_is_server_owned(self):
        response = self._submit(branch="local-draft/mallory/injected")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["local_label"], "default")
        self.assertEqual(body["branch"], "local-draft/alice")
        self.assertEqual(self._jobs()[0]["branch"], "local-draft/alice")

    def test_different_labels_coexist_for_same_user(self):
        first = self._submit(label="summary-a")
        second = self._submit(label="summary-b", repo="lab/other")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        jobs = self._jobs()
        self.assertEqual([job["status"] for job in jobs], ["pending", "pending"])
        self.assertEqual(
            {job["branch"] for job in jobs},
            {"local-draft/alice/summary-a", "local-draft/alice/summary-b"},
        )

    def test_same_label_replaces_only_matching_job_across_projects(self):
        replaced = self._submit(label="summary-a", repo="lab/first").get_json()["job_id"]
        preserved = self._submit(label="summary-b", repo="lab/second").get_json()["job_id"]
        replacement_response = self._submit(label="summary-a", repo="lab/third")
        self.assertEqual(replacement_response.status_code, 200)
        replacement = replacement_response.get_json()["job_id"]
        jobs = {job["job_id"]: job for job in self._jobs()}
        self.assertEqual(jobs[replaced]["status"], "failed")
        self.assertEqual(jobs[preserved]["status"], "pending")
        self.assertEqual(jobs[replacement]["status"], "pending")
        replacement_env = json.loads(jobs[replacement]["env_vars"])
        self.assertEqual(replacement_env["CLUSTER_CANCELLED_RUNS"], replaced)

    def test_same_label_for_different_users_does_not_collide(self):
        self._submit(username="alice", label="shared")
        self._submit(username="bob", label="shared")
        jobs = self._jobs()
        self.assertEqual([job["status"] for job in jobs], ["pending", "pending"])

    def test_default_label_replaces_legacy_default_job(self):
        first = self._submit().get_json()["job_id"]
        second = self._submit(label="default").get_json()["job_id"]
        jobs = {job["job_id"]: job for job in self._jobs()}
        self.assertEqual(jobs[first]["status"], "failed")
        self.assertEqual(jobs[second]["status"], "pending")

    def test_invalid_label_is_rejected_before_job_creation(self):
        response = self._submit(label="bad/label")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Local job labels", response.get_json()["error"])
        self.assertEqual(self._jobs(), [])

    def test_simultaneous_same_label_leaves_exactly_one_active_job(self):
        barrier = threading.Barrier(8)
        responses = []
        errors = []

        def submit(index):
            try:
                with headnode_service.app.test_client() as client:
                    content = f"source-{index}".encode()
                    create = client.post("/api/local_transfers", json={
                        "purpose": "source",
                        "total_size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }).get_json()
                    transfer_id = create["transfer_id"]
                    client.put(
                        f"/api/local_transfers/{transfer_id}/chunks/0",
                        data=content,
                        headers={"X-Chunk-SHA256": hashlib.sha256(content).hexdigest()},
                        content_type="application/octet-stream",
                    )
                    client.post(f"/api/local_transfers/{transfer_id}/complete", json={})
                    barrier.wait(timeout=5)
                    response = client.post("/submit_job", json={
                        "repo": f"lab/project-{index}",
                        "username": "alice",
                        "is_local": True,
                        "local_label": "simultaneous",
                        "source_transfer_id": transfer_id,
                        "ram_required_gb": 1,
                        "vram_required_gb": 0,
                        "max_runtime_hours": 1,
                    })
                    responses.append(response.status_code)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertEqual(errors, [])
        self.assertEqual(responses, [200] * 8)
        jobs = self._jobs()
        self.assertEqual(sum(job["status"] == "pending" for job in jobs), 1)
        self.assertEqual(sum(job["status"] == "failed" for job in jobs), 7)


if __name__ == "__main__":
    unittest.main()
