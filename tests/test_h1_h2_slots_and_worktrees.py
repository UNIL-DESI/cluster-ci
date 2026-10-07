"""
Tests unitaires et d'intégration pour le Lot H (Chantiers H1 & H2) :
- Allocation de slots GPU distincts (0 puis 1) sur une machine multi-GPU
- Mise en attente du 3e nœud lorsque la capacité GPU est saturée
- Isolation des worktrees Git par runner et montage central du cache DVC
- Nommage sans collision des conteneurs incluant safe_runner_id
- Filtrage des jobs actifs dans worker_poll
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from src.scheduler.persistence import init_db, get_db_conn
from src.scheduler.scheduler_loop import allocate_gpus, get_worker_allocated_resources, handle_next_node
from src.runner.branch_executor import BranchExecutor, DockerRunner


class TestH1H2SlotsAndWorktrees(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_h1_h2.db")
        os.environ["DATABASE_PATH"] = self.db_path
        init_db()

    def tearDown(self):
        if "DATABASE_PATH" in os.environ:
            del os.environ["DATABASE_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_multi_gpu_slot_assignment_and_saturation(self):
        """Vérifie l'attribution des slots GPU 0 et 1 sur machine 2 GPU et blocage du 3e nœud."""
        worker = {
            "worker_id": "gpu-worker-2x",
            "gpu_count": 2,
            "gpu_name": "2x RTX 3090",
            "vram_per_gpu": "[24.0, 24.0]",
            "cpus": 16,
            "total_ram": 64.0,
            "status": "ready",
            "arch": "x86_64",
        }

        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM job_nodes")
            c.execute("DELETE FROM jobs")
            c.execute("DELETE FROM workers")
            c.execute(
                "INSERT INTO workers (worker_id, gpu_count, gpu_name, vram_per_gpu, cpus, total_ram_gb, status, arch) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (worker["worker_id"], worker["gpu_count"], worker["gpu_name"], worker["vram_per_gpu"], worker["cpus"], worker["total_ram"], worker["status"], worker["arch"])
            )
            # Créer 2 jobs
            c.execute("INSERT INTO jobs (job_id, status, parallel_mode, repo, branch) VALUES ('job-A', 'running', 1, 'org/repo', 'main')")
            c.execute("INSERT INTO jobs (job_id, status, parallel_mode, repo, branch) VALUES ('job-B', 'running', 1, 'org/repo', 'main')")

            # Nœud 1 pour job-A
            c.execute(
                "INSERT INTO job_nodes (job_id, node_name, status, resources) VALUES ('job-A', 'train_a', 'ready', ?)",
                (json.dumps({"gpus": 1, "vram_gb": 12.0, "cpus": 4, "ram_gb": 8.0}),)
            )
            # Nœud 2 pour job-B
            c.execute(
                "INSERT INTO job_nodes (job_id, node_name, status, resources) VALUES ('job-B', 'train_b', 'ready', ?)",
                (json.dumps({"gpus": 1, "vram_gb": 12.0, "cpus": 4, "ram_gb": 8.0}),)
            )
            # Nœud 3 pour job-A
            c.execute(
                "INSERT INTO job_nodes (job_id, node_name, status, resources) VALUES ('job-A', 'train_c', 'ready', ?)",
                (json.dumps({"gpus": 1, "vram_gb": 12.0, "cpus": 4, "ram_gb": 8.0}),)
            )
            conn.commit()

        # 1. Allocation pour le premier nœud : doit obtenir le GPU 0
        req_1 = {
            "worker_id": "gpu-worker-2x",
            "runner_id": "runner-A-1",
            "job_id": "job-A",
            "current_image": "default-img",
        }
        res_1 = handle_next_node(req_1)
        self.assertIn(res_1.get("action"), ("run", "switch_image"))
        self.assertEqual(res_1.get("node"), "train_a")
        self.assertEqual(res_1.get("gpu_ids"), [0])
        self.assertEqual(res_1.get("gpu_indices"), [0])

        # 2. Allocation pour le deuxième nœud : doit obtenir le GPU 1 (slot distinct)
        req_2 = {
            "worker_id": "gpu-worker-2x",
            "runner_id": "runner-B-1",
            "job_id": "job-B",
            "current_image": "default-img",
        }
        res_2 = handle_next_node(req_2)
        self.assertIn(res_2.get("action"), ("run", "switch_image"))
        self.assertEqual(res_2.get("node"), "train_b")
        self.assertEqual(res_2.get("gpu_ids"), [1])
        self.assertEqual(res_2.get("gpu_indices"), [1])

        # 3. Allocation pour le troisième nœud : la machine est saturée (0 et 1 occupés), doit attendre
        req_3 = {
            "worker_id": "gpu-worker-2x",
            "runner_id": "runner-A-2",
            "job_id": "job-A",
            "current_image": "default-img",
        }
        res_3 = handle_next_node(req_3)
        self.assertEqual(res_3.get("action"), "yield")

        # Vérifier en base que les gpu_indices sont bien persistés
        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute("SELECT node_name, gpu_indices FROM job_nodes WHERE status = 'running'")
            rows = dict(c.fetchall())
            self.assertEqual(json.loads(rows["train_a"]), [0])
            self.assertEqual(json.loads(rows["train_b"]), [1])

    def test_gpu_indices_freed_on_retry_and_cancellation(self):
        """Vérifie que gpu_indices est bien libéré à '[]' lors d'un échec/retry et d'une annulation."""
        from src.scheduler.persistence import handle_node_failure_or_retry
        from src.scheduler.scheduler_loop import cancel_job_cleanly

        worker = {
            "worker_id": "gpu-worker-1x",
            "gpu_count": 1,
            "gpu_name": "1x RTX 3090",
            "vram_per_gpu": "[24.0]",
            "cpus": 8,
            "total_ram": 32.0,
            "status": "ready",
            "arch": "x86_64",
        }

        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM job_nodes")
            c.execute("DELETE FROM jobs")
            c.execute("DELETE FROM workers")
            c.execute(
                "INSERT INTO workers (worker_id, gpu_count, gpu_name, vram_per_gpu, cpus, total_ram_gb, status, arch) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (worker["worker_id"], worker["gpu_count"], worker["gpu_name"], worker["vram_per_gpu"], worker["cpus"], worker["total_ram"], worker["status"], worker["arch"])
            )
            c.execute("INSERT INTO jobs (job_id, status, parallel_mode, repo, branch) VALUES ('job-retry', 'running', 1, 'org/repo', 'main')")
            # Nœud 1 en running avec gpu_indices = [0]
            c.execute(
                "INSERT INTO job_nodes (job_id, node_name, status, worker_id, gpu_ids, gpu_indices, resources) VALUES ('job-retry', 'node_1', 'running', 'gpu-worker-1x', '[\"0\"]', '[\"0\"]', ?)",
                (json.dumps({"gpus": 1, "vram_gb": 12.0, "cpus": 4, "ram_gb": 8.0}),)
            )
            # Nœud 2 prêt
            c.execute(
                "INSERT INTO job_nodes (job_id, node_name, status, resources) VALUES ('job-retry', 'node_2', 'ready', ?)",
                (json.dumps({"gpus": 1, "vram_gb": 12.0, "cpus": 4, "ram_gb": 8.0}),)
            )
            conn.commit()

            # 1. Échec de node_1 déclenchant un retry
            res = handle_node_failure_or_retry(conn, 'job-retry', 'node_1', exit_code=1, max_retries=2)
            self.assertEqual(res["action"], "retry")

            # Vérifier que node_1 a bien gpu_indices = '[]'
            c.execute("SELECT gpu_ids, gpu_indices FROM job_nodes WHERE job_id = 'job-retry' AND node_name = 'node_1'")
            row = c.fetchone()
            self.assertEqual(row[0], "[]")
            self.assertEqual(row[1], "[]")

        # 2. Le 2e nœud GPU peut maintenant démarrer et allouer le slot GPU 0
        req_2 = {
            "worker_id": "gpu-worker-1x",
            "runner_id": "runner-2",
            "job_id": "job-retry",
            "current_image": "default-img",
        }
        res_2 = handle_next_node(req_2)
        self.assertIn(res_2.get("node"), ("node_1", "node_2"))
        self.assertEqual(res_2.get("gpu_indices"), [0])

        # 3. Annulation du job : les nœuds restants doivent libérer gpu_indices
        cancel_job_cleanly("job-retry", exit_code=-15)
        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute("SELECT node_name, status, gpu_ids, gpu_indices FROM job_nodes WHERE job_id = 'job-retry'")
            for row in c.fetchall():
                self.assertEqual(row[1], "blocked")
                self.assertEqual(row[2], "[]")
                self.assertEqual(row[3], "[]")

    def test_runner_worktree_isolation_and_container_name(self):
        """Vérifie l'isolation des worktrees Git par runner et le nommage unique des conteneurs."""
        repo_dir = os.path.join(self.temp_dir, "test_repo")
        os.makedirs(repo_dir, exist_ok=True)
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "tester@test.io"], cwd=repo_dir, check=True)
        dummy_file = os.path.join(repo_dir, "file.txt")
        with open(dummy_file, "w") as f:
            f.write("hello")
        subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

        # Créer .dvc/cache
        dvc_cache = os.path.join(repo_dir, ".dvc", "cache")
        os.makedirs(dvc_cache, exist_ok=True)

        mock_docker_1 = MagicMock(spec=DockerRunner)
        mock_docker_1.create_volume.return_value = 0
        mock_docker_1.run_container.return_value = 0
        mock_docker_1.exec_in_container.return_value = (0, "ok")

        mock_docker_2 = MagicMock(spec=DockerRunner)
        mock_docker_2.create_volume.return_value = 0
        mock_docker_2.run_container.return_value = 0
        mock_docker_2.exec_in_container.return_value = (0, "ok")

        executor_1 = BranchExecutor(
            headnode_url="http://localhost:5000",
            job_id="job123",
            runner_id="runner_alpha",
            worker_id="worker1",
            repo_dir=repo_dir,
            target_repo="owner/test_repo",
            target_branch="main",
            docker=mock_docker_1,
        )

        executor_2 = BranchExecutor(
            headnode_url="http://localhost:5000",
            job_id="job123",
            runner_id="runner_beta",
            worker_id="worker1",
            repo_dir=repo_dir,
            target_repo="owner/test_repo",
            target_branch="main",
            docker=mock_docker_2,
        )

        # Vérifier que les worktrees sont distincts
        self.assertIsNotNone(executor_1.worktree_dir)
        self.assertIsNotNone(executor_2.worktree_dir)
        self.assertNotEqual(executor_1.worktree_dir, executor_2.worktree_dir)
        self.assertTrue(os.path.isdir(executor_1.worktree_dir))
        self.assertTrue(os.path.isdir(executor_2.worktree_dir))

        # Vérifier le nommage des conteneurs avec safe_runner_id
        executor_1.start_container_for_image("python:3.11-slim")
        executor_2.start_container_for_image("python:3.11-slim")

        call_args_1 = mock_docker_1.run_container.call_args[1]
        call_args_2 = mock_docker_2.run_container.call_args[1]

        self.assertIn("runner_alpha", call_args_1["container_name"])
        self.assertIn("runner_beta", call_args_2["container_name"])
        self.assertNotEqual(call_args_1["container_name"], call_args_2["container_name"])

        # Vérifier le montage du cache DVC central
        self.assertEqual(call_args_1.get("dvc_cache_dir"), dvc_cache)
        self.assertEqual(call_args_2.get("dvc_cache_dir"), dvc_cache)

        # Nettoyage
        executor_1.cleanup_worktree()
        executor_2.cleanup_worktree()
        self.assertFalse(os.path.exists(executor_1.worktree_dir or ""))
        self.assertFalse(os.path.exists(executor_2.worktree_dir or ""))

    def test_worker_poll_multi_job_and_active_jobs_filtering(self):
        """Vérifie que worker_poll saute les jobs classiques actifs et sert les autres jobs éligibles."""
        from src.scheduler.headnode_service import app
        client = app.test_client()

        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM job_nodes")
            c.execute("DELETE FROM jobs")
            c.execute("DELETE FROM workers")
            # 1 worker
            c.execute("INSERT INTO workers (worker_id, status) VALUES ('w-poll', 'online')")
            # job-1 classique
            c.execute(
                "INSERT INTO jobs (job_id, status, worker_id, parallel_mode, repo, branch, created_at) "
                "VALUES ('job-1', 'running', 'w-poll', 0, 'org/repo1', 'main', '2026-01-01 10:00:00')"
            )
            # job-2 parallèle
            c.execute(
                "INSERT INTO jobs (job_id, status, worker_id, parallel_mode, repo, branch, created_at) "
                "VALUES ('job-2', 'assigned', 'w-poll', 1, 'org/repo2', 'main', '2026-01-01 10:05:00')"
            )
            c.execute(
                "INSERT INTO job_nodes (job_id, node_name, status) VALUES ('job-2', 'node_p', 'ready')"
            )
            conn.commit()

        # Sans active_jobs, renvoie job-1 (le premier par created_at)
        resp1 = client.get('/worker_poll/w-poll')
        self.assertEqual(resp1.status_code, 200)
        data1 = resp1.get_json()
        self.assertEqual(data1.get("job_id"), "job-1")

        # Avec active_jobs="job-1", doit sauter job-1 et renvoyer job-2
        resp2 = client.get('/worker_poll/w-poll?active_jobs=job-1')
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.get_json()
        self.assertEqual(data2.get("job_id"), "job-2")
        self.assertEqual(data2.get("parallel_mode"), 1)

        # Avec active_jobs="job-1,job-2", job-2 a encore un nœud ready donc peut renvoyer job-2
        resp3 = client.get('/worker_poll/w-poll?active_jobs=job-1,job-2')
        self.assertEqual(resp3.status_code, 200)
        data3 = resp3.get_json()
        self.assertEqual(data3.get("job_id"), "job-2")

        # Si le nœud ready de job-2 passe en running
        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute("UPDATE job_nodes SET status = 'running' WHERE job_id = 'job-2' AND node_name = 'node_p'")
            conn.commit()

        # Maintenant avec active_jobs="job-1,job-2", aucun job éligible restant
        resp4 = client.get('/worker_poll/w-poll?active_jobs=job-1,job-2')
        self.assertEqual(resp4.status_code, 200)
        data4 = resp4.get_json()
        self.assertEqual(data4.get("status"), "no_job")


if __name__ == "__main__":
    unittest.main()
