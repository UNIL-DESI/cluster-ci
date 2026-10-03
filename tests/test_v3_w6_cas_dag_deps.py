"""
Tests pour la résolution sémantique des dépendances DAG (W6) et le montage des caches de roues Docker.
Couvre :
  (1) Seuls les outs (fichiers et sous-fichiers de dossiers) sont demandés au CAS.
  (2) Un fichier git manquant -> échec explicite (cause + remède).
  (3) Une sortie présente avec le bon md5 -> aucune récupération CAS.
  (4) Montage des volumes partagés cluster-ci-uv-cache et cluster-ci-pip-cache.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.runner.branch_executor import BranchExecutor, DockerRunner
from src.runner.fetch_cas_dependencies import get_dvc_command
from src.scheduler.artifact_registry import extract_node_deps_from_dvc_lock


class TestDockerRunWheelCacheMounts(unittest.TestCase):
    """Test unitaire vérifiant les arguments de docker run et l'ordre des montages de volumes."""

    @patch("subprocess.run")
    def test_wheel_caches_mounted_over_home_volume(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        runner = DockerRunner(docker_cmd="docker")

        ret = runner.run_container(
            image="nvcr.io/nvidia/pytorch:26.05-py3",
            container_name="test-container",
            home_volume="cluster-ci-home-repo-image",
            repo_dir="/tmp/repo",
            base_dir="/tmp/base",
            ram_limit=8.0,
            vram_limit=0.0,
            user_id=1000,
            group_id=1000,
        )
        self.assertEqual(ret, 0)
        self.assertTrue(mock_run.called)
        cmd = mock_run.call_args[0][0]

        # Verify home volume and shared wheel caches
        home_arg = "cluster-ci-home-repo-image:/home/user"
        uv_arg = "cluster-ci-uv-cache:/home/user/.cache/uv"
        pip_arg = "cluster-ci-pip-cache:/home/user/.cache/pip"

        self.assertIn(home_arg, cmd)
        self.assertIn(uv_arg, cmd)
        self.assertIn(pip_arg, cmd)

        # Verify mount order: home volume mounted first, then uv and pip sub-mounts overlay it
        idx_home = cmd.index(home_arg)
        idx_uv = cmd.index(uv_arg)
        idx_pip = cmd.index(pip_arg)

        self.assertLess(idx_home, idx_uv, "uv cache must be mounted after home volume")
        self.assertLess(idx_home, idx_pip, "pip cache must be mounted after home volume")


class TestRealDvcDagDepsResolution(unittest.TestCase):
    """
    Tests d'intégration sur un VRAI dépôt git + DVC dans un dossier temporaire,
    SANS mock du parseur DVC / YAML.
    """

    @classmethod
    def setUpClass(cls):
        cls.temp_root = Path(tempfile.mkdtemp(prefix="test_dvc_dag_"))
        dvc_cmd = get_dvc_command()

        # 1. Init git and dvc repo
        subprocess.run(["git", "init"], cwd=cls.temp_root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=cls.temp_root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.ch"], cwd=cls.temp_root, check=True, capture_output=True)
        subprocess.run([*dvc_cmd, "init", "--no-scm"], cwd=cls.temp_root, check=True, capture_output=True)

        # 2. Stage A: produces data/out.parquet AND directory data/sub (with nested file f1.txt)
        stage_a_script = cls.temp_root / "stage_a.py"
        stage_a_script.write_text(
            "import pathlib\n"
            "p = pathlib.Path('data')\n"
            "p.mkdir(parents=True, exist_ok=True)\n"
            "(p / 'out.parquet').write_bytes(b'parquet_data_content')\n"
            "sub = p / 'sub'\n"
            "sub.mkdir(parents=True, exist_ok=True)\n"
            "(sub / 'f1.txt').write_text('content_f1_in_sub')\n",
            encoding="utf-8",
        )
        subprocess.run(
            [*dvc_cmd, "stage", "add", "-n", "stage_a", "-o", "data/out.parquet", "-o", "data/sub", sys.executable, "stage_a.py"],
            cwd=cls.temp_root,
            check=True,
            capture_output=True,
        )
        subprocess.run([*dvc_cmd, "repro", "stage_a"], cwd=cls.temp_root, check=True, capture_output=True)

        # 3. Stage B: depends on scripts/b.py (git), configs/x.yaml (git), data/out.parquet (out A), data/sub/f1.txt (subfile out A)
        scripts_dir = cls.temp_root / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        script_b = scripts_dir / "b.py"
        script_b.write_text("open('out_b.txt', 'w').write('done_b')", encoding="utf-8")

        configs_dir = cls.temp_root / "configs"
        configs_dir.mkdir(parents=True, exist_ok=True)
        config_x = configs_dir / "x.yaml"
        config_x.write_text("model: test_model\nlr: 0.001\n", encoding="utf-8")

        subprocess.run(
            [
                *dvc_cmd, "stage", "add", "-n", "stage_b",
                "-d", "scripts/b.py",
                "-d", "configs/x.yaml",
                "-d", "data/out.parquet",
                "-d", "data/sub/f1.txt",
                "-o", "out_b.txt",
                sys.executable, "scripts/b.py",
            ],
            cwd=cls.temp_root,
            check=True,
            capture_output=True,
        )
        subprocess.run([*dvc_cmd, "repro", "stage_b"], cwd=cls.temp_root, check=True, capture_output=True)

        # 4. Stage C: produces hashes/sig.hash with cache: false (-O)
        stage_c_script = cls.temp_root / "stage_c.py"
        stage_c_script.write_text(
            "import pathlib\n"
            "p = pathlib.Path('hashes')\n"
            "p.mkdir(parents=True, exist_ok=True)\n"
            "(p / 'sig.hash').write_text('hash_value_1234')\n",
            encoding="utf-8",
        )
        subprocess.run(
            [*dvc_cmd, "stage", "add", "-n", "stage_c", "-O", "hashes/sig.hash", sys.executable, "stage_c.py"],
            cwd=cls.temp_root,
            check=True,
            capture_output=True,
        )
        subprocess.run([*dvc_cmd, "repro", "stage_c"], cwd=cls.temp_root, check=True, capture_output=True)

        # 5. Stage D: depends on scripts/d.py (git) and hashes/sig.hash (uncached from C)
        script_d = scripts_dir / "d.py"
        script_d.write_text("open('out_d.txt', 'w').write('done_d')", encoding="utf-8")
        subprocess.run(
            [
                *dvc_cmd, "stage", "add", "-n", "stage_d",
                "-d", "scripts/d.py",
                "-d", "hashes/sig.hash",
                "-o", "out_d.txt",
                sys.executable, "scripts/d.py",
            ],
            cwd=cls.temp_root,
            check=True,
            capture_output=True,
        )
        subprocess.run([*dvc_cmd, "repro", "stage_d"], cwd=cls.temp_root, check=True, capture_output=True)

        # Commit everything to git
        subprocess.run(["git", "add", "."], cwd=cls.temp_root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial pipeline commit"], cwd=cls.temp_root, check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        for root, dirs, files in os.walk(cls.temp_root):
            for f in files:
                try:
                    os.chmod(os.path.join(root, f), stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    pass
        shutil.rmtree(cls.temp_root, ignore_errors=True)

    def setUp(self):
        self.executor = BranchExecutor(
            headnode_url="http://127.0.0.1:5000",
            job_id="test-job-w6",
            runner_id="runner-1",
            worker_id="worker-1",
            repo_dir=str(self.temp_root),
            target_repo="test/repo",
            target_branch="main",
        )

    def test_1_only_stage_outs_requested_from_cas(self):
        """
        Vérifie que (1) SEULS les outs de stage (data/out.parquet et data/sub/f1.txt)
        sont demandés au CAS. Les fichiers git (scripts/b.py et configs/x.yaml)
        ne doivent JAMAIS être inclus dans les dépendances CAS.
        """
        dep_paths = ["scripts/b.py", "configs/x.yaml", "data/out.parquet", "data/sub/f1.txt"]

        # Supprimer temporairement les sorties de l'espace de travail pour forcer le rapatriement CAS
        out_parquet = self.temp_root / "data" / "out.parquet"
        out_sub_f1 = self.temp_root / "data" / "sub" / "f1.txt"
        backup_parquet = out_parquet.read_bytes()
        backup_f1 = out_sub_f1.read_text(encoding="utf-8")

        out_parquet.unlink()
        out_sub_f1.unlink()

        try:
            with patch("src.runner.branch_executor.fetch_dependencies") as mock_fetch:
                mock_res = MagicMock()
                mock_res.success = True
                mock_res.missing_deps = []
                mock_res.missing_hashes = []
                mock_fetch.return_value = mock_res

                missing = self.executor.fetch_missing_deps("stage_b", dep_paths=dep_paths)
                self.assertEqual(missing, [])
                self.assertTrue(mock_fetch.called, "fetch_dependencies should be called for missing stage outs")

                # Inspect dependencies passed to fetch_dependencies
                called_deps = mock_fetch.call_args[1].get("dependencies") or mock_fetch.call_args[0][0]
                called_paths = [d.get("path") for d in called_deps if isinstance(d, dict) and d.get("path")]

                # (1) Seuls les outs sont demandés au CAS
                self.assertNotIn("scripts/b.py", called_paths, "scripts/b.py (git) must NEVER be requested from CAS!")
                self.assertNotIn("configs/x.yaml", called_paths, "configs/x.yaml (git) must NEVER be requested from CAS!")
                self.assertIn("data/out.parquet", called_paths, "data/out.parquet (stage out) must be requested from CAS")
                self.assertIn("data/sub/f1.txt", called_paths, "data/sub/f1.txt (subpath stage out) must be requested from CAS")
        finally:
            out_parquet.write_bytes(backup_parquet)
            out_sub_f1.write_text(backup_f1, encoding="utf-8")

    def test_2_missing_git_file_fails_explicitly(self):
        """
        Vérifie que (2) un fichier git manquant dans le checkout local
        déclenche un échec explicite sans tentative CAS.
        """
        script_b = self.temp_root / "scripts" / "b.py"
        backup_b = script_b.read_text(encoding="utf-8")
        script_b.unlink()

        try:
            with patch("src.runner.branch_executor.fetch_dependencies") as mock_fetch:
                missing = self.executor.fetch_missing_deps(
                    "stage_b",
                    dep_paths=["scripts/b.py", "configs/x.yaml", "data/out.parquet", "data/sub/f1.txt"],
                )
                self.assertIn("scripts/b.py", missing)
                # fetch_dependencies must NOT have been called for scripts/b.py
                if mock_fetch.called:
                    called_deps = mock_fetch.call_args[1].get("dependencies") or mock_fetch.call_args[0][0]
                    called_paths = [d.get("path") for d in called_deps if isinstance(d, dict)]
                    self.assertNotIn("scripts/b.py", called_paths)
        finally:
            script_b.write_text(backup_b, encoding="utf-8")

    def test_3_output_present_with_correct_md5_skips_recovery(self):
        """
        Vérifie que (3) si une sortie de stage est présente localement
        AVEC le bon md5 (calculé), aucune récupération CAS n'est tentée.
        """
        # Assurer que tous les fichiers (git et sorties de stage) sont présents et intacts
        self.assertTrue((self.temp_root / "scripts" / "b.py").is_file())
        self.assertTrue((self.temp_root / "configs" / "x.yaml").is_file())
        self.assertTrue((self.temp_root / "data" / "out.parquet").is_file())
        self.assertTrue((self.temp_root / "data" / "sub" / "f1.txt").is_file())

        with patch("src.runner.branch_executor.fetch_dependencies") as mock_fetch:
            missing = self.executor.fetch_missing_deps(
                "stage_b",
                dep_paths=["scripts/b.py", "configs/x.yaml", "data/out.parquet", "data/sub/f1.txt"],
            )
            self.assertEqual(missing, [])
            # Aucune récupération CAS requise !
            self.assertFalse(mock_fetch.called, "fetch_dependencies should NOT be called when outs are already valid locally")

    def test_4_uncached_output_consumed_by_downstream_node(self):
        """
        Vérifie qu'une sortie avec cache: false (stage_c) consommée par un nœud aval (stage_d) :
        - Est acceptée sans jamais solliciter le CAS lorsqu'elle est présente localement.
        - Échoue explicitement avec cause/remède git sans solliciter le CAS lorsqu'elle est absente.
        - Est automatiquement ajoutée à git (git add -f) par commit_and_push_node.
        """
        sig_file = self.temp_root / "hashes" / "sig.hash"
        backup_sig = sig_file.read_text(encoding="utf-8")

        # Cas A : Présente localement -> 0 CAS, 0 échec
        with patch("src.runner.branch_executor.fetch_dependencies") as mock_fetch:
            missing = self.executor.fetch_missing_deps(
                "stage_d",
                dep_paths=["scripts/d.py", "hashes/sig.hash"],
            )
            self.assertEqual(missing, [])
            self.assertFalse(mock_fetch.called, "CAS fetch must NEVER be called for cache: false outputs")

        # Cas B : Absente localement -> échec explicite sans CAS
        sig_file.unlink()
        try:
            with patch("src.runner.branch_executor.fetch_dependencies") as mock_fetch:
                missing = self.executor.fetch_missing_deps(
                    "stage_d",
                    dep_paths=["scripts/d.py", "hashes/sig.hash"],
                )
                self.assertIn("hashes/sig.hash", missing)
                self.assertFalse(mock_fetch.called, "CAS fetch must NEVER be called even when cache: false output is missing")
        finally:
            sig_file.write_text(backup_sig, encoding="utf-8")

        # Cas C : commit_and_push_node commite les sorties cache: false
        from src.runner.branch_executor import commit_and_push_node
        with patch("src.runner.dvc_git_helper.push_with_retries") as mock_push:
            sig_file.write_text("modified_hash_val", encoding="utf-8")
            ok = commit_and_push_node(str(self.temp_root), "stage_c", "main")
            self.assertTrue(ok)
            res = subprocess.run(["git", "status", "--porcelain"], cwd=self.temp_root, capture_output=True, text=True)
            self.assertEqual(res.stdout.strip(), "")
            self.assertTrue(mock_push.called)

    def test_5_upstream_reproduced_new_md5_accepted_and_incoherent_refused(self):
        """
        Vérifie qu'une étape amont relancée qui change le md5 d'une sortie cachée :
        - Le nœud aval accepte le nouveau md5 produit par l'amont (already_valid = True, 0 CAS).
        - Le nœud aval refuse un md5 incohérent (ou ancien) et demande au CAS le NOUVEAU md5 de l'amont.
        """
        import yaml
        from src.runner.fetch_cas_dependencies import compute_file_md5

        out_parquet = self.temp_root / "data" / "out.parquet"
        backup_parquet = out_parquet.read_bytes()

        dvc_lock_file = self.temp_root / "dvc.lock"
        with open(dvc_lock_file, "r", encoding="utf-8") as f:
            lock_data = yaml.safe_load(f)

        old_stage_a_md5 = next(o["md5"] for o in lock_data["stages"]["stage_a"]["outs"] if o.get("path") == "data/out.parquet")
        old_stage_b_dep_md5 = next(d["md5"] for d in lock_data["stages"]["stage_b"]["deps"] if d.get("path") == "data/out.parquet")
        self.assertEqual(old_stage_a_md5, old_stage_b_dep_md5)

        try:
            # Simuler que stage_a a produit un nouveau fichier avec un nouveau hash
            new_content = b"brand_new_parquet_data_content_from_stage_a_repro"
            out_parquet.write_bytes(new_content)
            new_md5 = compute_file_md5(str(out_parquet))
            self.assertNotEqual(new_md5, old_stage_a_md5)

            # Mettre à jour dvc.lock uniquement pour stage_a.outs (comme après repro de stage_a)
            # stage_b.deps CONSERVE l'ancien md5 (n'a pas encore été repro)
            for o in lock_data["stages"]["stage_a"]["outs"]:
                if o.get("path") == "data/out.parquet":
                    o["md5"] = new_md5
            with open(dvc_lock_file, "w", encoding="utf-8") as f:
                yaml.dump(lock_data, f)

            # 1. Le nœud aval (stage_b) doit ACCEPTER le nouveau md5 déjà présent sur disque
            with patch("src.runner.branch_executor.fetch_dependencies") as mock_fetch:
                missing = self.executor.fetch_missing_deps(
                    "stage_b",
                    dep_paths=["scripts/b.py", "configs/x.yaml", "data/out.parquet", "data/sub/f1.txt"],
                )
                self.assertEqual(missing, [])
                self.assertFalse(mock_fetch.called, "New md5 produced by stage_a should be accepted directly without CAS")

            # 2. Si le fichier sur disque a un md5 incohérent (ex. ancien ou corrompu),
            # stage_b doit REFUSER et demander au CAS le NOUVEAU md5 de stage_a (pas l'ancien)
            out_parquet.write_bytes(b"incoherent_or_stale_data")
            with patch("src.runner.branch_executor.fetch_dependencies") as mock_fetch:
                mock_res = MagicMock()
                mock_res.success = True
                mock_res.missing_deps = []
                mock_res.missing_hashes = []
                mock_fetch.return_value = mock_res

                missing = self.executor.fetch_missing_deps(
                    "stage_b",
                    dep_paths=["scripts/b.py", "configs/x.yaml", "data/out.parquet", "data/sub/f1.txt"],
                )
                self.assertEqual(missing, [])
                self.assertTrue(mock_fetch.called, "CAS fetch must be called when local file has incoherent hash")

                # Vérifier que le hash demandé au CAS est bien NEW_MD5 (produit par stage_a), et NON l'ancien hash
                called_deps = mock_fetch.call_args[1].get("dependencies") or mock_fetch.call_args[0][0]
                parquet_dep = next(d for d in called_deps if d.get("path") == "data/out.parquet")
                self.assertEqual(parquet_dep.get("md5"), new_md5, "Expected hash must be the FRESH producer hash, not stale consumer dep hash")

        finally:
            out_parquet.write_bytes(backup_parquet)
            lock_data["stages"]["stage_a"]["outs"][0]["md5"] = old_stage_a_md5
            with open(dvc_lock_file, "w", encoding="utf-8") as f:
                yaml.dump(lock_data, f)


if __name__ == "__main__":
    unittest.main()
