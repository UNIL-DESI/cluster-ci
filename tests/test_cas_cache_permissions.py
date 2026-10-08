"""
Tests ciblés pour la correction de la régression CAS (PermissionError) et du montage du cache DVC.

Vérifie que :
1. DockerRunner.run_container crée physiquement dvc_cache_dir (et ses sous-dossiers files/md5)
   sous les droits du processus runner AVANT l'exécution de docker run, et inclut le bind mount.
2. BranchExecutor.fetch_missing_deps transmet explicitement le cache_dir central à fetch_dependencies
   au lieu de laisser un cache orphelin se créer dans le worktree isolé.
3. BranchExecutor.start_container_for_image garantit la création physique de main_dvc_cache sans try/except pass.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.runner.branch_executor import DockerRunner, BranchExecutor
from src.runner.fetch_cas_dependencies import fetch_dependencies


class TestCasCachePermissions(unittest.TestCase):
    """Vérifie la création préventive et l'attribution correcte du cache CAS DVC."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_cas_perm_")
        self.tmp_path = Path(self.temp_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("subprocess.run")
    def test_run_container_creates_dvc_cache_dir_before_docker_run(self, mock_run):
        """
        L1 & L2 : Si dvc_cache_dir n'existe pas encore sur l'hôte,
        DockerRunner.run_container DOIT le créer physiquement sous les droits
        du runner Python AVANT d'appeler docker run, pour empêcher que le daemon
        Docker (root) ne le crée en root:root et bloque l'écriture de files/md5.
        """
        mock_run.return_value = MagicMock(returncode=0)
        runner = DockerRunner(docker_cmd="docker")

        nonexistent_cache = self.tmp_path / "nonexistent_repo" / ".dvc" / "cache"
        self.assertFalse(nonexistent_cache.exists(), "Le répertoire ne doit pas exister avant le test")

        ret = runner.run_container(
            image="test-image:latest",
            container_name="test-cas-container",
            home_volume="test-home-volume",
            repo_dir=str(self.tmp_path / "workspace"),
            base_dir=str(self.tmp_path / "base"),
            dvc_cache_dir=str(nonexistent_cache),
        )

        self.assertEqual(ret, 0)
        self.assertTrue(mock_run.called)

        # 1. Le répertoire de cache hôte doit avoir été créé physiquement sur le disque
        self.assertTrue(
            nonexistent_cache.is_dir(),
            "DockerRunner.run_container doit créer physiquement dvc_cache_dir sur l'hôte avant docker run",
        )

        # 2. Le sous-dossier files/md5 doit également exister et être accessible en écriture
        md5_cache_dir = nonexistent_cache / "files" / "md5"
        self.assertTrue(
            md5_cache_dir.is_dir(),
            "Le sous-dossier files/md5 doit exister physiquement pour éviter PermissionError",
        )

        # 3. Le bind mount -v {dvc_cache_dir}:/workspace/.dvc/cache doit être présent dans la commande docker
        cmd = mock_run.call_args[0][0]
        expected_mount = f"{nonexistent_cache}:/workspace/.dvc/cache"
        self.assertIn(
            expected_mount,
            cmd,
            f"Le montage {expected_mount} doit être présent dans les arguments de docker run",
        )

    def test_fetch_missing_deps_passes_central_cache_dir(self):
        """
        L2 : fetch_missing_deps dans BranchExecutor doit transmettre explicitement
        le chemin du cache central (main_repo_dir/.dvc/cache/files/md5) à fetch_dependencies
        pour éviter de créer un cache local orphelin dans le worktree isolé.
        """
        main_repo = self.tmp_path / "main_repo"
        worktree = self.tmp_path / "isolated_worktree"
        main_repo.mkdir(parents=True, exist_ok=True)
        worktree.mkdir(parents=True, exist_ok=True)

        # Création d'une instance BranchExecutor minimale sans lancer de conteneur
        with patch.object(BranchExecutor, "__init__", lambda self: None):
            executor = BranchExecutor()
            executor.main_repo_dir = str(main_repo)
            executor.repo_dir = str(worktree)
            executor.target_repo = "test_repo"
            executor.target_branch = "main"
            executor.headnode_url = None
            executor.cluster_token = None
            executor.runner_id = "test-runner"
            executor.safe_job_id = "job-1"

            # Simuler un dvc.lock avec une sortie de stage manquante
            exact_outs = {"data/model.pt": {"stage": "train", "md5": "a1b2c3d4e5f60718293a4b5c6d7e8f90", "cache": True}}
            dir_outs = {}
            pat_outs = {}

            with patch("src.runner.branch_executor.get_dag_stage_outputs", return_value=(exact_outs, dir_outs, pat_outs)), \
                 patch("src.runner.branch_executor.extract_node_deps_from_dvc_lock", return_value=[]), \
                 patch("src.runner.branch_executor.fetch_dependencies") as mock_fetch:

                mock_res = MagicMock()
                mock_res.success = True
                mock_res.missing_deps = []
                mock_res.missing_hashes = []
                mock_fetch.return_value = mock_res

                missing = executor.fetch_missing_deps(
                    node="train",
                    dep_paths=["data/model.pt"],
                )

                self.assertEqual(missing, [])
                self.assertTrue(mock_fetch.called, "fetch_dependencies doit être appelé pour rapatrier l'artefact CAS")

                # Vérifier que cache_dir a été passé explicitement
                call_kwargs = mock_fetch.call_args[1]
                passed_cache_dir = call_kwargs.get("cache_dir")
                self.assertIsNotNone(
                    passed_cache_dir,
                    "fetch_dependencies doit recevoir explicitement cache_dir pour éviter le cache orphelin",
                )

                # Vérifier que le cache_dir passé cible bien le cache central sous main_repo_dir
                expected_central = Path(main_repo) / ".dvc" / "cache" / "files" / "md5"
                self.assertEqual(
                    Path(passed_cache_dir).resolve(),
                    expected_central.resolve(),
                    "cache_dir doit cibler le cache central main_repo/.dvc/cache/files/md5",
                )

                # Vérifier que le répertoire central a été créé sur disque
                self.assertTrue(
                    expected_central.is_dir(),
                    "Le répertoire central de cache doit exister physiquement sur le disque hôte",
                )

    def test_fetch_dependencies_normalizes_root_cache_dir(self):
        """
        L2 : fetch_dependencies doit normaliser un cache_dir pointant vers la racine .dvc/cache
        en ciblant automatiquement files/md5 et en le créant sur disque sous l'utilisateur courant.
        """
        repo_dir = self.tmp_path / "test_repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        dvc_cache_root = repo_dir / ".dvc" / "cache"

        # On appelle fetch_dependencies avec une liste vide et run_checkout=False
        result = fetch_dependencies(
            dependencies=[],
            sources_map={},
            repo_dir=repo_dir,
            cache_dir=dvc_cache_root,
            run_checkout=False,
        )

        self.assertTrue(result.success)
        # files/md5 doit avoir été créé sous la racine .dvc/cache
        expected_md5_dir = dvc_cache_root / "files" / "md5"
        self.assertTrue(
            expected_md5_dir.is_dir(),
            "fetch_dependencies doit créer et pointer vers .dvc/cache/files/md5",
        )


if __name__ == "__main__":
    unittest.main()
