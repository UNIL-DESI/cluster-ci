import os
import json
import shutil
import time
import fcntl
import subprocess
from pathlib import Path
from unittest import mock
import pytest

from src.runner import gc_orchestrator as gc

class TestGCW9Deliverables:
    @pytest.fixture(autouse=True)
    def setup_env(self, tmp_path, monkeypatch):
        self.tmp_path = tmp_path
        self.repo_dir = tmp_path / "repositories"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(gc, "get_base_dir", lambda: tmp_path)
        monkeypatch.setattr(gc, "get_repositories_dir", lambda: self.repo_dir)
        # Clear GC_PROTECT_HOURS override by default
        monkeypatch.delenv("GC_PROTECT_HOURS", raising=False)
        monkeypatch.delenv("WORKSPACE_KEY", raising=False)
        monkeypatch.delenv("TARGET_REPO", raising=False)

    def _create_project(self, name, status="idle", hours_ago=10.0, files=None, has_dvc_cache=False):
        p_path = self.repo_dir / name
        p_path.mkdir(parents=True, exist_ok=True)
        if files:
            for fname, size in files.items():
                fpath = p_path / fname
                fpath.parent.mkdir(parents=True, exist_ok=True)
                with open(fpath, "wb") as f:
                    if size > 0:
                        f.seek(size - 1)
                        f.write(b"0")
                    else:
                        f.write(b"")
        if has_dvc_cache:
            cache_path = p_path / ".dvc" / "cache"
            cache_path.mkdir(parents=True, exist_ok=True)
            with open(cache_path / "data_obj", "wb") as f:
                f.seek(1024 * 1024 - 1)
                f.write(b"0")

        last_exec = time.time() - (hours_ago * 3600.0)
        registry_path = gc.get_registry_path()
        registry = {}
        if registry_path.exists():
            with open(registry_path, "r") as f:
                registry = json.load(f)
        registry[name] = {
            "status": status,
            "last_execution": last_exec,
            "size_bytes": gc.get_dir_size(p_path)
        }
        with open(registry_path, "w") as f:
            json.dump(registry, f, indent=4)
        return p_path

    def test_protection_running_project(self, monkeypatch):
        """(1) Jamais de purge d'un projet en cours d'exécution."""
        self._create_project("proj_running", status="running", hours_ago=20.0, files={"data.bin": 1000})
        self._create_project("proj_old_idle", status="idle", hours_ago=20.0, files={"old.bin": 1000})

        monkeypatch.setattr(gc, "get_free_space", lambda: 10 * 1024**3)

        gc.run_gc()

        with open(gc.get_registry_path(), "r") as f:
            reg = json.load(f)

        assert reg["proj_running"]["status"] == "running"
        assert (self.repo_dir / "proj_running").exists()
        assert reg["proj_old_idle"]["status"] == "deleted"
        assert not (self.repo_dir / "proj_old_idle").exists()

    def test_protection_recent_idle_project_default_6h(self, monkeypatch):
        """(1) Jamais de purge d'un projet inactif exécuté depuis moins de 6 heures."""
        self._create_project("proj_recent_idle", status="idle", hours_ago=2.0, files={"recent.bin": 1000})
        self._create_project("proj_old_idle", status="idle", hours_ago=10.0, files={"old.bin": 1000})

        monkeypatch.setattr(gc, "get_free_space", lambda: 10 * 1024**3)

        gc.run_gc()

        with open(gc.get_registry_path(), "r") as f:
            reg = json.load(f)

        assert reg["proj_recent_idle"]["status"] == "idle"
        assert (self.repo_dir / "proj_recent_idle").exists()
        assert reg["proj_old_idle"]["status"] == "deleted"
        assert not (self.repo_dir / "proj_old_idle").exists()

    def test_protection_configurable_hours(self, monkeypatch):
        """(1) Seuil N heures configurable via GC_PROTECT_HOURS (ex: 2 heures)."""
        monkeypatch.setenv("GC_PROTECT_HOURS", "2.0")
        self._create_project("proj_1h", status="idle", hours_ago=1.0, files={"f1.bin": 1000})
        self._create_project("proj_3h", status="idle", hours_ago=3.0, files={"f3.bin": 1000})

        monkeypatch.setattr(gc, "get_free_space", lambda: 10 * 1024**3)

        gc.run_gc()

        with open(gc.get_registry_path(), "r") as f:
            reg = json.load(f)

        assert reg["proj_1h"]["status"] == "idle"
        assert (self.repo_dir / "proj_1h").exists()
        assert reg["proj_3h"]["status"] == "deleted"
        assert not (self.repo_dir / "proj_3h").exists()

    def test_race_condition_current_project_marked_running_before_purge(self, monkeypatch):
        """
        Correction de course : run-gc recevant current_project ou WORKSPACE_KEY
        le marque 'running' AVANT toute purge.
        """
        self._create_project("starting_proj", status="idle", hours_ago=10.0, files={"f.bin": 1000})
        monkeypatch.setattr(gc, "get_free_space", lambda: 10 * 1024**3)

        gc.run_gc(current_project="starting_proj")

        with open(gc.get_registry_path(), "r") as f:
            reg = json.load(f)

        assert reg["starting_proj"]["status"] == "running"
        assert (self.repo_dir / "starting_proj").exists()

    def test_lru_eviction_order(self, monkeypatch):
        """(2) Ordre de purge LRU : le projet le plus ancien est évincé en premier."""
        self._create_project("proj_48h", status="idle", hours_ago=48.0, files={"f.bin": 1000})
        self._create_project("proj_24h", status="idle", hours_ago=24.0, files={"f.bin": 1000})

        eviction_order = []
        original_l5 = gc.cleanup_level_5
        def track_l5(path, name=None):
            eviction_order.append(name)
            return original_l5(path, name)

        monkeypatch.setattr(gc, "cleanup_level_5", track_l5)
        monkeypatch.setattr(gc, "get_free_space", lambda: 10 * 1024**3)

        gc.run_gc()

        assert eviction_order == ["proj_48h", "proj_24h"]

    def test_tiers_order_and_stop_when_target_reached(self, monkeypatch):
        """
        (2) Paliers dans l'ordre :
        caches régénérables -> volumes Docker -> cache DVC -> workspace complet.
        Arrêt dès que l'espace cible est atteint !
        """
        self._create_project("p_test", status="idle", hours_ago=20.0, files={"code.py": 1000}, has_dvc_cache=True)

        current_space = [40 * 1024**3]  # starts at 40 GB (< 50 GB threshold)
        def mock_free_space():
            return current_space[0]

        monkeypatch.setattr(gc, "get_free_space", mock_free_space)

        original_l4 = gc.cleanup_level_4
        def fake_l4(path, name=None):
            res = original_l4(path, name)
            current_space[0] = 60 * 1024**3  # target reached!
            return res

        monkeypatch.setattr(gc, "cleanup_level_4", fake_l4)

        gc.run_gc()

        assert not (self.repo_dir / "p_test" / ".dvc" / "cache").exists()
        assert (self.repo_dir / "p_test" / "code.py").exists()
        with open(gc.get_registry_path(), "r") as f:
            reg = json.load(f)
        assert reg["p_test"]["status"] == "idle"

    def test_docker_volume_schema_v3(self, monkeypatch):
        """(2) Nettoyage des volumes Docker y compris le schéma v3 cluster-ci-home-<repo>-<image_slug>."""
        p_path = self._create_project("my-lab/my-repo", status="idle", hours_ago=12.0)

        fake_docker_volumes = [
            "cluster-ci-home-my-lab-my-repo",
            "cluster-ci-home-my-lab-my-repo-cuda12",
            "cluster-ci-home-my-lab-my-repo-pytorch2",
            "cluster-ci-home-other-proj"
        ]

        def fake_volume_ls(*args, **kwargs):
            cmd = args[0]
            if "volume" in cmd and "ls" in cmd:
                return mock.Mock(returncode=0, stdout="\n".join(fake_docker_volumes))
            return mock.Mock(returncode=0, stdout="")

        deleted_volumes = []
        def fake_volume_rm(*args, **kwargs):
            cmd = args[0]
            if "volume" in cmd and "rm" in cmd:
                deleted_volumes.append(cmd[-1])
            return mock.Mock(returncode=0, stdout="")

        def fake_subprocess_run(cmd, *args, **kwargs):
            if "ls" in cmd:
                return fake_volume_ls(cmd)
            elif "rm" in cmd:
                return fake_volume_rm(cmd)
            return mock.Mock(returncode=0, stdout="")

        monkeypatch.setattr(gc.subprocess, "run", fake_subprocess_run)

        gc.cleanup_all_project_docker_volumes(p_path, "my-lab/my-repo")

        assert "cluster-ci-home-my-lab-my-repo" in deleted_volumes
        assert "cluster-ci-home-my-lab-my-repo-cuda12" in deleted_volumes
        assert "cluster-ci-home-my-lab-my-repo-pytorch2" in deleted_volumes
        assert "cluster-ci-home-other-proj" not in deleted_volumes

    def test_dry_run_mode(self, monkeypatch, capsys):
        """(4) Mode --dry-run : aucune suppression réelle sur le disque ni modification du registre."""
        p_path = self._create_project("proj_dry", status="idle", hours_ago=20.0, files={"important.bin": 5000}, has_dvc_cache=True)
        monkeypatch.setattr(gc, "get_free_space", lambda: 10 * 1024**3)

        gc.run_gc(dry_run=True)

        captured = capsys.readouterr().out
        assert "[GC DRY-RUN]" in captured
        assert "Would delete" in captured

        assert (p_path / "important.bin").exists()
        assert (p_path / ".dvc" / "cache").exists()

        with open(gc.get_registry_path(), "r") as f:
            reg = json.load(f)
        assert reg["proj_dry"]["status"] == "idle"

    def test_logging_deletion_format(self, capsys):
        """(3) Journalisation de chaque suppression (chemin, taille libérée, raison)."""
        gc.log_deletion("/path/to/dvc/cache", 1024 * 1024 * 15, "Tier 3: Local DVC cache", dry_run=False)
        captured = capsys.readouterr().out
        assert "[GC LOG] Deleted: '/path/to/dvc/cache'" in captured
        assert "15.00 MB" in captured
        assert "Tier 3: Local DVC cache" in captured

    # --- NOUVEAUX TESTS (Relecture & Corrections W9) ---

    def test_corrupted_registry_halts_gc_and_saves_backup(self, monkeypatch):
        """(1) Registre corrompu -> sauvegarde .bak, aucune purge, arrêt avec code != 0."""
        p_path = self._create_project("proj_safe", status="idle", hours_ago=20.0, files={"safe.bin": 1000})
        registry_path = gc.get_registry_path()
        # Corrupt the JSON file deliberately
        with open(registry_path, "w") as f:
            f.write("{invalid_json: true, unterminated")

        monkeypatch.setattr(gc, "get_free_space", lambda: 10 * 1024**3)

        with pytest.raises(SystemExit) as excinfo:
            gc.run_gc()
        assert excinfo.value.code != 0

        # Project must NOT be purged
        assert (p_path / "safe.bin").exists()

        # A .bak backup must have been created
        bak_files = list(registry_path.parent.glob("registry*.corrupt.*.bak"))
        assert len(bak_files) >= 1

    def test_active_docker_container_protection(self, monkeypatch):
        """(2) Détection 'en cours' : projet protégé si conteneur Docker ou volume actif."""
        p_path = self._create_project("proj_docker_active", status="idle", hours_ago=20.0, files={"active.bin": 1000})
        monkeypatch.setattr(gc, "get_free_space", lambda: 10 * 1024**3)

        # Mock docker ps returning an active container for this project
        monkeypatch.setattr(gc, "get_active_docker_info", lambda: {
            "cluster-job-proj_docker_active",
            "cluster-ci-home-proj_docker_active"
        })

        gc.run_gc()

        with open(gc.get_registry_path(), "r") as f:
            reg = json.load(f)

        # Must NOT be deleted because active in Docker
        assert reg["proj_docker_active"]["status"] == "idle"
        assert (p_path / "active.bin").exists()

    def test_docker_ps_failure_halts_gc(self, monkeypatch):
        """(2) Si 'docker ps' échoue -> aucune purge (erreur explicite, sortie non-zéro)."""
        p_path = self._create_project("proj_safe_docker_fail", status="idle", hours_ago=20.0, files={"safe.bin": 1000})
        monkeypatch.setattr(gc, "get_free_space", lambda: 10 * 1024**3)

        def mock_docker_ps_fail(*args, **kwargs):
            return mock.Mock(returncode=1, stderr="Cannot connect to Docker daemon")

        monkeypatch.setattr(gc.subprocess, "run", mock_docker_ps_fail)

        with pytest.raises(SystemExit) as excinfo:
            gc.run_gc()
        assert excinfo.value.code != 0
        assert (p_path / "safe.bin").exists()

    def test_docker_volume_rm_failure_not_counted(self, monkeypatch, capsys):
        """(4) docker volume rm en échec -> code retour inspecté, non compté comme libéré."""
        p_path = self.repo_dir / "proj_vol"
        p_path.mkdir(parents=True, exist_ok=True)

        def mock_volume_rm_fail(*args, **kwargs):
            return mock.Mock(returncode=1, stderr="volume is in use")

        monkeypatch.setattr(gc.subprocess, "run", mock_volume_rm_fail)

        gc.cleanup_level_3(p_path, "proj_vol")

        captured = capsys.readouterr().out
        assert "Failed to delete Docker volume" in captured
        assert "[GC LOG] Deleted: 'cluster-ci-home-proj_vol'" not in captured

    def test_cleanup_level_2_whitelist(self):
        """(5) cleanup_level_2 : uniquement liste blanche explicite, jamais .venv ni fichiers tracked."""
        p_path = self.repo_dir / "proj_whitelist"
        p_path.mkdir(parents=True, exist_ok=True)

        # 1. Whitelisted cache dirs & files
        pycache_dir = p_path / "__pycache__"
        pycache_dir.mkdir()
        (pycache_dir / "mod.cpython-312.pyc").write_bytes(b"123")

        cache_pip = p_path / ".cache" / "pip"
        cache_pip.mkdir(parents=True)
        (cache_pip / "wheel.whl").write_bytes(b"wheel")

        log_file = p_path / "large.log"
        log_file.write_bytes(b"log data")

        tmp_file = p_path / "temp.tmp"
        tmp_file.write_bytes(b"tmp data")

        # 2. Protected paths: .venv
        venv_dir = p_path / ".venv" / "lib"
        venv_dir.mkdir(parents=True)
        so_file = venv_dir / "libcublasLt.so.13"
        so_file.write_bytes(b"binary so")

        # 3. Regular non-whitelisted untracked file
        regular_file = p_path / "important_data.csv"
        regular_file.write_bytes(b"csv data")

        gc.cleanup_level_2(p_path, "proj_whitelist")

        # Whitelisted caches must be purged
        assert not pycache_dir.exists()
        assert not cache_pip.exists()
        assert not log_file.exists()
        assert not tmp_file.exists()

        # Non-whitelisted and venv files must be strictly preserved
        assert so_file.exists()
        assert regular_file.exists()

    def test_dry_run_zero_disk_writes_with_current_project(self, monkeypatch):
        """(6) --dry-run : AUCUNE écriture sur disque, même avec current_project."""
        self._create_project("proj_dry2", status="idle", hours_ago=20.0, files={"f.bin": 100})
        monkeypatch.setattr(gc, "get_free_space", lambda: 10 * 1024**3)

        registry_path = gc.get_registry_path()
        mtime_before = registry_path.stat().st_mtime_ns
        content_before = registry_path.read_text()

        # Pass current_project in dry-run mode
        gc.run_gc(dry_run=True, current_project="new_job_start")

        mtime_after = registry_path.stat().st_mtime_ns
        content_after = registry_path.read_text()

        assert mtime_before == mtime_after
        assert content_before == content_after
        assert "new_job_start" not in content_after

    def test_validate_project_name(self):
        """(7) validate_project_name : rejette '', '.', '..', séparateurs invalides."""
        valid_names = ["user/repo", "_local/user/repo", "proj1", "my-lab/my-proj_v2"]
        for name in valid_names:
            gc.validate_project_name(name)

        invalid_names = [
            "",
            "   ",
            ".",
            "..",
            "/absolute/path",
            "../escape",
            "a/../b",
            "proj/",
            "/proj",
            "\\proj",
            "a\\b",
            "a//b"
        ]
        for name in invalid_names:
            with pytest.raises(ValueError):
                gc.validate_project_name(name)

    def test_storage_saturation_threshold_required(self, monkeypatch):
        """(1) Le ménage ne se déclenche QUE si stockage saturé (< 50GB / 100GB). Zéro purge à l'âge seul."""
        p_path = self._create_project("very_old_proj", status="idle", hours_ago=500.0, files={"old.bin": 1000})

        # Free space is 150 GB (ample space, > 100 GB maintenance & > 50 GB emergency threshold)
        monkeypatch.setattr(gc, "get_free_space", lambda: 150 * 1024**3)

        gc.run_transfer_gc()
        gc.run_gc()

        # Project MUST remain intact because disk is not saturated
        assert (p_path / "old.bin").exists()
        with open(gc.get_registry_path(), "r") as f:
            reg = json.load(f)
        assert reg["very_old_proj"]["status"] == "idle"

        # Now simulate disk saturation (< 50 GB emergency threshold)
        monkeypatch.setattr(gc, "get_free_space", lambda: 20 * 1024**3)
        gc.run_gc()

        # Now it is evicted
        assert not (p_path / "old.bin").exists()

    def test_a11_shared_dvc_cache_protection_when_active_executor_on_repo(self, monkeypatch):
        """(3) A11 : le cache DVC partagé d'un dépôt n'est purgé que si AUCUN exécuteur de ce dépôt n'est actif."""
        ws_idle = self._create_project("UNIL-DESI/my-repo_runner_1", status="idle", hours_ago=48.0, files={"code.py": 100}, has_dvc_cache=True)
        ws_active = self._create_project("UNIL-DESI/my-repo_runner_2", status="running", hours_ago=0.1, files={"code2.py": 100})

        current_space = [40 * 1024**3]  # starts < 50 GB
        monkeypatch.setattr(gc, "get_free_space", lambda: current_space[0])

        # Intercept cleanup_level_4
        l4_called = []
        orig_l4 = gc.cleanup_level_4
        def track_l4(path, name=None):
            l4_called.append(name)
            return orig_l4(path, name)
        monkeypatch.setattr(gc, "cleanup_level_4", track_l4)

        # Intercept cleanup_level_5
        l5_called = []
        monkeypatch.setattr(gc, "cleanup_level_5", lambda path, name=None: l5_called.append(name))

        gc.run_gc()

        # Level 4 DVC cache MUST NOT be called for ws_idle because ws_active is running on same base repo!
        assert "UNIL-DESI/my-repo_runner_1" not in l4_called
        assert (ws_idle / ".dvc" / "cache").exists()
        # Active workspace MUST NEVER be touched in Tier 5
        assert "UNIL-DESI/my-repo_runner_2" not in l5_called
        assert (ws_active / "code2.py").exists()

    def test_docker_images_lru_cleanup(self, monkeypatch):
        """(2) Images Docker inutilisées triées par LRU, jamais si un conteneur actif ou arrêté existe."""
        def mock_docker_ps_a(*args, **kwargs):
            # Container stopped using image 'used_img:tag'
            return mock.Mock(returncode=0, stdout="used_img:tag\tsha256:111111111111\n")

        def mock_docker_images(*args, **kwargs):
            # 3 images: one used, two unused with different creation dates (LRU)
            stdout = (
                "111111111111\tused_img\ttag\t5.0GB\t2026-05-01 10:00:00\n"
                "222222222222\tunused_old\tv1\t10.0GB\t2023-01-01 10:00:00\n"
                "333333333333\tunused_new\tv2\t15.0GB\t2026-01-01 10:00:00\n"
            )
            return mock.Mock(returncode=0, stdout=stdout)

        monkeypatch.setattr(gc.subprocess, "run", lambda cmd, *a, **kw: (
            mock_docker_ps_a() if "ps" in cmd else
            mock_docker_images() if "images" in cmd else
            mock.Mock(returncode=0, stdout="")
        ))

        unused = gc.get_unused_docker_images()
        # Used image must be filtered out
        assert len(unused) == 2
        # LRU order: oldest (2023) first, then (2026)
        assert unused[0]["id"] == "222222222222"
        assert unused[0]["ref"] == "unused_old:v1"
        assert unused[0]["size_bytes"] == int(10.0 * 1024**3)

        assert unused[1]["id"] == "333333333333"
        assert unused[1]["ref"] == "unused_new:v2"

