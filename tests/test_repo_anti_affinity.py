"""
Tests ciblés pour l'anti-affinité par dépôt dans le scheduler et la purge ciblée des conteneurs.

Vérifie que :
1. Deux jobs distincts ciblant le MÊME dépôt (repo) ne peuvent pas être assignés au même worker
   simultanément dans scheduler_loop (anti-affinité par dépôt pour isoler les workspaces Git).
2. purge_orphan_runners_and_containers dans worker_agent protège tous les conteneurs associés
   aux jobs et runners actifs (y compris avec suffixes de runners et d'images) et ne purge que
   les vrais conteneurs orphelins.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.scheduler.persistence import init_db, get_db_conn
from src.scheduler.scheduler_loop import schedule_iteration, get_worker_allocated_resources
from src.scheduler.worker_agent import purge_orphan_runners_and_containers, active_executors, job_lock


class TestRepoAntiAffinityAndPurge(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_anti_affinity_")
        self.db_path = os.path.join(self.temp_dir, "test_scheduler.db")
        os.environ["DATABASE_PATH"] = self.db_path
        init_db()

    def tearDown(self):
        if "DATABASE_PATH" in os.environ:
            del os.environ["DATABASE_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_classic_jobs_same_repo_anti_affinity_on_same_worker(self):
        """
        Deux jobs classiques distincts pour le même dépôt (mais branches distinctes)
        ne doivent JAMAIS être assignés au même worker en même temps.
        Si un seul worker est disponible, le 2e job doit rester en attente (pending).
        """
        worker = {
            "worker_id": "worker-1",
            "hostname": "worker1.cluster",
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
                "INSERT INTO workers (worker_id, hostname, gpu_count, gpu_name, vram_per_gpu, cpus, total_ram_gb, status, last_seen, arch) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'online', CURRENT_TIMESTAMP, ?)",
                (worker["worker_id"], worker["hostname"], worker["gpu_count"], worker["gpu_name"],
                 worker["vram_per_gpu"], worker["cpus"], worker["total_ram"], worker["arch"])
            )
            # Job 1 déjà en cours sur worker-1 pour org/research-repo
            c.execute(
                "INSERT INTO jobs (job_id, status, worker_id, parallel_mode, repo, branch, ram_required_gb, vram_required_gb) "
                "VALUES ('job-1', 'running', 'worker-1', 0, 'org/research-repo', 'main', 8.0, 0.0)"
            )
            # Job 2 en attente pour le MÊME repo mais branche dev
            c.execute(
                "INSERT INTO jobs (job_id, status, parallel_mode, repo, branch, ram_required_gb, vram_required_gb) "
                "VALUES ('job-2', 'pending', 0, 'org/research-repo', 'dev', 8.0, 0.0)"
            )
            conn.commit()

        # Exécuter un cycle d'ordonnancement
        schedule_iteration()

        # Vérifier l'état de job-2 en base
        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute("SELECT status, worker_id FROM jobs WHERE job_id = 'job-2'")
            status, worker_id = c.fetchone()

            # job-2 ne doit PAS avoir été assigné à worker-1 (anti-affinité par dépôt)
            self.assertNotEqual(
                worker_id, "worker-1",
                "Violation d'anti-affinité : job-2 a été assigné au même worker que job-1 pour le même repo 'org/research-repo'"
            )
            self.assertEqual(status, "pending", "job-2 doit rester 'pending' tant qu'aucun worker distinct n'est libre")

    @patch("src.scheduler.worker_agent.safe_docker_rm_f")
    @patch("src.scheduler.worker_agent.subprocess.run")
    @patch("src.scheduler.worker_agent.prune_all_git_worktrees")
    @patch("src.scheduler.worker_agent.purge_ollama_vram_on_host")
    def test_purge_orphan_containers_protects_active_runners_with_suffixes(
        self, mock_ollama, mock_worktrees, mock_subproc, mock_safe_rm
    ):
        """
        purge_orphan_runners_and_containers ne doit pas purger les conteneurs des runners
        actifs ayant un suffixe (ex: cluster-job-{job_id}-{runner_id}-{image_slug} en mode v3).
        Seuls les conteneurs orphelins doivent être purgés.
        """
        # Simuler un exécuteur actif pour job-A
        with job_lock:
            active_executors.clear()
            active_executors["runner-A1"] = {
                "runner_id": "runner-A1",
                "job_id": "job-A",
                "repo": "org/repo",
                "branch": "main",
                "is_parallel": True,
                "process": None
            }

        try:
            # Docker ps retourne 3 conteneurs :
            # 1. Conteneur actif v3 avec runner_id et image
            # 2. Conteneur viewer actif
            # 3. Conteneur orphelin d'un ancien job terminé
            mock_res = MagicMock()
            mock_res.returncode = 0
            mock_res.stdout = (
                "cluster-job-job-A-runner-A1-python-3-11-slim\n"
                "cluster-viewer-job-A\n"
                "cluster-job-old-stale-job-999\n"
            )
            mock_subproc.return_value = mock_res

            # Exécuter la purge pour un nouveau job entrant 'job-B'
            purge_orphan_runners_and_containers(job_id="job-B")

            # Collecter les conteneurs envoyés à safe_docker_rm_f
            purged_containers = [call.args[0] for call in mock_safe_rm.call_args_list]

            # Le conteneur actif de job-A NE DOIT PAS être purgé
            self.assertNotIn(
                "cluster-job-job-A-runner-A1-python-3-11-slim",
                purged_containers,
                "Régression purge : le conteneur du runner actif job-A a été détruit par erreur !"
            )
            # Le viewer actif de job-A NE DOIT PAS être purgé
            self.assertNotIn(
                "cluster-viewer-job-A",
                purged_containers,
                "Régression purge : le viewer du job actif job-A a été détruit par erreur !"
            )
            # Le conteneur orphelin DOIT être purgé
            self.assertIn(
                "cluster-job-old-stale-job-999",
                purged_containers,
                "Le conteneur orphelin aurait dû être purgé !"
            )
        finally:
            with job_lock:
                active_executors.clear()


if __name__ == "__main__":
    unittest.main()
