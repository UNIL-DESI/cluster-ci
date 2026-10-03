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

        # Free space below panic threshold
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

        # Recent (< 6h) project must be preserved
        assert reg["proj_recent_idle"]["status"] == "idle"
        assert (self.repo_dir / "proj_recent_idle").exists()
        # Old (> 6h) project is purged
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
        # Initially marked idle 10h ago
        self._create_project("starting_proj", status="idle", hours_ago=10.0, files={"f.bin": 1000})
        monkeypatch.setattr(gc, "get_free_space", lambda: 10 * 1024**3)

        # Execute run-gc passing current_project
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
        # Project with both DVC cache and workspace
        self._create_project("p_test", status="idle", hours_ago=20.0, files={"code.py": 1000}, has_dvc_cache=True)

        current_space = [40 * 1024**3]  # starts at 40 GB (< 50 GB threshold)
        def mock_free_space():
            return current_space[0]

        monkeypatch.setattr(gc, "get_free_space", mock_free_space)

        # When cleanup_level_4 (DVC cache) runs, free space increases to 60 GB (> 50 GB threshold)
        original_l4 = gc.cleanup_level_4
        def fake_l4(path, name=None):
            res = original_l4(path, name)
            current_space[0] = 60 * 1024**3  # target reached!
            return res

        monkeypatch.setattr(gc, "cleanup_level_4", fake_l4)

        gc.run_gc()

        # DVC cache was deleted
        assert not (self.repo_dir / "p_test" / ".dvc" / "cache").exists()
        # But workspace was NOT deleted because target was reached in Tier 3!
        assert (self.repo_dir / "p_test" / "code.py").exists()
        with open(gc.get_registry_path(), "r") as f:
            reg = json.load(f)
        assert reg["p_test"]["status"] == "idle"  # Not deleted

    def test_docker_volume_schema_v3(self, monkeypatch):
        """(2) Nettoyage des volumes Docker y compris le schéma v3 cluster-ci-home-<repo>-<image_slug>."""
        p_path = self._create_project("my-lab/my-repo", status="idle", hours_ago=12.0)

        fake_docker_volumes = [
            "cluster-ci-home-my-lab-my-repo",
            "cluster-ci-home-my-lab-my-repo-cuda12",
            "cluster-ci-home-my-lab-my-repo-pytorch2",
            "cluster-ci-home-other-proj"  # should not be touched
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

        # Run Tier 2 cleanup for my-lab/my-repo
        gc.cleanup_all_project_docker_volumes(p_path, "my-lab/my-repo")

        # Must have deleted base volume + both image slug volumes
        assert "cluster-ci-home-my-lab-my-repo" in deleted_volumes
        assert "cluster-ci-home-my-lab-my-repo-cuda12" in deleted_volumes
        assert "cluster-ci-home-my-lab-my-repo-pytorch2" in deleted_volumes
        assert "cluster-ci-home-other-proj" not in deleted_volumes

    def test_dry_run_mode(self, monkeypatch, capsys):
        """(4) Mode --dry-run : aucune suppression réelle sur le disque ni modification du registre."""
        p_path = self._create_project("proj_dry", status="idle", hours_ago=20.0, files={"important.bin": 5000}, has_dvc_cache=True)
        monkeypatch.setattr(gc, "get_free_space", lambda: 10 * 1024**3)

        gc.run_gc(dry_run=True)

        # Check stdout contains DRY-RUN logs
        captured = capsys.readouterr().out
        assert "[GC DRY-RUN]" in captured
        assert "Would delete" in captured

        # Files on disk must STILL exist!
        assert (p_path / "important.bin").exists()
        assert (p_path / ".dvc" / "cache").exists()

        # Registry must NOT be marked deleted
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
