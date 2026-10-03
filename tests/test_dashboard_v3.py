"""
Test suite and mock server for Cluster-CI v3 Dashboard (Worker W12).
Serves src/scheduler/templates/dashboard.html with mock endpoints
for both classic (parallel_mode=0) and v3 (parallel_mode=1) multi-node jobs.
"""

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from flask import Flask, jsonify, request, Response

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = BASE_DIR / "src" / "scheduler" / "templates" / "dashboard.html"
FIXTURE_PATH = BASE_DIR / "tests" / "fixtures" / "v3_job_fixture.json"


def load_fixtures():
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def create_test_app():
    fixtures = load_fixtures()
    app = Flask(__name__)

    @app.route("/")
    def index():
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            html = f.read()
        return Response(html, mimetype="text/html")

    @app.route("/api/projects")
    def api_projects():
        return jsonify([
            {
                "id": "org/latent-multimodal-v3",
                "repo": "org/latent-multimodal-v3",
                "name": "latent-multimodal-v3",
                "active_runs": [fixtures["v3_job"]],
                "last_run": fixtures["v3_job"]
            },
            {
                "id": "org/classic-pipeline",
                "repo": "org/classic-pipeline",
                "name": "classic-pipeline",
                "active_runs": [],
                "last_run": fixtures["classic_job"]
            }
        ])

    @app.route("/api/runs/active")
    def api_runs_active():
        return jsonify([fixtures["v3_job"]])

    @app.route("/api/runs/history")
    def api_runs_history():
        return jsonify({
            "items": [fixtures["v3_job"], fixtures["classic_job"]],
            "total": 2,
            "page": 1,
            "per_page": 20
        })

    @app.route("/scheduler_status")
    def scheduler_status():
        workers = fixtures["scheduler_status"]["active_workers"]
        return jsonify({
            "workers": {w["id"]: w for w in workers},
            "active_workers": workers,
            "queue_count": 0,
            "running_count": 1,
            "cluster_utilization": fixtures["scheduler_status"]["cluster_utilization"]
        })

    @app.route("/workers")
    def workers_endpoint():
        return jsonify(fixtures["scheduler_status"]["active_workers"])

    @app.route("/job_status/<job_id>")
    def job_status(job_id):
        if "v3" in job_id or job_id == fixtures["v3_job"]["id"]:
            return jsonify(fixtures["v3_job"])
        return jsonify(fixtures["classic_job"])

    @app.route("/api/jobs/<job_id>/logs")
    def job_logs(job_id):
        offset = int(request.args.get("offset", 0))
        if "v3" in job_id or job_id == fixtures["v3_job"]["id"]:
            raw_logs = fixtures["v3_job_logs"]
        else:
            raw_logs = fixtures["classic_job_logs"]

        slice_logs = raw_logs[offset:]
        return jsonify({
            "logs": slice_logs,
            "offset": offset + len(slice_logs),
            "finished": True
        })

    @app.route("/favicon.ico")
    def favicon():
        return ("", 204)

    @app.route("/api/projects/<path:project_name>/branches")
    def api_project_branches(project_name):
        return jsonify([{"name": "master"}, {"name": "main"}, {"name": "feat/distributed-dvc"}])

    @app.route("/api/projects/<path:project_name>/artifacts/latest")
    def api_project_artifacts(project_name):
        return jsonify([])

    @app.route("/api/projects/<path:project_name>/runs")
    def api_project_runs(project_name):
        if "latent" in project_name or "v3" in project_name:
            return jsonify([fixtures["v3_job"]])
        return jsonify([fixtures["classic_job"]])

    @app.route("/api/queue")
    def api_queue():
        return jsonify([])

    @app.route("/api/cluster/stats")
    def api_cluster_stats():
        return jsonify(fixtures["scheduler_status"])

    @app.route("/api/version")
    def api_version():
        return jsonify({"version": "v3.0.0-w12", "parallel_mode": "v3-enabled"})

    return app


class DashboardV3ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_test_app()
        cls.client = cls.app.test_client()

    def test_dashboard_template_serves_html(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Cluster-CI", resp.text)
        self.assertIn("v3DagModal", resp.text)
        self.assertIn("v3-log-filter-container", resp.text)

    def test_job_status_v3_structure(self):
        fixtures = load_fixtures()
        resp = self.client.get(f"/job_status/{fixtures['v3_job']['id']}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["parallel_mode"], 1)
        self.assertEqual(data["home_worker"], "alpha")
        self.assertIn("train_model", [n["name"] for n in data["nodes"]])

    def test_logs_endpoint_with_v3_prefixes(self):
        fixtures = load_fixtures()
        resp = self.client.get(f"/api/jobs/{fixtures['v3_job']['id']}/logs")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("[prep_data@alpha]", data["logs"])
        self.assertIn("[train_model@beta]", data["logs"])


if __name__ == "__main__":
    if "--serve" in sys.argv:
        port = 5055
        if "--port" in sys.argv:
            idx = sys.argv.index("--port")
            if idx + 1 < len(sys.argv):
                port = int(sys.argv[idx + 1])
        print(f"Starting Cluster-CI v3 mock dashboard server on http://127.0.0.1:{port} ...")
        app = create_test_app()
        app.run(host="127.0.0.1", port=port, debug=False)
    else:
        unittest.main()
