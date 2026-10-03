"""
Tests unitaires et d'intégration pour BranchExecutor (Cluster-CI v3).

Couvre :
  - Séquence run -> switch_image -> run -> finish produit exactement 2 conteneurs
  - Remontée d'échec de nœud (status="failed", exit_code)
  - Détection et remontée de dépendances manquantes (status="missing_deps", Amendement A4)
  - Rapatriement réussi d'une dépendance via /fetch_artifact
  - Heartbeat régulier vers le headnode
  - Validation syntaxique bash -n sur run_research_pipeline.sh
  - Test d'intégration Docker (conditionnel si démon actif, sinon « non vérifié »)
"""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.runner.branch_executor import BranchExecutor, DockerRunner


# =====================================================================
# Mock Docker Runner
# =====================================================================

class MockDockerRunner(DockerRunner):
    """Simulateur Docker pour tester les cycles de vie des conteneurs sans démon."""

    def __init__(self, fail_nodes: Optional[Dict[str, int]] = None):
        super().__init__()
        self.containers_started: List[Dict[str, Any]] = []
        self.containers_stopped: List[str] = []
        self.containers_removed: List[str] = []
        self.volumes_created: List[str] = []
        self.exec_commands: List[Dict[str, Any]] = []
        self.fail_nodes = fail_nodes or {}
        self.streamed_lines: List[str] = []

    def create_volume(self, volume_name: str) -> int:
        self.volumes_created.append(volume_name)
        return 0

    def run_container(
        self,
        image: str,
        container_name: str,
        home_volume: str,
        repo_dir: str,
        base_dir: str,
        ram_limit: float = 10.0,
        vram_limit: float = 0.0,
        env: Optional[Dict[str, str]] = None,
        user_id: int = 1000,
        group_id: int = 1000,
    ) -> int:
        self.containers_started.append({
            "name": container_name,
            "image": image,
            "volume": home_volume,
            "repo_dir": repo_dir,
            "ram_limit": ram_limit,
            "vram_limit": vram_limit,
        })
        return 0

    def stop_container(self, container_name: str, timeout: int = 10) -> int:
        self.containers_stopped.append(container_name)
        return 0

    def remove_container(self, container_name: str, force: bool = True) -> int:
        self.containers_removed.append(container_name)
        return 0

    def exec_in_container(
        self,
        container_name: str,
        command: str,
        env: Optional[Dict[str, str]] = None,
        user: Optional[str] = None,
        stream_prefix: Optional[str] = None,
    ) -> tuple[int, str]:
        self.exec_commands.append({
            "container": container_name,
            "command": command,
            "env": env,
            "user": user,
            "prefix": stream_prefix,
        })

        # Vérifier si c'est une commande dvc repro -s <node>
        for node_name, exit_code in self.fail_nodes.items():
            if f"dvc repro -s {node_name}" in command:
                msg = f"Error executing stage {node_name}\n"
                if stream_prefix:
                    self.streamed_lines.append(f"{stream_prefix} {msg}")
                return exit_code, msg

        out = "Stage executed successfully.\n"
        if stream_prefix:
            self.streamed_lines.append(f"{stream_prefix} {out}")
        return 0, out


# =====================================================================
# Serveur Headnode Simulé
# =====================================================================

class SimulatedHeadnodeHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Silencieux pour ne pas polluer la sortie de test

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else ""
        data = json.loads(body) if body else {}

        if "/api/jobs/" in self.path and "/next_node" in self.path:
            self.server.next_node_calls.append(data)  # type: ignore[attr-defined]
            if self.server.next_node_responses:  # type: ignore[attr-defined]
                resp = self.server.next_node_responses.pop(0)  # type: ignore[attr-defined]
            else:
                resp = {"action": "finish"}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))

        elif "/api/jobs/" in self.path and "/runner_heartbeat" in self.path:
            self.server.heartbeats.append(data)  # type: ignore[attr-defined]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == "/list_workers":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            workers = getattr(self.server, "workers_list", [])
            self.wfile.write(json.dumps(workers).encode("utf-8"))

        elif self.path.startswith("/fetch_artifact/"):
            # Format: /fetch_artifact/<repo_owner>/<repo_name>/<path>
            # Match path against registered artifacts
            artifacts = getattr(self.server, "artifacts", {})
            found = False
            for art_path, content in artifacts.items():
                if self.path.endswith(f"/{art_path}"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers()
                    self.wfile.write(content)
                    found = True
                    break
            if not found:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


class SimulatedHeadnode:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SimulatedHeadnodeHandler)
        self.port = self.server.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        self.server.next_node_calls = []  # type: ignore[attr-defined]
        self.server.next_node_responses = []  # type: ignore[attr-defined]
        self.server.heartbeats = []  # type: ignore[attr-defined]
        self.server.workers_list = []  # type: ignore[attr-defined]
        self.server.artifacts = {}  # type: ignore[attr-defined]
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# =====================================================================
# Tests Unitaires & d'Intégration
# =====================================================================

class TestBranchExecutor(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.headnode = SimulatedHeadnode()
        self.headnode.start()

    def tearDown(self):
        self.headnode.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_run_switch_run_finish_produces_exactly_two_containers(self):
        """
        Vérifie qu'une séquence run (img1) -> switch_image (img2) -> run (img2) -> finish
        produit EXACTEMENT 2 conteneurs (réutilisation du second conteneur pour le 3e nœud).
        """
        self.headnode.server.next_node_responses = [  # type: ignore[attr-defined]
            {
                "action": "run",
                "node": "node_prep",
                "image": "python:3.11-slim",
                "resources": {"ram_gb": 4},
                "gpu_ids": [],
            },
            {
                "action": "switch_image",
                "node": "node_train",
                "image": "python:3.12-slim",
                "resources": {"ram_gb": 8},
                "gpu_ids": [0],
            },
            {
                "action": "run",
                "node": "node_eval",
                "image": "python:3.12-slim",
                "resources": {"ram_gb": 8},
                "gpu_ids": [0],
            },
            {
                "action": "finish",
            },
        ]

        mock_docker = MockDockerRunner()
        executor = BranchExecutor(
            headnode_url=self.headnode.url,
            job_id="job-test-sequence",
            runner_id="runner-1",
            worker_id="worker-node-1",
            repo_dir=self.temp_dir,
            target_repo="hjamet/llm-as-recommender",
            target_branch="main",
            docker=mock_docker,
            poll_interval=0.01,
            heartbeat_interval=0.05,
        )

        with patch("src.runner.branch_executor.sync_before_node", return_value=True), \
             patch("src.runner.branch_executor.commit_and_push_node", return_value=True):
            exit_code = executor.run()

        self.assertEqual(exit_code, 0)

        # Vérification clé : EXACTEMENT 2 conteneurs produits
        self.assertEqual(len(mock_docker.containers_started), 2)
        self.assertEqual(mock_docker.containers_started[0]["image"], "python:3.11-slim")
        self.assertEqual(mock_docker.containers_started[1]["image"], "python:3.12-slim")

        # Vérification des volumes dédiés par image
        vol1 = mock_docker.containers_started[0]["volume"]
        vol2 = mock_docker.containers_started[1]["volume"]
        self.assertIn("python-3.11-slim", vol1)
        self.assertIn("python-3.12-slim", vol2)
        self.assertNotEqual(vol1, vol2)

        # Vérification des appels next_node
        calls = self.headnode.server.next_node_calls  # type: ignore[attr-defined]
        self.assertEqual(len(calls), 4)

        # Appel 1: démarrage (node=None, status=None)
        self.assertIsNone(calls[0]["node"])
        self.assertIsNone(calls[0]["status"])

        # Appel 2: node_prep terminé avec succès
        self.assertEqual(calls[1]["node"], "node_prep")
        self.assertEqual(calls[1]["status"], "done")
        self.assertEqual(calls[1]["exit_code"], 0)

        # Appel 3: node_train terminé avec succès
        self.assertEqual(calls[2]["node"], "node_train")
        self.assertEqual(calls[2]["status"], "done")
        self.assertEqual(calls[2]["exit_code"], 0)

        # Appel 4: node_eval terminé avec succès
        self.assertEqual(calls[3]["node"], "node_eval")
        self.assertEqual(calls[3]["status"], "done")
        self.assertEqual(calls[3]["exit_code"], 0)

        # Vérification des logs préfixés [node@machine]
        self.assertTrue(any("[node_prep@worker-node-1]" in line for line in mock_docker.streamed_lines))
        self.assertTrue(any("[node_train@worker-node-1]" in line for line in mock_docker.streamed_lines))
        self.assertTrue(any("[node_eval@worker-node-1]" in line for line in mock_docker.streamed_lines))

    def test_executor_node_failure_reported(self):
        """Vérifie qu'un code d'erreur lors d'un nœud est fidèlement remonté au headnode."""
        self.headnode.server.next_node_responses = [  # type: ignore[attr-defined]
            {
                "action": "run",
                "node": "failing_stage",
                "image": "python:3.11-slim",
                "resources": {},
                "gpu_ids": [0],
            },
            {
                "action": "finish",
            },
        ]

        mock_docker = MockDockerRunner(fail_nodes={"failing_stage": 42})
        executor = BranchExecutor(
            headnode_url=self.headnode.url,
            job_id="job-fail",
            runner_id="runner-fail",
            worker_id="worker-fail",
            repo_dir=self.temp_dir,
            target_repo="test/repo",
            target_branch="main",
            docker=mock_docker,
            poll_interval=0.01,
            heartbeat_interval=0.05,
        )

        with patch("src.runner.branch_executor.sync_before_node", return_value=True), \
             patch("src.runner.branch_executor.commit_and_push_node", return_value=True):
            executor.run()

        calls = self.headnode.server.next_node_calls  # type: ignore[attr-defined]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["node"], "failing_stage")
        self.assertEqual(calls[1]["status"], "failed")
        self.assertEqual(calls[1]["exit_code"], 42)
        self.assertIn("failed with exit code 42", calls[1]["error_message"])

    def test_executor_missing_deps_reported_amendment_a4(self):
        """
        Vérifie qu'en cas de dépendance lourde introuvable (Amendement A4),
        le statut 'missing_deps' est renvoyé avec la liste exacte sans exécuter dvc repro.
        """
        self.headnode.server.next_node_responses = [  # type: ignore[attr-defined]
            {
                "action": "run",
                "node": "calc_stage",
                "image": "python:3.11-slim",
                "dep_paths": ["data/unobtainable_artifact.parquet"],
            },
            {
                "action": "finish",
            },
        ]

        mock_docker = MockDockerRunner()
        executor = BranchExecutor(
            headnode_url=self.headnode.url,
            job_id="job-missing-deps",
            runner_id="runner-md",
            worker_id="worker-md",
            repo_dir=self.temp_dir,
            target_repo="test/repo",
            target_branch="main",
            docker=mock_docker,
            poll_interval=0.01,
            heartbeat_interval=0.05,
        )

        with patch("src.runner.branch_executor.sync_before_node", return_value=True), \
             patch("src.runner.branch_executor.commit_and_push_node", return_value=True):
            executor.run()

        calls = self.headnode.server.next_node_calls  # type: ignore[attr-defined]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["node"], "calc_stage")
        self.assertEqual(calls[1]["status"], "missing_deps")
        self.assertEqual(calls[1]["missing_deps"], ["data/unobtainable_artifact.parquet"])
        self.assertEqual(calls[1]["exit_code"], 1)

        # dvc repro ne doit PAS avoir été exécuté
        dvc_commands = [c["command"] for c in mock_docker.exec_commands if "dvc repro" in c["command"]]
        self.assertEqual(len(dvc_commands), 0)

    def test_executor_fetches_missing_dep_from_worker(self):
        """Vérifie le rapatriement P2P automatique d'une dépendance présente sur un pair."""
        rel_path = "data/features.parquet"
        file_bytes = b"PARQUET_MAGIC_BYTES_12345"

        # Simuler l'artefact sur le serveur headnode / worker
        self.headnode.server.artifacts[rel_path] = file_bytes  # type: ignore[attr-defined]
        self.headnode.server.workers_list = [  # type: ignore[attr-defined]
            {"worker_id": "W2", "service_url": self.headnode.url}
        ]

        self.headnode.server.next_node_responses = [  # type: ignore[attr-defined]
            {
                "action": "run",
                "node": "train_model",
                "image": "python:3.11-slim",
                "dep_paths": [rel_path],
                "resources": {"workers": [self.headnode.url]},
            },
            {
                "action": "finish",
            },
        ]

        mock_docker = MockDockerRunner()
        executor = BranchExecutor(
            headnode_url=self.headnode.url,
            job_id="job-fetch-ok",
            runner_id="runner-fok",
            worker_id="worker-fok",
            repo_dir=self.temp_dir,
            target_repo="test/repo",
            target_branch="main",
            docker=mock_docker,
            poll_interval=0.01,
            heartbeat_interval=0.05,
        )

        with patch("src.runner.branch_executor.sync_before_node", return_value=True), \
             patch("src.runner.branch_executor.commit_and_push_node", return_value=True):
            executor.run()

        # Le fichier a bien été écrit localement dans le workspace
        dest_file = os.path.join(self.temp_dir, rel_path)
        self.assertTrue(os.path.exists(dest_file))
        with open(dest_file, "rb") as f:
            self.assertEqual(f.read(), file_bytes)

        # Le nœud a pu s'exécuter et remonter "done"
        calls = self.headnode.server.next_node_calls  # type: ignore[attr-defined]
        self.assertEqual(calls[1]["node"], "train_model")
        self.assertEqual(calls[1]["status"], "done")

    def test_executor_heartbeat_sent_periodically(self):
        """Vérifie que le thread de heartbeat émet régulièrement vers /api/jobs/<id>/runner_heartbeat."""
        self.headnode.server.next_node_responses = [  # type: ignore[attr-defined]
            {"action": "wait"},
            {"action": "wait"},
            {"action": "finish"},
        ]

        mock_docker = MockDockerRunner()
        executor = BranchExecutor(
            headnode_url=self.headnode.url,
            job_id="job-hb",
            runner_id="runner-hb-1",
            worker_id="worker-hb-1",
            repo_dir=self.temp_dir,
            target_repo="test/repo",
            target_branch="main",
            docker=mock_docker,
            poll_interval=0.04,
            heartbeat_interval=0.02,
        )

        executor.run()

        heartbeats = self.headnode.server.heartbeats  # type: ignore[attr-defined]
        self.assertGreaterEqual(len(heartbeats), 2)
        self.assertEqual(heartbeats[0]["runner_id"], "runner-hb-1")
        self.assertEqual(heartbeats[0]["worker"], "worker-hb-1")

    def test_bash_n_on_pipeline_script(self):
        """Vérifie la syntaxe bash stricte de src/runner/run_research_pipeline.sh."""
        repo_root = Path(__file__).resolve().parents[2]
        rel_script_path = "src/runner/run_research_pipeline.sh"
        self.assertTrue((repo_root / rel_script_path).exists())

        res = subprocess.run(
            ["bash", "-n", rel_script_path],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            res.returncode,
            0,
            f"Erreur de syntaxe bash dans {rel_script_path}:\n{res.stderr}",
        )

    def test_docker_integration_if_daemon_available(self):
        """
        Test d'intégration Docker si le démon est disponible localement.
        Si le démon n'est pas actif (Docker Desktop arrêté), le test est ignoré
        et consigné comme « non vérifié » conformément au contrat.
        """
        check_docker = subprocess.run(["docker", "info"], capture_output=True)
        if check_docker.returncode != 0:
            self.skipTest("Démon Docker indisponible localement ('non vérifié' selon contrat W2).")

        # Ce bloc ne s'exécute que si Docker est réellement actif sur la machine
        # (ex. CI linux ou machine avec Docker démarré)
        runner = DockerRunner()
        res = runner.run_container(
            image="python:3.12-slim",
            container_name=f"test-ci-integration-{os.getpid()}",
            home_volume=f"test-ci-vol-{os.getpid()}",
            repo_dir=self.temp_dir,
            base_dir=str(Path(__file__).resolve().parents[2]),
        )
        self.assertEqual(res, 0)
        runner.stop_container(f"test-ci-integration-{os.getpid()}")
        runner.remove_container(f"test-ci-integration-{os.getpid()}")


if __name__ == "__main__":
    unittest.main()
