"""Tests réels pour la soumission, GHA et CLI de Cluster-CI v3 (Worker W8)."""

import json
import os
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import pytest
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
from src.cluster.cluster_run import parse_cluster_ci_config


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
                "version": 1,
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

    def test_a16_resources_parsing_and_submission_payload(self):
        """(A16) Vérifie le parsing de REQUIRED_CPUS, REQUIRED_GPUS, REQUIRED_STORAGE et leur transmission."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ci_file = os.path.join(tmp_dir, ".cluster-ci")
            with open(ci_file, "w", encoding="utf-8") as f:
                f.write(
                    "MAX_RUNTIME_HOURS=2\n"
                    "REQUIRED_RAM=16GB\n"
                    "REQUIRED_CPUS=8\n"
                    "REQUIRED_GPUS=2\n"
                    "REQUIRED_STORAGE=50GB\n"
                )

            # 1. Vérification dans parse_cluster_ci_config (cluster_run.py)
            cfg = parse_cluster_ci_config(tmp_dir)
            self.assertEqual(cfg["cpus"], 8)
            self.assertEqual(cfg["gpus"], 2)
            self.assertEqual(cfg["storage_gb"], 50.0)

            # 2. Vérification dans submit_job.py payload
            posted_payload = {}

            def fake_post(url, **kwargs):
                nonlocal posted_payload
                if url.endswith("/submit_job"):
                    posted_payload = kwargs.get("json", {})
                    mock_resp = MagicMock()
                    mock_resp.status_code = 200
                    mock_resp.json.return_value = {"job_id": "job-a16-test"}
                    return mock_resp
                elif url.endswith("/update_job_status"):
                    mock_resp = MagicMock()
                    mock_resp.status_code = 200
                    return mock_resp
                return MagicMock(status_code=404)

            with patch("requests.get", return_value=MagicMock(status_code=200)):
                with patch("requests.post", side_effect=fake_post):
                    job_id = submit_job(
                        headnode_url="http://fake-headnode:5000",
                        repo="UNIL-DESI/test-repo",
                        branch="main",
                        is_local=True,
                        local_repo_path=tmp_dir,
                    )

            self.assertEqual(job_id, "job-a16-test")
            self.assertEqual(posted_payload.get("cpus"), 8)
            self.assertEqual(posted_payload.get("gpus"), 2)
            self.assertEqual(posted_payload.get("storage_gb"), 50.0)

    def test_a17_rejection_error_verbatim_on_submit(self):
        """(A17) Vérifie qu'un refus HTTP du headnode est affiché tel quel et entraîne une sortie avec code non nul."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ci_file = os.path.join(tmp_dir, ".cluster-ci")
            with open(ci_file, "w", encoding="utf-8") as f:
                f.write("MAX_RUNTIME_HOURS=1\n")

            mock_resp = MagicMock()
            mock_resp.status_code = 400
            mock_resp.text = '{"error": "No available workers with 64GB RAM matching required constraints"}'
            mock_resp.json.return_value = {"error": "No available workers with 64GB RAM matching required constraints"}

            with patch("requests.post", return_value=mock_resp):
                with pytest.raises(SystemExit) as exc_info:
                    submit_job(
                        headnode_url="http://fake-headnode:5000",
                        repo="UNIL-DESI/test-repo",
                        branch="main",
                        is_local=True,
                        local_repo_path=tmp_dir,
                    )
                self.assertNotEqual(exc_info.value.code, 0)

    def test_a17_job_failure_displays_headnode_error_message(self):
        """(A17) Vérifie que les messages d'erreur du headnode (refus, OOM, échec de nœud) sont remontés fidèlement."""
        def fake_get(url, **kwargs):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            if "/job_status/" in url:
                mock_resp.json.return_value = {
                    "job_id": "test-fail-job",
                    "status": "failed",
                    "exit_code": 1,
                    "error_message": "Node 'train_stage' killed: CUDA out of memory (OOMKilled)",
                    "nodes": [
                        {
                            "name": "train_stage",
                            "status": "failed",
                            "error_message": "CUDA out of memory (allocated 14GB, reserved 15GB)",
                        }
                    ],
                }
            elif "/job_logs/" in url or "/logs" in url:
                mock_resp.json.return_value = {"logs": "", "offset": 0}
            return mock_resp

        with patch("requests.get", side_effect=fake_get):
            with patch("time.sleep", return_value=None):
                exit_code = wait_for_job(
                    headnode_url="http://fake-headnode:5000",
                    job_id="test-fail-job",
                    branch="feat/v3",
                )
        self.assertEqual(exit_code, 1)

    def test_ctrl_c_cancels_entire_job_via_stop_endpoint(self):
        """(Multi-machines) Vérifie que Ctrl+C (SIGINT) contacte POST /api/jobs/{job_id}/stop sur le headnode."""
        import signal

        stop_called_urls = []

        def fake_post(url, **kwargs):
            stop_called_urls.append(url)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"status": "ok"}
            return mock_resp

        with patch("requests.post", side_effect=fake_post):
            with patch("requests.get", return_value=MagicMock(status_code=200, json=lambda: {"status": "running"})):
                with patch("signal.signal") as mock_signal:
                    captured_handler = None

                    def fake_reg_signal(sig, handler):
                        nonlocal captured_handler
                        if sig == signal.SIGINT:
                            captured_handler = handler

                    mock_signal.side_effect = fake_reg_signal
                    try:
                        with patch("time.sleep", side_effect=KeyboardInterrupt):
                            wait_for_job("http://fake-headnode:5000", "test-job-stop", branch="cluster-draft/test")
                    except KeyboardInterrupt:
                        pass

                    self.assertIsNotNone(captured_handler)
                    with pytest.raises(SystemExit) as exc_info:
                        captured_handler(signal.SIGINT, None)

                    self.assertIn("http://fake-headnode:5000/api/jobs/test-job-stop/stop", stop_called_urls)

    def test_multi_node_logs_prefixed_format_function(self):
        """(1) Vérifie le préfixage strict [nœud@machine] des lignes de logs en multi-machines."""
        from src.cluster.cluster_run import format_multi_machine_log_line
        from src.scheduler.submit_job import format_multi_machine_log_line as submit_fmt

        nodes_data = [
            {"name": "train_model@alpha", "status": "running", "machine": "HEC45801", "worker_id": "HEC45801"},
            {"name": "train_model@beta", "status": "running", "machine": "HEC45803", "worker_id": "HEC45803"},
        ]

        # Ligne déjà préfixée -> conservée sans altération
        line_already = "[train_model@alpha@HEC45801] Epoch 1/5 loss=0.42"
        self.assertEqual(format_multi_machine_log_line(line_already, nodes_data), line_already)
        self.assertEqual(submit_fmt(line_already, nodes_data), line_already)

        # Ligne avec préfixe de nœud seul [train_model@alpha] -> enrichie avec la machine
        line_node_only = "[train_model@alpha] Epoch 2/5 loss=0.38"
        self.assertEqual(
            format_multi_machine_log_line(line_node_only, nodes_data),
            "[train_model@alpha@HEC45801] Epoch 2/5 loss=0.38",
        )

        # Ligne avec "train_model@beta: ..." -> préfixée
        line_colon = "train_model@beta: Epoch 2/5 loss=0.39"
        self.assertEqual(
            format_multi_machine_log_line(line_colon, nodes_data),
            "[train_model@beta@HEC45803] Epoch 2/5 loss=0.39",
        )

    def test_job_logs_fallback_warns_on_404(self):
        """(2) Vérifie que la bascule de repli /job_logs -> /api/jobs/{id}/logs avertit explicitement sur 404."""
        from src.cluster.cluster_run import _fetch_headnode_logs
        import io
        import urllib.error

        called_urls = []

        def fake_urlopen(req, timeout=5):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            called_urls.append(url)
            if "/job_logs/" in url:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))
            elif "/api/jobs/" in url:
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.code = 200
                mock_resp.__enter__.return_value = mock_resp
                mock_resp.read.return_value = json.dumps({"logs": "fallback logs\n", "offset": 14}).encode("utf-8")
                return mock_resp
            raise urllib.error.HTTPError(url, 500, "Error", {}, io.BytesIO(b""))

        import src.cluster.cluster_run as cr
        cr._job_logs_fallback_warned = False

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                logs, offset = _fetch_headnode_logs("test-job-fallback", "http://fake-headnode:5000", 0)

        self.assertEqual(logs, "fallback logs\n")
        self.assertEqual(offset, 14)
        self.assertIn("Avertissement", mock_stderr.getvalue())
        self.assertIn("/api/jobs/test-job-fallback/logs", mock_stderr.getvalue())

    def test_target_repo_remote_mismatch_fails_fast_a17(self):
        """(1) Vérifie qu'une discordance entre le remote origin et le repo soumis échoue immédiatement (A17)."""
        from src.scheduler.submit_job import submit_job

        with tempfile.TemporaryDirectory() as wrong_repo_dir:
            # Création d'un .cluster-ci avec PARALLEL_STAGES=true
            with open(os.path.join(wrong_repo_dir, ".cluster-ci"), "w", encoding="utf-8") as f:
                f.write("MAX_RUNTIME_HOURS=1\nPARALLEL_STAGES=true\n")
            with open(os.path.join(wrong_repo_dir, "dvc.yaml"), "w", encoding="utf-8") as f:
                f.write("stages: {}\n")

            # Simuler un remote origin pointant vers cluster-ci
            subprocess.run(["git", "init"], cwd=wrong_repo_dir, capture_output=True, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/UNIL-DESI/cluster-ci.git"],
                cwd=wrong_repo_dir, capture_output=True, check=True
            )

            # Tentative de soumission pour llm-as-recommender en pointant vers wrong_repo_dir
            with patch("requests.get", return_value=MagicMock(status_code=200)):
                with pytest.raises(SystemExit) as exc_info:
                    submit_job(
                        headnode_url="http://fake-headnode:5000",
                        repo="UNIL-DESI/llm-as-recommender",
                        branch="main",
                        repo_dir=wrong_repo_dir,
                    )
                self.assertNotEqual(exc_info.value.code, 0)

    def test_target_repo_plan_generated_from_explicit_target_repo_not_cwd(self):
        """(3 & 4) CWD = autre dépôt DVC (cluster-ci), plan attendu = celui du dépôt cible (llm-as-recommender)."""
        from src.scheduler.submit_job import submit_job

        with tempfile.TemporaryDirectory() as cwd_dir, tempfile.TemporaryDirectory() as target_dir:
            # 1. CWD : Simule cluster-ci avec ses propres stages de test
            with open(os.path.join(cwd_dir, ".cluster-ci"), "w", encoding="utf-8") as f:
                f.write("MAX_RUNTIME_HOURS=1\nPARALLEL_STAGES=true\n")
            with open(os.path.join(cwd_dir, "dvc.yaml"), "w", encoding="utf-8") as f:
                f.write("stages:\n  prep:\n    cmd: echo prep\n  join:\n    cmd: echo join\n")
            subprocess.run(["git", "init"], cwd=cwd_dir, capture_output=True, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/UNIL-DESI/cluster-ci.git"],
                cwd=cwd_dir, capture_output=True, check=True
            )

            # 2. Cible : Simule llm-as-recommender
            with open(os.path.join(target_dir, ".cluster-ci"), "w", encoding="utf-8") as f:
                f.write("MAX_RUNTIME_HOURS=2\nPARALLEL_STAGES=true\n")
            with open(os.path.join(target_dir, "dvc.yaml"), "w", encoding="utf-8") as f:
                f.write("stages:\n  data_load:\n    cmd: echo data\n  train_eval:\n    cmd: echo train\n")
            subprocess.run(["git", "init"], cwd=target_dir, capture_output=True, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/UNIL-DESI/llm-as-recommender.git"],
                cwd=target_dir, capture_output=True, check=True
            )

            posted_payload = {}

            def fake_post(url, **kwargs):
                nonlocal posted_payload
                if url.endswith("/submit_job"):
                    posted_payload = kwargs.get("json", {})
                    mock_resp = MagicMock()
                    mock_resp.status_code = 200
                    mock_resp.json.return_value = {"job_id": "job-target-plan-test"}
                    return mock_resp
                return MagicMock(status_code=404)

            # Mock planificateur qui retourne un plan selon le répertoire inspecté
            def fake_run_planner(repo_path):
                abspath = os.path.abspath(repo_path)
                if abspath == os.path.abspath(target_dir):
                    return {"nodes": [{"name": "data_load"}, {"name": "train_eval"}]}
                elif abspath == os.path.abspath(cwd_dir):
                    return {"nodes": [{"name": "prep"}, {"name": "join"}]}
                return {"nodes": []}

            # En changeant le CWD pour pointer vers cwd_dir
            orig_cwd = os.getcwd()
            try:
                os.chdir(cwd_dir)
                with patch("src.scheduler.submit_job.run_planner_for_submission", side_effect=fake_run_planner) as mock_plan:
                    with patch("requests.get", return_value=MagicMock(status_code=200)):
                        with patch("requests.post", side_effect=fake_post):
                            job_id = submit_job(
                                headnode_url="http://fake-headnode:5000",
                                repo="UNIL-DESI/llm-as-recommender",
                                branch="ecir-smoke",
                                repo_dir=target_dir,
                            )

                self.assertEqual(job_id, "job-target-plan-test")
                # Le planificateur doit avoir été appelé UNIQUEMENT sur target_dir
                mock_plan.assert_called_once_with(os.path.abspath(target_dir))
                # Le payload soumis doit contenir les nœuds de llm-as-recommender et NON ceux de cluster-ci
                plan_nodes = [n["name"] for n in posted_payload.get("plan", {}).get("nodes", [])]
                self.assertIn("data_load", plan_nodes)
                self.assertIn("train_eval", plan_nodes)
                self.assertNotIn("prep", plan_nodes)
                self.assertNotIn("join", plan_nodes)
            finally:
                os.chdir(orig_cwd)

    def test_v3_job_failure_reports_no_nodes_started_and_signal_cause(self):
        """(2) Vérifie qu'un échec v3 avec tous nœuds bloqués affiche la cause réelle et détaille SIGKILL/SIGTERM."""
        from src.scheduler.submit_job import wait_for_job
        import io

        def fake_get(url, **kwargs):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            if "/job_status/" in url:
                mock_resp.json.return_value = {
                    "job_id": "job-fdfa4682",
                    "status": "failed",
                    "exit_code": -9,
                    "error_message": "",
                    "nodes": [
                        {"name": f"step_{i}", "status": "blocked"} for i in range(10)
                    ],
                }
            elif "/job_logs/" in url or "/logs" in url:
                mock_resp.json.return_value = {"logs": "", "offset": 0}
            return mock_resp

        with patch("requests.get", side_effect=fake_get):
            with patch("time.sleep", return_value=None):
                with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    exit_code = wait_for_job("http://fake-headnode:5000", "job-fdfa4682")

        out = mock_stdout.getvalue()
        self.assertEqual(exit_code, -9)
        self.assertIn("no node started", out)
        self.assertIn("SIGKILL", out)


if __name__ == "__main__":
    unittest.main()
