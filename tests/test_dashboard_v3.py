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
from flask import Flask, jsonify, request, Response, render_template

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = BASE_DIR / "src" / "scheduler" / "templates"
STATIC_DIR = BASE_DIR / "src" / "scheduler" / "static"
FIXTURE_PATH = BASE_DIR / "tests" / "fixtures" / "v3_job_fixture.json"


def load_fixtures():
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def create_test_app():
    fixtures = load_fixtures()
    app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=str(STATIC_DIR), static_url_path="/static")

    @app.route("/")
    def index():
        return render_template("dashboard.html", user={"login": "hjamet"})

    @app.route("/logout")
    def logout():
        return ("", 200)

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
                "active_runs": [fixtures["classic_job_concurrent"]],
                "last_run": fixtures["classic_job"]
            }
        ])

    @app.route("/api/runs/active")
    def api_runs_active():
        # Returns both v3 DAG job and concurrent classic job (testing A11 multi-executor packing on beta)
        return jsonify([fixtures["v3_job"], fixtures["classic_job_concurrent"]])

    @app.route("/api/runs/history")
    def api_runs_history():
        return jsonify({
            "items": [fixtures["v3_job"], fixtures["classic_job_concurrent"], fixtures["classic_job"]],
            "total": 3,
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
            "running_count": 2,
            "cluster_utilization": fixtures["scheduler_status"]["cluster_utilization"]
        })

    @app.route("/workers")
    def workers_endpoint():
        return jsonify(fixtures["scheduler_status"]["active_workers"])

    @app.route("/job_status/<job_id>")
    def job_status(job_id):
        if "v3" in job_id or job_id == fixtures["v3_job"]["id"]:
            return jsonify(fixtures["v3_job"])
        elif "concurrent" in job_id or job_id == fixtures["classic_job_concurrent"]["id"]:
            return jsonify(fixtures["classic_job_concurrent"])
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
        return jsonify([fixtures["classic_job_concurrent"], fixtures["classic_job"]])

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
        self.assertIn("<style>", resp.text)
        self.assertIn("ansiToHtml", resp.text)
        self.assertIn("getActiveRuns", resp.text)
        self.assertIn("Alerte Disque (&gt;85%)", resp.text)
        self.assertNotIn("Packing A11", resp.text)
        self.assertNotIn("CPUs Admis (A11)", resp.text)
        self.assertIn("hjamet", resp.text)
        self.assertNotIn("{{ user", resp.text)
        self.assertIn("CLUSTER MACHINES", resp.text)
        self.assertIn("Active Cluster Runs", resp.text)
        self.assertIn("badge-spec-pill", resp.text)
        self.assertIn("v3-modal-section-title", resp.text)


    def test_local_mermaid_static_serving(self):
        resp = self.client.get("/static/mermaid.min.js")
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(len(resp.data), 1_000_000)
        resp.close()

    def test_job_status_v3_structure(self):
        fixtures = load_fixtures()
        resp = self.client.get(f"/job_status/{fixtures['v3_job']['id']}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["parallel_mode"], 1)
        self.assertEqual(data["home_worker"], "alpha")
        self.assertIn("train_model", [n["name"] for n in data["nodes"]])

    def test_a11_a13_fixture_structure(self):
        fixtures = load_fixtures()
        workers = fixtures["scheduler_status"]["active_workers"]
        beta = next(w for w in workers if w["id"] == "beta")
        self.assertEqual(beta["unified_memory"], 1)
        self.assertIn("docker_images", beta)
        self.assertIn("pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime", beta["docker_images"])

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
