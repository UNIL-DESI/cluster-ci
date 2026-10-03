"""Tests réels pour la soumission, GHA et CLI de Cluster-CI v3 (Worker W8)."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import pytest
import io
import shutil
import threading
import zipfile
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
import yaml

from src.config.defaults import DEFAULT_RESOURCES
from src.scheduler.submit_job import (
    DEFAULT_RAM_GB,
    get_ram_requirement,
    get_planner_module_name,
    run_planner_for_submission,
    format_nodes_status_summary,
    print_final_dag_summary,
    submit_job,
    wait_for_job,
)
from src.cluster.cluster_run import (
    parse_cluster_ci_config,
    fetch_local_results,
    stream_local_job_logs_and_wait,
    _headnode_stop_job,
)


class TestV3W8Submission(unittest.TestCase):
    """Banc de tests unitaire et d'intégration simulé pour W8."""

    def test_default_ram_10gb_single_source_of_truth(self):
        """(1) Vérifie que la RAM par défaut est de 10 Go issue du module unique defaults.py."""
        # 1. Vérification de la source de vérité
        self.assertEqual(DEFAULT_RAM_GB, 10.0)
        self.assertEqual(DEFAULT_RESOURCES["ram_gb"], 10)

        # 2. Vérification dans submit_job.py get_ram_requirement
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Sans .cluster-ci
            ram_none = get_ram_requirement(is_local=True, local_repo_path=tmp_dir)
            self.assertEqual(ram_none, 10.0)

            # Avec .cluster-ci sans REQUIRED_RAM
            with open(os.path.join(tmp_dir, ".cluster-ci"), "w", encoding="utf-8") as f:
                f.write("MAX_RUNTIME_HOURS=2\n")
            ram_default = get_ram_requirement(is_local=True, local_repo_path=tmp_dir)
            self.assertEqual(ram_default, 10.0)

            # Avec REQUIRED_RAM explicite (32GB)
            with open(os.path.join(tmp_dir, ".cluster-ci"), "w", encoding="utf-8") as f:
                f.write("REQUIRED_RAM=32GB\nMAX_RUNTIME_HOURS=2\n")
            ram_explicit = get_ram_requirement(is_local=True, local_repo_path=tmp_dir)
            self.assertEqual(ram_explicit, 32.0)

        # 3. Vérification dans cluster_run.py parse_cluster_ci_config
        with tempfile.TemporaryDirectory() as tmp_dir:
            with open(os.path.join(tmp_dir, ".cluster-ci"), "w", encoding="utf-8") as f:
                f.write("MAX_RUNTIME_HOURS=1\n")
            cfg = parse_cluster_ci_config(tmp_dir)
            self.assertEqual(cfg["ram_required_gb"], 10.0)

    def test_submit_payload_without_parallel_stages_strictly_identical(self):
        """(2a) Sans PARALLEL_STAGES, la charge utile POST /submit_job est strictement identique à origin/main."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Fichier .cluster-ci classique (sans PARALLEL_STAGES)
            ci_file = os.path.join(tmp_dir, ".cluster-ci")
            with open(ci_file, "w", encoding="utf-8") as f:
                f.write("MAX_RUNTIME_HOURS=4\nREQUIRED_RAM=12GB\nREQUIRED_VRAM=0GB\n")

            posted_payload = {}

            def fake_post(url, **kwargs):
                nonlocal posted_payload
                if url.endswith("/submit_job"):
                    posted_payload = kwargs.get("json", {})
                    mock_resp = MagicMock()
                    mock_resp.status_code = 200
                    mock_resp.json.return_value = {"job_id": "test-job-classic-123"}
                    return mock_resp
                elif url.endswith("/update_job_status"):
                    mock_resp = MagicMock()
                    mock_resp.status_code = 200
                    return mock_resp
                return MagicMock(status_code=404)

            mock_get = MagicMock()
            mock_get.status_code = 200

            with patch("requests.get", return_value=mock_get):
                with patch("requests.post", side_effect=fake_post):
                    job_id = submit_job(
                        headnode_url="http://fake-headnode:5000",
                        repo="UNIL-DESI/test-repo",
                        branch="main",
                        is_local=True,
                        local_repo_path=tmp_dir,
                    )

            self.assertEqual(job_id, "test-job-classic-123")
            # Invariant strict : absence absolue du champ 'plan'
            self.assertNotIn("plan", posted_payload)

            # Clés exactes origin/main
            expected_keys = {
                "repo",
                "branch",
                "commit_hash",
                "ram_required_gb",
                "vram_required_gb",
                "max_runtime_hours",
                "exposed_port",
                "custom_web_app",
                "allowed_workers",
                "gh_run_id",
                "gh_token",
                "env_vars",
                "username",
                "is_local",
                "local_repo_path",
            }
            self.assertEqual(set(posted_payload.keys()), expected_keys)
            self.assertEqual(posted_payload["ram_required_gb"], 12.0)
            self.assertEqual(posted_payload["max_runtime_hours"], 4.0)

    def test_submit_payload_with_parallel_stages_includes_plan(self):
        """(2b) Avec PARALLEL_STAGES=true et dvc.yaml, le champ plan est présent dans POST /submit_job."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            # .cluster-ci avec PARALLEL_STAGES
            with open(os.path.join(tmp_dir, ".cluster-ci"), "w", encoding="utf-8") as f:
                f.write("PARALLEL_STAGES=true\nMAX_RUNTIME_HOURS=2\n")

            # dvc.yaml présent
            with open(os.path.join(tmp_dir, "dvc.yaml"), "w", encoding="utf-8") as f:
                f.write("stages:\n  step1:\n    cmd: echo 1\n")

            fake_plan = {
                "version": "3.0",
                "defaults": DEFAULT_RESOURCES,
                "nodes": [
                    {
                        "name": "step1",
                        "deps": [],
                        "stale": True,
                        "priority": 1.0,
                        "resources": {"cpus": 4, "ram_gb": 10},
                    }
                ],
            }

            posted_payload = {}

            def fake_post(url, **kwargs):
                nonlocal posted_payload
                if url.endswith("/submit_job"):
                    posted_payload = kwargs.get("json", {})
                    mock_resp = MagicMock()
                    mock_resp.status_code = 200
                    mock_resp.json.return_value = {"job_id": "test-job-parallel-456"}
                    return mock_resp
                elif url.endswith("/update_job_status"):
                    mock_resp = MagicMock()
                    mock_resp.status_code = 200
                    return mock_resp
                return MagicMock(status_code=404)

            mock_get = MagicMock()
            mock_get.status_code = 200

            with patch("requests.get", return_value=mock_get):
                with patch("src.scheduler.submit_job.run_planner_for_submission", return_value=fake_plan):
                    with patch("requests.post", side_effect=fake_post):
                        job_id = submit_job(
                            headnode_url="http://fake-headnode:5000",
                            repo="UNIL-DESI/test-repo",
                            branch="feat/v3",
                            is_local=True,
                            local_repo_path=tmp_dir,
                        )

            self.assertEqual(job_id, "test-job-parallel-456")
            self.assertIn("plan", posted_payload)
            self.assertEqual(posted_payload["plan"], fake_plan)

    def test_planner_error_fails_submission_without_fallback(self):
        """(2c) En cas d'erreur du planificateur (ex: clé inconnue dans meta.cluster), la soumission échoue (code != 0)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            with open(os.path.join(tmp_dir, ".cluster-ci"), "w", encoding="utf-8") as f:
                f.write("PARALLEL_STAGES=true\nMAX_RUNTIME_HOURS=1\n")
            with open(os.path.join(tmp_dir, "dvc.yaml"), "w", encoding="utf-8") as f:
                f.write("stages:\n  invalid:\n    cmd: echo bad\n")

            # Simuler un échec du planificateur CLI
            mock_proc = MagicMock()
            mock_proc.returncode = 1
            mock_proc.stderr = "Error: Stage 'invalid': unknown key under meta.cluster: 'unknown_foo'"
            mock_proc.stdout = ""

            with patch("subprocess.run", return_value=mock_proc):
                # run_planner_for_submission doit lever SystemExit avec le code d'erreur
                with pytest.raises(SystemExit) as exc_info:
                    run_planner_for_submission(tmp_dir)

                self.assertEqual(exc_info.value.code, 1)

    def test_multi_node_status_summary_formatting(self):
        """(3a) Vérifie le calcul exact et le formatage du résumé d'état par nœud (done/running/ready/failed/blocked)."""
        nodes_input = [
            {"name": "prep_data", "status": "done", "worker_id": "HEC45801"},
            {"name": "train_model@alpha", "status": "running", "worker_id": "HEC45801"},
            {"name": "train_model@beta", "status": "failed", "worker_id": "HEC45803"},
            {"name": "evaluate@alpha", "status": "ready", "worker_id": None},
            {"name": "evaluate@beta", "status": "blocked", "worker_id": None},
            {"name": "cache_clean", "status": "skipped", "worker_id": None},
        ]

        summary_line, details = format_nodes_status_summary(nodes_input)

        # Vérification des compteurs
        self.assertIn("done=1", summary_line)
        self.assertIn("running=1", summary_line)
        self.assertIn("ready=1", summary_line)
        self.assertIn("failed=1", summary_line)
        self.assertIn("blocked=1", summary_line)
        self.assertIn("skipped=1", summary_line)

        # Vérification des détails
        details_str = " ".join(details)
        self.assertIn("train_model@alpha@HEC45801", details_str)
        self.assertIn("train_model@beta", details_str)
        self.assertIn("evaluate@beta", details_str)

    def test_multi_node_logs_prefixed_and_final_summary_in_wait_for_job(self):
        """(3b) Vérifie l'affichage des logs agrégés préfixés [node@machine] et du statut consolidé."""
        log_stream_lines = (
            "[prep_data@HEC45801] Loading dataset chunks...\n"
            "[train_model@alpha@HEC45801] Epoch 1/5 loss=0.42\n"
            "[train_model@beta@HEC45803] Epoch 1/5 loss=0.45\n"
        )

        poll_count = 0

        def fake_get(url, **kwargs):
            nonlocal poll_count
            poll_count += 1
            mock_resp = MagicMock()
            mock_resp.status_code = 200

            if "/job_status/" in url:
                if poll_count == 1:
                    mock_resp.json.return_value = {
                        "job_id": "test-v3-job",
                        "status": "running",
                        "worker_service_url": "http://worker1:5001",
                        "nodes": [
                            {"name": "prep_data", "status": "done", "worker_id": "HEC45801"},
                            {"name": "train_model@alpha", "status": "running", "worker_id": "HEC45801"},
                        ],
                    }
                else:
                    mock_resp.json.return_value = {
                        "job_id": "test-v3-job",
                        "status": "completed",
                        "exit_code": 0,
                        "worker_service_url": "http://worker1:5001",
                        "nodes": [
                            {"name": "prep_data", "status": "done", "worker_id": "HEC45801"},
                            {"name": "train_model@alpha", "status": "done", "worker_id": "HEC45801"},
                        ],
                    }
                return mock_resp

            elif "/job_logs/" in url or "/logs" in url:
                mock_resp.json.return_value = {
                    "logs": log_stream_lines if poll_count == 1 else "",
                    "offset": len(log_stream_lines),
                }
                return mock_resp

            return MagicMock(status_code=404)

        with patch("requests.get", side_effect=fake_get):
            with patch("time.sleep", return_value=None):
                exit_code = wait_for_job(
                    headnode_url="http://fake-headnode:5000",
                    job_id="test-v3-job",
                    branch="feat/v3",
                )

        self.assertEqual(exit_code, 0)

    def test_workflow_yaml_validity_and_uv_setup(self):
        """(3c) Vérifie la validité du fichier .github/workflows/cluster-ci.yml et de l'installation uv."""
        workflow_path = os.path.join(
            os.path.dirname(__file__), "..", ".github", "workflows", "cluster-ci.yml"
        )
        self.assertTrue(os.path.isfile(workflow_path))

        with open(workflow_path, "r", encoding="utf-8") as f:
            wf_data = yaml.safe_load(f)

        self.assertIn("jobs", wf_data)
        self.assertIn("execute-on-cluster", wf_data["jobs"])

        steps = wf_data["jobs"]["execute-on-cluster"]["steps"]
        step_names = [s.get("name") for s in steps]

        # Vérification présence des étapes clés
        self.assertIn("Checkout Code", step_names)
        self.assertIn("Setup uv for Parallel Planner", step_names)
        self.assertIn("Run Orchestrator", step_names)

        # Vérification que le setup uv vérifie PARALLEL_STAGES sans alourdir le chemin classique
        uv_step = next(s for s in steps if s.get("name") == "Setup uv for Parallel Planner")
        run_script = uv_step.get("run", "")
        self.assertIn("PARALLEL_STAGES", run_script)
        self.assertIn("uv", run_script)

    def test_fetch_local_results_with_shutil_and_real_http_server(self):
        """(Correction 1) Vérifie que fetch_local_results utilise shutil.copyfileobj sans NameError via vrai serveur HTTP."""
        server = HTTPServer(("127.0.0.1", 0), MockHeadnodeHandler)
        server.endpoints_called = []
        port = server.server_port
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        try:
            headnode_url = f"http://127.0.0.1:{port}"
            with tempfile.TemporaryDirectory() as tmp_dir:
                old_cwd = os.getcwd()
                os.chdir(tmp_dir)
                try:
                    success = fetch_local_results("test-shutil-job", headnode_url)
                    self.assertTrue(success)
                    # Vérifier que le fichier archivé a été extrait avec succès
                    self.assertTrue(os.path.isfile(os.path.join(tmp_dir, "metrics.json")))
                    with open(os.path.join(tmp_dir, "metrics.json"), "r") as f:
                        data = json.load(f)
                    self.assertEqual(data.get("accuracy"), 0.95)
                finally:
                    os.chdir(old_cwd)
        finally:
            server.shutdown()

    def test_stream_local_job_logs_parallel_mode_real_http_server(self):
        """(Correction 2a) Vérifie que stream_local_job_logs_and_wait en mode parallèle interroge /job_logs et affiche le résumé nœuds."""
        server = HTTPServer(("127.0.0.1", 0), MockHeadnodeHandler)
        server.endpoints_called = []
        port = server.server_port
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        try:
            headnode_url = f"http://127.0.0.1:{port}"
            with patch("time.sleep", return_value=None):
                code = stream_local_job_logs_and_wait("test-parallel-job", headnode_url, is_parallel=True)
            self.assertEqual(code, 0)
            self.assertIn("job_logs", server.endpoints_called)
            self.assertIn("job_status", server.endpoints_called)
        finally:
            server.shutdown()

    def test_stream_local_job_logs_classic_mode_real_http_server(self):
        """(Correction 2b) Vérifie que le mode classique interroge l'ancien /api/jobs/<id>/logs sans altération."""
        server = HTTPServer(("127.0.0.1", 0), MockHeadnodeHandler)
        server.endpoints_called = []
        port = server.server_port
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        try:
            headnode_url = f"http://127.0.0.1:{port}"
            with patch("time.sleep", return_value=None):
                code = stream_local_job_logs_and_wait("test-classic-job", headnode_url, is_parallel=False)
            self.assertEqual(code, 0)
            self.assertIn("classic_logs", server.endpoints_called)
        finally:
            server.shutdown()

    def test_stream_local_job_logs_ctrl_c_cancels_job_on_headnode(self):
        """(Correction 2c) Vérifie que l'interruption Ctrl+C annule le job complet via _headnode_stop_job (POST /api/jobs/<id>/stop)."""
        server = HTTPServer(("127.0.0.1", 0), MockHeadnodeHandler)
        server.endpoints_called = []
        port = server.server_port
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        try:
            server.job_status = "running"
            headnode_url = f"http://127.0.0.1:{port}"
            with patch("time.sleep", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    stream_local_job_logs_and_wait("test-interrupt-job", headnode_url, is_parallel=True)
            self.assertIn("stop_job", server.endpoints_called)
        finally:
            server.shutdown()

    def test_planner_module_strict_w1_no_fallback(self):
        """(Correction 3) Vérifie que le planificateur pointe strictement vers src.planner.stage_plan sans repli."""
        mod = get_planner_module_name()
        self.assertEqual(mod, "src.planner.stage_plan")

    def test_wait_for_job_bounded_retries_on_network_errors(self):
        """(Correction 4) Vérifie que submit_job gère les erreurs réseau par retries bornés avec message et fait remonter les autres."""
        # 1. Erreur transitoire de connexion sur la récupération des logs
        attempt_count = 0

        def flaky_get(url, **kwargs):
            nonlocal attempt_count
            mock_resp = MagicMock()
            if "/job_status/" in url:
                mock_resp.status_code = 200
                mock_resp.json.return_value = {"job_id": "flaky-job", "status": "completed", "exit_code": 0}
                return mock_resp
            elif "/job_logs/" in url or "/logs" in url:
                attempt_count += 1
                raise requests.exceptions.ConnectionError("Temporary network reset")
            return MagicMock(status_code=404)

        with patch("requests.get", side_effect=flaky_get):
            with patch("time.sleep", return_value=None):
                code = wait_for_job("http://fake:5000", "flaky-job")
                self.assertEqual(code, 0)
                self.assertGreater(attempt_count, 0)

        # 2. Erreur persistante dépassant le seuil max_log_errors -> lève l'exception
        persistent_count = 0

        def persistent_error_get(url, **kwargs):
            nonlocal persistent_count
            mock_resp = MagicMock()
            if "/job_status/" in url:
                mock_resp.status_code = 200
                mock_resp.json.return_value = {"job_id": "fail-job", "status": "running"}
                return mock_resp
            elif "/job_logs/" in url or "/logs" in url:
                persistent_count += 1
                raise requests.exceptions.ConnectionError("Permanent network dead")
            return MagicMock(status_code=404)

        with patch("requests.get", side_effect=persistent_error_get):
            with patch("time.sleep", return_value=None):
                with self.assertRaises(requests.exceptions.RequestException):
                    wait_for_job("http://fake:5000", "fail-job")
                self.assertGreaterEqual(persistent_count, 10)


class MockHeadnodeHandler(BaseHTTPRequestHandler):
    """Serveur HTTP simulant les routes du Headnode en local."""

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = self.path
        if "/api/jobs/" in parsed and "/results" in parsed:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("metrics.json", '{"accuracy": 0.95}')
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif "/job_logs/" in parsed:
            self.server.endpoints_called.append("job_logs")
            data = json.dumps({"logs": "[stage1@HEC45801] test log line\n", "offset": 32}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
        elif "/api/jobs/" in parsed and "/logs" in parsed:
            self.server.endpoints_called.append("classic_logs")
            data = json.dumps({"logs": "classic log line\n", "offset": 17}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
        elif "/job_status/" in parsed:
            self.server.endpoints_called.append("job_status")
            status_val = getattr(self.server, "job_status", "completed")
            status_data = {
                "job_id": "test-job-id",
                "status": status_val,
                "exit_code": 0,
                "nodes": [
                    {"name": "step1", "status": "done", "worker_id": "HEC45801"},
                    {"name": "step2", "status": "missing_deps", "worker_id": None},
                ],
            }
            data = json.dumps(status_data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = self.path
        if "/api/jobs/" in parsed and "/stop" in parsed:
            self.server.endpoints_called.append("stop_job")
            data = json.dumps({"status": "ok", "message": "Job stopped"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    unittest.main()
